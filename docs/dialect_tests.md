# 方言测试用例（S8）

脚本：`scripts/dialect_tests.sh`
运行：`./scripts/dialect_tests.sh`（需先起好 docker-compose）

| # | 用例 | MySQL 特性 | Proxy/Agent 处理 | 状态 |
|---|------|-----------|------------------|------|
| 1 | `SELECT COUNT(*) FROM dw.ads_thor_fin_payment_iap_data_new` | `dw.` 前缀 | 剥前缀 → `FROM ads_thor_fin_payment_iap_data_new` | ✅ 返回 100 |
| 2 | `` SELECT `uid`, `amount` FROM ... `` | 反引号标识符 | `` ` `` → `"` | ✅ 返回 3 行 |
| 3 | `... LIMIT 2, 3` | MySQL 两参 LIMIT | → `LIMIT 3 OFFSET 2` | ✅ 返回 3 行 |
| 4 | `IFNULL(app_version, 'unknown')` | IFNULL | → `COALESCE(app_version, 'unknown')` | ✅ 返回 3 行 |
| 5 | `DATE_FORMAT(event_time, '%Y-%m-%d')` | 日期格式化 | → `TO_CHAR(event_time, 'YYYY-MM-DD')` | ✅ 按日分组 |
| 6 | `GROUP_CONCAT(DISTINCT app_id)` | 聚合拼接 | → `LISTAGG(DISTINCT app_id, ',') WITHIN GROUP (ORDER BY app_id)` | ✅ 返回 apps 字符串 |
| 7 | `DATE_SUB('2025-09-20', INTERVAL 3 DAY)` | 日期运算 | → `DATEADD(day, -3, '2025-09-20'::timestamp)`（Redshift 等价） | ✅ SQL 成功（0 行符合过滤） |
| 8 | `UNIX_TIMESTAMP(event_time)` | 转 epoch 秒 | → `EXTRACT(EPOCH FROM event_time)` | ✅ 返回 unix 整数 |
| 9 | `CONCAT_WS('-', uid, transaction_id)` | 带分隔符拼接 | 保持不变（Redshift 同名可用） | ✅ 拼接正确 |
| 10 | 用户样例（WHERE + IN + ORDER BY + LIMIT OFFSET） | 综合 | 剥前缀；IN 列表保留 | ✅ 返回 5 行 |

**结论**：Sonnet 4.6 + Markdown reference pre-filter 可以覆盖 IAP/BI 场景下的常见方言差异。
Agent 在调用 `get_table_schema` tool 后曾出现多余 prose；`agent/app.py::extract_sql()` 用正则从
`SELECT|WITH|INSERT|...` 关键字起截断，彻底解决。
