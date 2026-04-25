# 规则：CONCAT_WS 支持情况

MySQL 和 Redshift **都支持** `CONCAT_WS(separator, a, b, ...)`，行为一致：
```sql
SELECT CONCAT_WS(',', uid, game_name, country_code)
```

不需要翻译。

## 相关：CONCAT

普通 `CONCAT(a, b, c, ...)` 两边也都支持，直接保留。
注意 MySQL 允许任意多个参数，Redshift 早期版本只接受 2 个，新版（最近 2 年）已放开多参数。
若碰到报错，可把 `CONCAT(a, b, c)` 改写成 `CONCAT(a, CONCAT(b, c))` 或者用 `||` 操作符：
```sql
SELECT a || b || c FROM t
```

## 反引号 + 字符串拼接常见坑

MySQL 的 `||` 默认是 OR 操作符（除非开了 `PIPES_AS_CONCAT` SQL mode）；
Redshift 的 `||` 是字符串拼接（SQL 标准）。

所以 MySQL 里 `a || b` 是"a OR b"，Redshift 里是"a 拼 b"。翻译时要特别注意。
