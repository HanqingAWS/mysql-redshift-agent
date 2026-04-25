# 规则：DATE_FORMAT → TO_CHAR（格式符要转）

## MySQL
```sql
SELECT DATE_FORMAT(event_time, '%Y-%m-%d %H:%i:%s') FROM t
SELECT DATE_FORMAT(event_time, '%Y-%m') AS month FROM t
```

## Redshift
```sql
SELECT TO_CHAR(event_time, 'YYYY-MM-DD HH24:MI:SS') FROM t
SELECT TO_CHAR(event_time, 'YYYY-MM') AS month FROM t
```

## 格式符转换表

| MySQL | Redshift | 含义 |
|---|---|---|
| `%Y` | `YYYY` | 4 位年 |
| `%y` | `YY` | 2 位年 |
| `%m` | `MM` | 月 01-12 |
| `%c` | `FMMM` | 月 1-12（无前导零） |
| `%d` | `DD` | 日 01-31 |
| `%e` | `FMDD` | 日 1-31（无前导零） |
| `%H` | `HH24` | 时 00-23 |
| `%h` / `%I` | `HH12` | 时 01-12 |
| `%i` | `MI` | 分 00-59 |
| `%s` / `%S` | `SS` | 秒 00-59 |
| `%p` | `AM`/`PM` | 上下午 |
| `%W` | `Day` | 星期几全称 |
| `%a` | `Dy` | 星期几缩写 |
| `%M` | `Month` | 月份全称 |
| `%b` | `Mon` | 月份缩写 |
| `%j` | `DDD` | 一年第几天 |

⚠️ Redshift 的 TO_CHAR 会对字符串做填充，如果不要前导零要加 `FM` 前缀。
