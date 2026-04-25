# 规则：去掉 `dw.` schema 前缀

本项目中，Aurora MySQL 的数据库名为 `dw`，客户端 SQL 中常写成：

```sql
SELECT * FROM dw.ads_thor_fin_payment_iap_data_new
```

而 Redshift 侧表建在默认 schema `public` 下，**不存在名为 `dw` 的 schema**。

## 翻译规则

把所有 `dw.` 前缀去掉：

```sql
-- MySQL
SELECT * FROM dw.ads_thor_fin_payment_iap_data_new WHERE uid='123'

-- Redshift
SELECT * FROM ads_thor_fin_payment_iap_data_new WHERE uid='123'
```

⚠️ 保留其他 schema（如果有）。只剥除 `dw.`。

## 反例

不要误删其他含 `dw` 的字符串：
```sql
-- 保留不动
SELECT game_name FROM ads_xxx WHERE game_name LIKE '%dw%'
```
