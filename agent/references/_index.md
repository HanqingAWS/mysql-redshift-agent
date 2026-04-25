# MySQL → Redshift 方言差异索引

Agent 在翻译 SQL 时按关键词匹配，匹配到就把对应 .md 内容注入 prompt。

## 关键词 → 文件

| 检测关键词（正则，大小写不敏感） | 引用文件 |
|---|---|
| `\bdw\.` | schema_prefix.md |
| `` ` `` （反引号） | backticks.md |
| `\blimit\s+\d+\s*,\s*\d+` | limit_offset.md |
| `\bIFNULL\s*\(` | ifnull.md |
| `\bGROUP_CONCAT\s*\(` | group_concat.md |
| `\bDATE_FORMAT\s*\(` | date_format.md |
| `\b(DATE_ADD\|DATE_SUB\|ADDDATE\|SUBDATE)\s*\(` | date_arith.md |
| `\bON\s+DUPLICATE\s+KEY` | on_duplicate_key.md |
| `\b(TINYINT\|MEDIUMTEXT\|DATETIME)\b` | types.md |
| `\bSTR_TO_DATE\s*\(` | str_to_date.md |
| `\bUNIX_TIMESTAMP\s*\(` | unix_timestamp.md |
| `\bCONCAT_WS\s*\(` | concat_ws.md |

## 总则（每次翻译都注入）

1. Redshift 基于 PostgreSQL 8.0，严格大小写敏感（标识符默认小写）
2. 反引号 `` ` `` 在 Redshift 里改为双引号 `"`；字符串用单引号 `'`
3. Redshift 不支持反向分号语法如 `LIMIT a,b`
4. Redshift DDL 不支持 `ENGINE=`、`KEY idx_xxx` 这些 MySQL 关键字
5. Redshift 不支持部分 MySQL 函数（GROUP_CONCAT、IFNULL、DATE_FORMAT 等），需改用 Redshift 等价函数
