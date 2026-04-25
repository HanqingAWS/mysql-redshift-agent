# 规则：STR_TO_DATE → TO_TIMESTAMP / TO_DATE

## MySQL
```sql
SELECT STR_TO_DATE('2025-09-13 02:50:00', '%Y-%m-%d %H:%i:%s')
SELECT STR_TO_DATE('13/09/2025', '%d/%m/%Y')
```

## Redshift
```sql
SELECT TO_TIMESTAMP('2025-09-13 02:50:00', 'YYYY-MM-DD HH24:MI:SS')
SELECT TO_DATE('13/09/2025', 'DD/MM/YYYY')
```

格式符转换见 `date_format.md`。

字符串到时间戳：
- 想要 DATE 类型用 `TO_DATE`
- 想要 TIMESTAMP 用 `TO_TIMESTAMP`
