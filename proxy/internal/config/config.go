package config

import (
	"fmt"
	"os"
	"strconv"
)

// Config 保存 proxy 全部运行参数
type Config struct {
	// Listen
	ListenAddr string // ":3306"

	// Auth (MySQL clients connect with these)
	MySQLUser     string
	MySQLPassword string
	MySQLDBName   string // 虚拟 DB 名，所有客户端看到的都是这个

	// Downstream Redshift
	RedshiftDSN string // postgres://user:pass@host:5439/db?sslmode=require

	// Downstream MySQL (direct pass-through for non-whitelisted tables).
	// Empty → MySQL routing disabled, all SQL goes to Redshift (original demo behavior).
	MySQLDSN string // user:pass@tcp(host:3306)/db?parseTime=true

	// Comma-separated list of tables that SHOULD be served from Redshift.
	// A query is routed to Redshift iff ALL of its referenced tables are in
	// this list. Empty list = always Redshift (backwards compatible).
	TableWhitelist string

	// Agent
	AgentURL string // http://agent:8088

	// Cache
	CacheSize int

	// Translation retry
	MaxAttempts int
}

// FromEnv loads from environment with sensible defaults for demo.
func FromEnv() Config {
	return Config{
		ListenAddr:    getenv("PROXY_LISTEN", ":3306"),
		MySQLUser:     getenv("PROXY_MYSQL_USER", "demo"),
		MySQLPassword: mustEnv("PROXY_MYSQL_PASSWORD"),
		MySQLDBName:   getenv("PROXY_MYSQL_DB", "dw"),
		RedshiftDSN:    mustEnv("REDSHIFT_DSN"),
		MySQLDSN:       getenv("MYSQL_DSN", ""),
		TableWhitelist: getenv("TABLE_WHITELIST", ""),
		AgentURL:       getenv("AGENT_URL", "http://localhost:8088"),
		CacheSize:      mustAtoi(getenv("CACHE_SIZE", "1024")),
		MaxAttempts:    mustAtoi(getenv("MAX_ATTEMPTS", "3")),
	}
}

// Redacted returns a copy of Config with secrets masked, for logging.
func (c Config) Redacted() Config {
	c.MySQLPassword = "****"
	c.RedshiftDSN = "****"
	return c
}

func getenv(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func mustEnv(k string) string {
	v := os.Getenv(k)
	if v == "" {
		panic(fmt.Sprintf("missing required env %s", k))
	}
	return v
}

func mustAtoi(s string) int {
	n, err := strconv.Atoi(s)
	if err != nil {
		panic(fmt.Sprintf("invalid int env %q: %v", s, err))
	}
	return n
}
