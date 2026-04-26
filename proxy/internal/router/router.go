// Package router decides whether a SQL should run on MySQL or Redshift,
// based on an explicit table-name whitelist.
//
// Rule: if EVERY table referenced in the SQL is in the whitelist → Redshift;
// otherwise → MySQL (direct pass-through, no agent translation).
// Empty whitelist → always Redshift (backwards compatible with the original
// demo behavior).
package router

import (
	"strings"

	"github.com/pingcap/tidb/pkg/parser"
	"github.com/pingcap/tidb/pkg/parser/ast"
	_ "github.com/pingcap/tidb/pkg/parser/test_driver" // required: registers literal types
)

// Destination identifies which backend the SQL should run on.
type Destination int

const (
	DestRedshift Destination = iota
	DestMySQL
)

func (d Destination) String() string {
	if d == DestMySQL {
		return "mysql"
	}
	return "redshift"
}

// Router holds the whitelist and a parser instance.
type Router struct {
	whitelist map[string]struct{}
	p         *parser.Parser
}

// New builds a Router from a comma-separated whitelist.
// Table names are lowercased for case-insensitive matching (MySQL default on linux
// is case-sensitive, but users specify whitelists with intent so we normalize).
// Empty input → empty whitelist (routes everything to Redshift).
func New(csv string) *Router {
	m := map[string]struct{}{}
	for _, t := range strings.Split(csv, ",") {
		t = strings.TrimSpace(strings.ToLower(t))
		if t == "" {
			continue
		}
		m[t] = struct{}{}
	}
	return &Router{whitelist: m, p: parser.New()}
}

// Whitelist returns the raw set (for logging/debug).
func (r *Router) Whitelist() []string {
	out := make([]string, 0, len(r.whitelist))
	for t := range r.whitelist {
		out = append(out, t)
	}
	return out
}

// tableCollector walks the AST and records every TableName node.
type tableCollector struct{ tables map[string]struct{} }

func (t *tableCollector) Enter(n ast.Node) (ast.Node, bool) {
	if tn, ok := n.(*ast.TableName); ok {
		t.tables[tn.Name.L] = struct{}{}
	}
	return n, false
}
func (t *tableCollector) Leave(n ast.Node) (ast.Node, bool) { return n, true }

// ExtractTables parses sql and returns the set of referenced table names
// (lowercased, schema stripped). Parse errors yield (nil, err) and the caller
// should treat that as "cannot decide → default to Redshift" (fall back to the
// current agent translation path, which can report better errors to the user).
func (r *Router) ExtractTables(sql string) ([]string, error) {
	stmts, _, err := r.p.Parse(sql, "", "")
	if err != nil {
		return nil, err
	}
	c := &tableCollector{tables: map[string]struct{}{}}
	for _, s := range stmts {
		s.Accept(c)
	}
	out := make([]string, 0, len(c.tables))
	for t := range c.tables {
		out = append(out, t)
	}
	return out, nil
}

// Route decides MySQL vs Redshift. Rules:
//   - empty whitelist → Redshift (demo/legacy mode)
//   - parse failure   → Redshift (let the agent+Redshift pipeline surface the error)
//   - at least one referenced table NOT in whitelist → MySQL
//   - all referenced tables in whitelist → Redshift
//
// Returns (dest, tablesReferenced, reason).
func (r *Router) Route(sql string) (Destination, []string, string) {
	if len(r.whitelist) == 0 {
		return DestRedshift, nil, "whitelist empty (all -> redshift)"
	}
	tables, err := r.ExtractTables(sql)
	if err != nil {
		return DestRedshift, nil, "parse failed, falling back to redshift: " + err.Error()
	}
	if len(tables) == 0 {
		// SELECT 1, SHOW, SET ... — no table. Stay on Redshift (same as today).
		return DestRedshift, tables, "no table referenced"
	}
	for _, t := range tables {
		if _, ok := r.whitelist[t]; !ok {
			return DestMySQL, tables, "table '" + t + "' not in whitelist -> mysql"
		}
	}
	return DestRedshift, tables, "all tables in whitelist -> redshift"
}
