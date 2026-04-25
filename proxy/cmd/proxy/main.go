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
	"github.com/HanqingAWS/mysql-redshift-agent/proxy/internal/server"
)

func main() {
	cfg := config.FromEnv()
	log.Printf("[proxy] starting with cfg=%+v", cfg.Redacted())

	exec, err := executor.NewRedshift(cfg.RedshiftDSN)
	if err != nil {
		log.Fatalf("redshift connect failed: %v", err)
	}
	defer exec.Close()

	srv := server.New(cfg, exec)

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
