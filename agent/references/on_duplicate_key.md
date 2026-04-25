# 规则：ON DUPLICATE KEY UPDATE → MERGE

## MySQL
```sql
INSERT INTO users(id, name, cnt) VALUES(1, 'a', 1)
ON DUPLICATE KEY UPDATE cnt = cnt + 1
```

## Redshift
Redshift 不支持 `ON DUPLICATE KEY`。对应写法是 `MERGE INTO`（Redshift 2022+ 支持）：

```sql
MERGE INTO users AS tgt
USING (SELECT 1 AS id, 'a' AS name, 1 AS cnt) AS src
ON tgt.id = src.id
WHEN MATCHED THEN UPDATE SET cnt = tgt.cnt + 1
WHEN NOT MATCHED THEN INSERT (id, name, cnt) VALUES (src.id, src.name, src.cnt);
```

## 判断
- 若本项目**只读**（OLAP 场景），`INSERT ... ON DUPLICATE KEY` 类语句不会出现
- 若翻译器碰到这种 SQL，优先策略：**直接拒绝翻译**，返回错误告诉用户"Redshift 不支持 ON DUPLICATE KEY，请改用 MERGE INTO"，避免 LLM 自由发挥把业务语义写错
