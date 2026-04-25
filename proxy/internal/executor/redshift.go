// Package executor 对 Redshift 执行 SQL 并返回结果集
package executor

import (
	"context"
	"database/sql"
	"fmt"
	"log"

	_ "github.com/jackc/pgx/v5/stdlib" // postgres driver
)

type Redshift struct {
	db *sql.DB
}

func NewRedshift(dsn string) (*Redshift, error) {
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		return nil, fmt.Errorf("sql.Open: %w", err)
	}
	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(2)
	if err := db.PingContext(context.Background()); err != nil {
		return nil, fmt.Errorf("redshift ping: %w", err)
	}
	log.Printf("[executor] Redshift connected")
	return &Redshift{db: db}, nil
}

func (r *Redshift) Close() error {
	return r.db.Close()
}

// Result 抽象：列名 + 值矩阵
type Result struct {
	Columns []string
	Rows    [][]any
}

// ExecSelect runs a query and returns columns + rows (all materialized).
// For demo, streaming is not yet implemented.
func (r *Redshift) ExecSelect(ctx context.Context, query string) (*Result, error) {
	rows, err := r.db.QueryContext(ctx, query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	cols, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	out := &Result{Columns: cols}
	for rows.Next() {
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return nil, err
		}
		out.Rows = append(out.Rows, vals)
	}
	return out, rows.Err()
}
