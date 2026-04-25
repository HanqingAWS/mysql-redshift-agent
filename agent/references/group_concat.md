# 规则：GROUP_CONCAT → LISTAGG

## MySQL
```sql
SELECT game_name, GROUP_CONCAT(uid) FROM t GROUP BY game_name
SELECT game_name, GROUP_CONCAT(DISTINCT uid ORDER BY uid SEPARATOR '|') FROM t GROUP BY game_name
```

## Redshift
```sql
SELECT game_name, LISTAGG(uid, ',') WITHIN GROUP (ORDER BY uid) FROM t GROUP BY game_name
SELECT game_name, LISTAGG(DISTINCT uid, '|') WITHIN GROUP (ORDER BY uid) FROM t GROUP BY game_name
```

## 要点

- MySQL 默认分隔符是逗号，Redshift 的 `LISTAGG` 第二个参数强制必填（分隔符）
- ORDER BY 在 MySQL 里内嵌在函数里，在 Redshift 里用 `WITHIN GROUP (ORDER BY ...)` 语法
- DISTINCT 两边都支持写在参数前
- **限制**：Redshift `LISTAGG` 返回的字符串最长 65535 字节（VARCHAR(MAX)）；超长会报错
