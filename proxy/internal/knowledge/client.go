// Package knowledge 在 Proxy 这头把成功执行的 MySQL→Redshift 翻译异步回写到 agent 的知识库。
package knowledge

import (
	"bytes"
	"context"
	"encoding/json"
	"log"
	"net/http"
	"time"
)

type Client struct {
	baseURL string
	http    *http.Client
	ch      chan SaveReq
}

type SaveReq struct {
	MySQLSQL    string   `json:"mysql_sql"`
	RedshiftSQL string   `json:"redshift_sql"`
	UsedRules   []string `json:"used_rules,omitempty"`
	RowCount    int64    `json:"row_count,omitempty"`
	MySQLMs     int      `json:"mysql_ms,omitempty"`
	RedshiftMs  int      `json:"redshift_ms,omitempty"`
	CompareMode string   `json:"compare_mode,omitempty"`
	Source      string   `json:"source,omitempty"`
}

func New(baseURL string) *Client {
	c := &Client{
		baseURL: baseURL,
		http:    &http.Client{Timeout: 10 * time.Second},
		ch:      make(chan SaveReq, 256),
	}
	go c.worker()
	return c
}

// SaveAsync 把请求丢进 channel，由 worker 异步提交，永不阻塞调用方。
func (c *Client) SaveAsync(req SaveReq) {
	select {
	case c.ch <- req:
	default:
		log.Printf("[knowledge] channel full, drop save for sql=%.80q", req.MySQLSQL)
	}
}

func (c *Client) worker() {
	for req := range c.ch {
		if err := c.save(req); err != nil {
			log.Printf("[knowledge] save failed: %v", err)
		}
	}
}

func (c *Client) save(req SaveReq) error {
	body, err := json.Marshal(req)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	httpReq, err := http.NewRequestWithContext(ctx, "POST", c.baseURL+"/save_example", bytes.NewReader(body))
	if err != nil {
		return err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(httpReq)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return nil // ignore non-2xx, don't flood logs
	}
	return nil
}
