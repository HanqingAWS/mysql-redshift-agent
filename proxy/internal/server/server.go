// Package server 实现 MySQL wire protocol 服务端，接收客户端查询转发到 Redshift
//
// 依赖 go-mysql-org/go-mysql/server；该库只提供协议解析与结果回包的基础框架，
// 业务逻辑（翻译 + 执行 + 映射）在这里组装。
package server

import (
	"context"
	"fmt"
	"log"
	"net"
	"strings"
	"time"

	gomysql "github.com/go-mysql-org/go-mysql/mysql"
	mysqlsrv "github.com/go-mysql-org/go-mysql/server"

	"github.com/HanqingAWS/mysql-redshift-agent/proxy/internal/cache"
	"github.com/HanqingAWS/mysql-redshift-agent/proxy/internal/config"
	"github.com/HanqingAWS/mysql-redshift-agent/proxy/internal/convertor_client"
	"github.com/HanqingAWS/mysql-redshift-agent/proxy/internal/executor"
	"github.com/HanqingAWS/mysql-redshift-agent/proxy/internal/knowledge"
	"github.com/HanqingAWS/mysql-redshift-agent/proxy/internal/resultmap"
	"github.com/HanqingAWS/mysql-redshift-agent/proxy/internal/router"
)

type Server struct {
	cfg       config.Config
	rsExec    *executor.Redshift
	myExec    *executor.MySQL // nil if MySQL routing disabled
	router    *router.Router
	cache     *cache.Cache
	agent     *convertor_client.Client
	kb        *knowledge.Client
	authH     *mysqlsrv.InMemoryAuthenticationHandler
	serverCfg *mysqlsrv.Server
}

func New(cfg config.Config, rsExec *executor.Redshift, myExec *executor.MySQL, rtr *router.Router) *Server {
	cc, err := cache.New(cfg.CacheSize)
	if err != nil {
		log.Fatalf("cache init failed: %v", err)
	}
	// Use mysql_native_password — simplest plugin, avoids RSA handshake complexity
	authH := mysqlsrv.NewInMemoryAuthenticationHandler(gomysql.AUTH_NATIVE_PASSWORD)
	if err := authH.AddUser(cfg.MySQLUser, cfg.MySQLPassword, gomysql.AUTH_NATIVE_PASSWORD); err != nil {
		log.Fatalf("add user failed: %v", err)
	}
	srvCfg := mysqlsrv.NewServer("8.0.0-proxy", gomysql.DEFAULT_COLLATION_ID, gomysql.AUTH_NATIVE_PASSWORD, nil, nil)
	return &Server{
		cfg:       cfg,
		rsExec:    rsExec,
		myExec:    myExec,
		router:    rtr,
		cache:     cc,
		agent:     convertor_client.New(cfg.AgentURL),
		kb:        knowledge.New(cfg.AgentURL),
		authH:     authH,
		serverCfg: srvCfg,
	}
}

func (s *Server) Serve(ctx context.Context) error {
	ln, err := net.Listen("tcp", s.cfg.ListenAddr)
	if err != nil {
		return err
	}
	log.Printf("[server] MySQL proxy listening on %s (user=%s)", s.cfg.ListenAddr, s.cfg.MySQLUser)

	// accept loop
	for {
		select {
		case <-ctx.Done():
			ln.Close()
			return nil
		default:
		}
		conn, err := ln.Accept()
		if err != nil {
			if strings.Contains(err.Error(), "use of closed network") {
				return nil
			}
			log.Printf("[server] accept error: %v", err)
			continue
		}
		go s.handleConn(ctx, conn)
	}
}

func (s *Server) handleConn(ctx context.Context, nc net.Conn) {
	defer nc.Close()

	// Build a per-connection handler
	h := &connHandler{srv: s, ctx: ctx}

	// Use our server config + in-memory auth handler + our query handler.
	c, err := mysqlsrv.NewCustomizedConn(nc, s.serverCfg, s.authH, h)
	if err != nil {
		log.Printf("[server] handshake failed: %v", err)
		return
	}
	log.Printf("[server] client %s connected", nc.RemoteAddr())
	for {
		if err := c.HandleCommand(); err != nil {
			if !strings.Contains(err.Error(), "EOF") {
				log.Printf("[server] conn %s closed: %v", nc.RemoteAddr(), err)
			}
			return
		}
	}
}

// connHandler 实现 go-mysql-org/server.Handler 接口
type connHandler struct {
	srv *Server
	ctx context.Context
}

func (h *connHandler) UseDB(dbName string) error { return nil }
func (h *connHandler) HandleQuery(query string) (*gomysql.Result, error) {
	log.Printf("[query] %s", trunc(query, 200))

	// 1. "SET ...", "SHOW ...", "BEGIN", "USE ..." 这种管理命令忽略（返回空结果）
	if shouldNoop(query) {
		return emptyResult(), nil
	}

	// 2. Route — table whitelist decides MySQL vs Redshift.
	dest, tables, reason := h.srv.router.Route(query)
	log.Printf("[route] -> %s tables=%v (%s)", dest, tables, reason)

	if dest == router.DestMySQL {
		if h.srv.myExec == nil {
			return nil, fmt.Errorf("router selected MySQL but MYSQL_DSN not configured (tables=%v)", tables)
		}
		ctx, cancel := context.WithTimeout(h.ctx, 60*time.Second)
		defer cancel()
		result, err := h.srv.myExec.ExecSelect(ctx, query)
		if err != nil {
			return nil, fmt.Errorf("mysql exec: %w", err)
		}
		return resultmap.ToMySQL(result)
	}

	// 3. Redshift path: cache → translate → exec with retry
	key := cache.Key(query)
	rsQuery, ok := h.srv.cache.Get(key)
	if ok {
		cache.IncHit()
		log.Printf("[cache] HIT key=%s", key[:8])
	} else {
		cache.IncMiss()
		resp, err := h.srv.agent.Translate(h.ctx, convertor_client.TranslateRequest{SQL: query})
		if err != nil {
			return nil, fmt.Errorf("translate failed: %w", err)
		}
		log.Printf("[translate] agent ms=%d attempt=%s rules=%v",
			resp.LatencyMs, resp.Attempt, resp.UsedRules)
		rsQuery = resp.RedshiftSQL
		h.srv.cache.Set(key, rsQuery)
	}

	result, winningSQL, rsMs, err := h.execWithRetry(query, rsQuery)
	if err != nil {
		return nil, err
	}
	// 缓存更新为真正执行成功的 SQL（可能是 retry 修正过的）
	if winningSQL != rsQuery {
		h.srv.cache.Set(key, winningSQL)
	}
	// 异步回写知识库（fire-and-forget，不阻塞响应）
	if h.srv.kb != nil && len(result.Rows) > 0 && len(winningSQL) < 8000 {
		h.srv.kb.SaveAsync(knowledge.SaveReq{
			MySQLSQL:    query,
			RedshiftSQL: winningSQL,
			RowCount:    int64(len(result.Rows)),
			RedshiftMs:  rsMs,
			CompareMode: "skipped", // Proxy 运行态不做双边对比（已被 Redshift 成功返回即可）
			Source:      "runtime",
		})
	}
	return resultmap.ToMySQL(result)
}

// execWithRetry 返回 (result, winningSQL, elapsedMs, err)。
// winningSQL 是真正跑通的那版 SQL（可能是初始翻译，也可能是修正后的）。
func (h *connHandler) execWithRetry(originalSQL, rsSQL string) (*executor.Result, string, int, error) {
	ctx, cancel := context.WithTimeout(h.ctx, 60*time.Second)
	defer cancel()

	var prevErr error
	curSQL := rsSQL
	for attempt := 0; attempt < h.srv.cfg.MaxAttempts; attempt++ {
		started := time.Now()
		result, err := h.srv.rsExec.ExecSelect(ctx, curSQL)
		if err == nil {
			return result, curSQL, int(time.Since(started).Milliseconds()), nil
		}
		log.Printf("[redshift] attempt %d failed: %v", attempt, err)
		prevErr = err
		if attempt+1 >= h.srv.cfg.MaxAttempts {
			break
		}
		// 回喂 agent 修正
		fixResp, fixErr := h.srv.agent.Translate(h.ctx, convertor_client.TranslateRequest{
			SQL:       originalSQL,
			PrevError: err.Error(),
			PrevSQL:   curSQL,
		})
		if fixErr != nil {
			log.Printf("[translate-fix] agent failed: %v", fixErr)
			break
		}
		log.Printf("[translate-fix] new sql=%s", trunc(fixResp.RedshiftSQL, 200))
		curSQL = fixResp.RedshiftSQL
	}
	return nil, "", 0, fmt.Errorf("redshift exec after %d attempts: %w", h.srv.cfg.MaxAttempts, prevErr)
}

// The rest: COM_STMT_* and others — proxy returns basic support
func (h *connHandler) HandleFieldList(table string, fieldWildcard string) ([]*gomysql.Field, error) {
	return nil, nil
}
func (h *connHandler) HandleStmtPrepare(query string) (int, int, any, error) {
	return 0, 0, nil, fmt.Errorf("prepared statements not supported")
}
func (h *connHandler) HandleStmtExecute(ctx any, query string, args []any) (*gomysql.Result, error) {
	return nil, fmt.Errorf("prepared statements not supported")
}
func (h *connHandler) HandleStmtClose(ctx any) error { return nil }
func (h *connHandler) HandleOtherCommand(cmd byte, data []byte) error {
	return gomysql.NewError(gomysql.ER_UNKNOWN_ERROR, fmt.Sprintf("unsupported command: %d", cmd))
}

// ---- helpers ----
func trunc(s string, n int) string {
	if len(s) > n {
		return s[:n] + "…"
	}
	return s
}

func shouldNoop(q string) bool {
	t := strings.ToLower(strings.TrimSpace(q))
	for _, p := range []string{"set ", "start transaction", "begin", "commit", "rollback", "use ", "show "} {
		if strings.HasPrefix(t, p) {
			return true
		}
	}
	return false
}

func emptyResult() *gomysql.Result {
	return &gomysql.Result{Status: 0, Warnings: 0, InsertId: 0, AffectedRows: 0}
}
