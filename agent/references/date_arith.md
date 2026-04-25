# 规则：DATE_ADD / DATE_SUB → INTERVAL 算术

## MySQL
```sql
SELECT DATE_ADD(event_time, INTERVAL 7 DAY)
SELECT DATE_SUB(NOW(), INTERVAL 1 HOUR)
SELECT DATE_ADD('2025-01-01', INTERVAL 3 MONTH)
```

## Redshift
Redshift 支持 `INTERVAL` 算术，也支持 `DATEADD` / `DATE_ADD`：

```sql
SELECT event_time + INTERVAL '7 days'
SELECT CURRENT_TIMESTAMP - INTERVAL '1 hour'
SELECT DATE '2025-01-01' + INTERVAL '3 months'

-- 或者用 Redshift 的 DATEADD 函数（推荐，更显式）
SELECT DATEADD(day, 7, event_time)
SELECT DATEADD(hour, -1, CURRENT_TIMESTAMP)
SELECT DATEADD(month, 3, DATE '2025-01-01')
```

## 重要差异

- **引号**：Redshift INTERVAL 表达式要用**字符串**（`INTERVAL '7 days'`），不是裸的 `INTERVAL 7 DAY`
- **复数**：Redshift 习惯 `'7 days'`（复数），MySQL 是 `DAY`（单数）
- `NOW()` 在 Redshift 可用（返回事务开始时间，等价于 `TRANSACTION_TIMESTAMP()`）
- `CURDATE()` 在 Redshift **不支持**，改用 `CURRENT_DATE`
- `UNIX_TIMESTAMP()` 两边函数不一样，见 `unix_timestamp.md`
