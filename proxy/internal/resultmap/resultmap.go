// Package resultmap：把 Redshift(pgx) 结果转成 MySQL wire 的 Result 结构
package resultmap

import (
	"fmt"
	"strconv"
	"time"

	gomysql "github.com/go-mysql-org/go-mysql/mysql"

	"github.com/HanqingAWS/mysql-redshift-agent/proxy/internal/executor"
)

// ToMySQL converts an executor.Result into a MySQL-wire Result.
// 简化实现：所有列都以字符串形式返回（FIELD_TYPE_VAR_STRING），避免类型映射踩坑。
// MySQL 客户端（命令行/JDBC/Python）都能正确处理 string 结果并自行转换。
func ToMySQL(r *executor.Result) (*gomysql.Result, error) {
	if r == nil {
		return &gomysql.Result{}, nil
	}
	// 用 BuildSimpleTextResultset 构造一个文本结果集
	rows := make([][]any, 0, len(r.Rows))
	for _, raw := range r.Rows {
		row := make([]any, len(raw))
		for i, v := range raw {
			row[i] = anyToString(v)
		}
		rows = append(rows, row)
	}
	rs, err := gomysql.BuildSimpleTextResultset(r.Columns, rows)
	if err != nil {
		return nil, err
	}
	return &gomysql.Result{Resultset: rs}, nil
}

func anyToString(v any) any {
	if v == nil {
		return nil
	}
	switch x := v.(type) {
	case []byte:
		return string(x)
	case string:
		return x
	case time.Time:
		return x.Format("2006-01-02 15:04:05")
	case int, int8, int16, int32, int64:
		return fmt.Sprintf("%d", x)
	case uint, uint8, uint16, uint32, uint64:
		return fmt.Sprintf("%d", x)
	case float32:
		return strconv.FormatFloat(float64(x), 'f', -1, 32)
	case float64:
		return strconv.FormatFloat(x, 'f', -1, 64)
	case bool:
		if x {
			return "1"
		}
		return "0"
	default:
		return fmt.Sprintf("%v", x)
	}
}
