# 规则：LIMIT 语法差异

## MySQL 支持两种写法

```sql
SELECT * FROM t LIMIT 10              -- 取前 10 行
SELECT * FROM t LIMIT 20, 10          -- 跳过 20 取 10
SELECT * FROM t LIMIT 10 OFFSET 20    -- 等价于上面
```

## Redshift 只支持

```sql
SELECT * FROM t LIMIT 10
SELECT * FROM t LIMIT 10 OFFSET 20
```

**不支持** `LIMIT a, b` 逗号语法。

## 翻译规则

把 `LIMIT <offset>, <count>` 改写为 `LIMIT <count> OFFSET <offset>`：

```sql
-- MySQL
SELECT * FROM t LIMIT 100, 50

-- Redshift
SELECT * FROM t LIMIT 50 OFFSET 100
```

`LIMIT N OFFSET M` 的形式两边都支持，不需要改。
