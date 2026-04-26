// Package main: MySQL wire protocol 前端 + Redshift 后端的 Proxy 启动器
package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/HanqingAWS/mysql-redshift-agent/proxy/internal/config"
	"github.com/HanqingAWS/mysql-redshift-agent/proxy/internal/executor"
	"github.com/HanqingAWS/mysql-redshift-agent/proxy/internal/router"
	"github.com/HanqingAWS/mysql-redshift-agent/proxy/internal/server"
)

func main() {
	cfg := config.FromEnv()
	log.Printf("[proxy] starting with cfg=%+v", cfg.Redacted())

	rsExec, err := executor.NewRedshift(cfg.RedshiftDSN)
	if err != nil {
		log.Fatalf("redshift connect failed: %v", err)
	}
	defer rsExec.Close()

	var myExec *executor.MySQL
	if cfg.MySQLDSN != "" {
		myExec, err = executor.NewMySQL(cfg.MySQLDSN)
		if err != nil {
			log.Fatalf("mysql connect failed: %v", err)
		}
		defer myExec.Close()
	} else {
		log.Printf("[proxy] MYSQL_DSN empty -> MySQL routing disabled (all SQL goes to Redshift)")
	}

	rtr := router.New(cfg.TableWhitelist)
	log.Printf("[proxy] table whitelist: %v", rtr.Whitelist())

	srv := server.New(cfg, rsExec, myExec, rtr)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Start server in its own goroutine
	errCh := make(chan error, 1)
	go func() {
		errCh <- srv.Serve(ctx)
	}()

	// Handle SIGINT/SIGTERM
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	select {
	case sig := <-sigCh:
		log.Printf("[proxy] received signal %s, shutting down", sig)
		cancel()
	case err := <-errCh:
		if err != nil {
			log.Fatalf("server exited with error: %v", err)
		}
	}
}
