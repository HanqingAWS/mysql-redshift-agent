# 规则：UNIX_TIMESTAMP / FROM_UNIXTIME

## MySQL
```sql
SELECT UNIX_TIMESTAMP()                        -- 当前秒级时间戳
SELECT UNIX_TIMESTAMP('2025-09-13 02:50:00')
SELECT FROM_UNIXTIME(1757732053)               -- 秒 → datetime
SELECT FROM_UNIXTIME(1757732053, '%Y-%m-%d')
```

## Redshift
Redshift 没有直接等价函数，用 Epoch 算术实现：

```sql
-- 当前秒级时间戳
SELECT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)::BIGINT

-- 字符串转秒级时间戳
SELECT EXTRACT(EPOCH FROM TO_TIMESTAMP('2025-09-13 02:50:00','YYYY-MM-DD HH24:MI:SS'))::BIGINT

-- 秒 → timestamp
SELECT TIMESTAMP 'epoch' + 1757732053 * INTERVAL '1 second'

-- 秒 → 格式化字符串
SELECT TO_CHAR(TIMESTAMP 'epoch' + 1757732053 * INTERVAL '1 second', 'YYYY-MM-DD')
```

## 注意：毫秒级

MySQL 的 `UNIX_TIMESTAMP()` 也可返回毫秒（MySQL 5.6.4+ 的小数位）；
Redshift 需要手动乘除 1000：
```sql
-- 毫秒 → timestamp
SELECT TIMESTAMP 'epoch' + (ms / 1000.0) * INTERVAL '1 second'
```

本项目 `ts` 列是**毫秒**时间戳，翻译时注意除以 1000。
