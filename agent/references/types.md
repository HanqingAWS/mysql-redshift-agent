# 规则：数据类型差异

Redshift 基于 PostgreSQL，数据类型系统跟 MySQL 有若干差异。

## 数值类型

| MySQL | Redshift | 说明 |
|---|---|---|
| TINYINT | SMALLINT | Redshift 无 TINYINT（最小 SMALLINT = INT2） |
| SMALLINT | SMALLINT | 同 |
| MEDIUMINT | INTEGER | Redshift 无 MEDIUMINT |
| INT / INTEGER | INTEGER | 同 |
| BIGINT | BIGINT | 同 |
| DECIMAL(p,s) | DECIMAL(p,s) | 同（精度上限 Redshift 是 38，MySQL 是 65） |
| FLOAT | REAL (FLOAT4) | |
| DOUBLE | DOUBLE PRECISION (FLOAT8) | |

## 字符串类型

| MySQL | Redshift | 说明 |
|---|---|---|
| CHAR(n) | CHAR(n) | 同 |
| VARCHAR(n) | VARCHAR(n) | Redshift n 是字节数（UTF-8 汉字占 3 字节），MySQL 是字符数 |
| TEXT | VARCHAR(65535) | Redshift 无 TEXT |
| MEDIUMTEXT / LONGTEXT | VARCHAR(65535) | 同上，超出 65535 字节需自行截断 |

## 日期时间

| MySQL | Redshift | 说明 |
|---|---|---|
| DATE | DATE | 同 |
| DATETIME | TIMESTAMP | Redshift 无 DATETIME 类型 |
| TIMESTAMP | TIMESTAMP | MySQL TIMESTAMP 自带时区逻辑，Redshift 的 TIMESTAMP 默认无时区；要时区用 TIMESTAMPTZ |
| TIME | TIME | 同 |
| YEAR | SMALLINT | Redshift 无 YEAR |

## 布尔

| MySQL | Redshift |
|---|---|
| TINYINT(1) / BOOL | BOOLEAN |

## 其他

| MySQL | Redshift |
|---|---|
| JSON | SUPER（Redshift 自研半结构化类型，兼容 JSON） |
| BLOB / VARBINARY | VARBYTE（最长 1024000） |
| ENUM('a','b') | VARCHAR(n) + CHECK 约束；或直接 VARCHAR，业务层校验 |
| SET | 不支持；改用 VARCHAR + 分隔符 |
