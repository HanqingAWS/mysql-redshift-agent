# 规则：IFNULL → COALESCE

## MySQL
```sql
SELECT IFNULL(nick_name, 'anon') FROM users
SELECT IFNULL(amount, 0) * IFNULL(rate, 1) FROM orders
```

## Redshift
Redshift 不支持 `IFNULL`，用 SQL 标准的 `COALESCE`：
```sql
SELECT COALESCE(nick_name, 'anon') FROM users
SELECT COALESCE(amount, 0) * COALESCE(rate, 1) FROM orders
```

`COALESCE` 功能更强（接受任意多个参数，返回第一个非 NULL），`IFNULL` 只有两个参数。
一对一替换即可。

相关函数：
- `IF(cond, a, b)` (MySQL) → `CASE WHEN cond THEN a ELSE b END` (Redshift)
- `NVL(a, b)` — Redshift 也支持（Oracle 历史遗留），和 COALESCE 两参数等价
