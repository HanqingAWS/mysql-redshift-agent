// Package convertor_client：HTTP 客户端，调用 db-convertor-agent 的 /translate 接口
package convertor_client

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

type Client struct {
	baseURL string
	http    *http.Client
}

func New(baseURL string) *Client {
	return &Client{
		baseURL: baseURL,
		http:    &http.Client{Timeout: 60 * time.Second},
	}
}

type TranslateRequest struct {
	SQL       string `json:"sql"`
	PrevError string `json:"prev_error,omitempty"`
	PrevSQL   string `json:"prev_sql,omitempty"`
}

type TranslateResponse struct {
	RedshiftSQL string   `json:"redshift_sql"`
	UsedRules   []string `json:"used_rules"`
	LatencyMs   int      `json:"latency_ms"`
	Attempt     string   `json:"attempt"`
}

func (c *Client) Translate(ctx context.Context, req TranslateRequest) (*TranslateResponse, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return nil, err
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/translate", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("http call failed: %w", err)
	}
	defer resp.Body.Close()

	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("agent returned %d: %s", resp.StatusCode, string(data))
	}
	var out TranslateResponse
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("decode agent response: %w; body=%s", err, string(data))
	}
	return &out, nil
}
