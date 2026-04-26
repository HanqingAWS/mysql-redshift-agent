// MySQL direct executor — used by the router when a query references
// a table not in the whitelist. Uses go-sql-driver/mysql.
package executor

import (
	"context"
	"database/sql"
	"fmt"
	"log"

	_ "github.com/go-sql-driver/mysql"
)

type MySQL struct {
	db *sql.DB
}

func NewMySQL(dsn string) (*MySQL, error) {
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		return nil, fmt.Errorf("sql.Open mysql: %w", err)
	}
	db.SetMaxOpenConns(10)
	db.SetMaxIdleConns(2)
	if err := db.PingContext(context.Background()); err != nil {
		return nil, fmt.Errorf("mysql ping: %w", err)
	}
	log.Printf("[executor] MySQL connected")
	return &MySQL{db: db}, nil
}

func (m *MySQL) Close() error {
	return m.db.Close()
}

// ExecSelect runs a query against MySQL and returns columns + rows.
// Same Result shape as Redshift executor so resultmap.ToMySQL() can reuse.
func (m *MySQL) ExecSelect(ctx context.Context, query string) (*Result, error) {
	rows, err := m.db.QueryContext(ctx, query)
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
