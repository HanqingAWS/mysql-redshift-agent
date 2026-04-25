// Package cache 提供 SQL 翻译结果的 LRU 缓存
// 设计：
//   - Key: md5(normalize(sql)) —— normalize 把字面量参数化以提升命中率
//   - 缓存只记 Redshift SQL 文本；不记参数（参数替换由上层处理）
package cache

import (
	"crypto/md5"
	"encoding/hex"
	"regexp"
	"strings"
	"sync"

	lru "github.com/hashicorp/golang-lru/v2"
)

type Cache struct {
	inner *lru.Cache[string, string]
}

func New(size int) (*Cache, error) {
	c, err := lru.New[string, string](size)
	if err != nil {
		return nil, err
	}
	return &Cache{inner: c}, nil
}

// Key 将 SQL 标准化后 hash。标准化规则：
//   - 小写化
//   - 压缩空白
//   - 字面量替换为占位符（简化版：整数、浮点、字符串）
func Key(sql string) string {
	n := normalize(sql)
	sum := md5.Sum([]byte(n))
	return hex.EncodeToString(sum[:])
}

var (
	reStringLit = regexp.MustCompile(`'(?:[^']|'')*'`)
	reNumber    = regexp.MustCompile(`\b\d+(\.\d+)?\b`)
	reWhitespc  = regexp.MustCompile(`\s+`)
)

func normalize(sql string) string {
	s := strings.TrimSpace(sql)
	s = strings.TrimRight(s, ";")
	s = strings.ToLower(s)
	s = reStringLit.ReplaceAllString(s, "?")
	s = reNumber.ReplaceAllString(s, "?")
	s = reWhitespc.ReplaceAllString(s, " ")
	return s
}

func (c *Cache) Get(key string) (string, bool) {
	return c.inner.Get(key)
}

func (c *Cache) Set(key, val string) {
	c.inner.Add(key, val)
}

func (c *Cache) Len() int {
	return c.inner.Len()
}

// Stats 返回命中 / 未命中计数（调用方自己维护更准确；此处用内部锁保护）
var stats struct {
	sync.Mutex
	hit, miss int64
}

func IncHit()  { stats.Lock(); stats.hit++; stats.Unlock() }
func IncMiss() { stats.Lock(); stats.miss++; stats.Unlock() }
func Stats() (int64, int64) {
	stats.Lock()
	defer stats.Unlock()
	return stats.hit, stats.miss
}
