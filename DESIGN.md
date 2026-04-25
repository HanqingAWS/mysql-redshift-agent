# MySQL → Redshift LLM Proxy 设计文档

## 1. 背景

### 1.1 客户场景
- 客户现有业务系统大量使用 **MySQL**，代码里硬编码了 MySQL 语法（反引号、`LIMIT a,b`、`IFNULL`、`GROUP_CONCAT`、`DATE_FORMAT` 等）。
- 出于分析/成本/扩展性考虑，客户希望把查询后端迁移到 **AWS Redshift**（OLAP，PostgreSQL 兼容）。
- **核心诉求：客户端代码零改动或最小改动**，SQL 原样提交，由中间层负责把 MySQL 方言翻译成 Redshift 方言并返回结果。

### 1.2 为什么不能直接连
- Redshift 走 **PostgreSQL wire protocol**，MySQL 客户端走 **MySQL wire protocol**——协议不兼容。
- MySQL 和 Redshift 方言差异巨大（函数名、LIMIT 语法、引号、DDL、数据类型等），SQL 不能直接原样执行。
- 现有开源方案（ProxySQL/ShardingSphere 都只支持 MySQL 后端；AWS Zero-ETL 是复制不是代理；Heimdall Proxy 仍要求客户端用 Postgres 驱动）——没有"MySQL 客户端 → Redshift 后端"的开源直通方案。

### 1.3 本项目目标
构建一个代理服务，对客户端表现为 MySQL 服务器，对后端使用 Redshift，中间用 **LLM Agent 做 SQL 翻译与自修复**，实现客户"真正零代码改动"。

---

## 2. 关键技术判断

### 2.1 可行性已有先例
| 项目 | 证明的事 |
|---|---|
| StarRocks / Doris | "MySQL 协议端 + 非 MySQL 后端"架构成立 |
| Vitess / TiDB | MySQL wire protocol 解析/转发/结果回包的工程成熟 |
| Presto/Trino MySQL gateway | 客户端 MySQL → 任意后端的模式可用 |

### 2.2 为什么选择 LLM 做翻译（而非硬编码规则）
- 方言差异是**无底洞**——业务 SQL 每多用一个 MySQL 特性就要多写一条规则，维护成本持续上升。
- 本场景**延迟不敏感**（OLAP/BI/分析报表，秒级可接受），给 LLM 推理留足预算。
- **缓存命中后 LLM 不参与**，生产环境 90%+ 查询是重复的，实际 LLM 调用很稀疏。
- **错误可自修复**——Redshift 报错信息（如 `function group_concat does not exist`）足够让 LLM 自己改写 SQL 重试。

### 2.3 方言差异举例（会在翻译层处理）
```sql
-- 客户端写（MySQL）             →   Proxy 转成（Redshift）
SELECT * FROM t LIMIT 10, 20      →   SELECT * FROM t LIMIT 20 OFFSET 10
IFNULL(x, 0)                      →   COALESCE(x, 0)
GROUP_CONCAT(name)                →   LISTAGG(name, ',')
DATE_FORMAT(d, '%Y-%m')           →   TO_CHAR(d, 'YYYY-MM')
`column_name`                     →   "column_name"
ON DUPLICATE KEY UPDATE           →   (Redshift 不支持 → MERGE 或报错)
```

---

## 3. 架构设计

### 3.1 总体数据流

```
MySQL client
    │ MySQL wire protocol (TCP 3306)
    ▼
┌──────────────────────────────────────────┐
│ Proxy (Go)                               │
│                                          │
│  1. 接受 MySQL 握手/认证                  │
│  2. 解析 MySQL query 包                   │
│  3. 查缓存 (normalize → hash → 译文)      │  ──命中──┐
│  4. 未命中 → LLM 翻译                     │         │
│  5. 执行 Redshift query                  │ ◄───────┘
│  6. 失败 → LLM 带报错修正 → 重试 (≤3 次)   │
│  7. Postgres 结果集 → MySQL 结果集打包    │
│  8. 写缓存                                │
└──────────────────────────────────────────┘
    │ PostgreSQL wire protocol (TCP 5439)
    ▼
AWS Redshift
```

### 3.2 模块划分

| 模块 | 职责 | 技术选型 |
|---|---|---|
| `proxy/server` | MySQL wire protocol 服务端（握手/认证/查询包/结果包） | [`go-mysql-server`](https://github.com/dolthub/go-mysql-server)（Dolt 维护） |
| `proxy/translator` | MySQL SQL → Redshift SQL | Anthropic Claude API（Haiku 4.5 主用，Sonnet 4.6 兜底） |
| `proxy/cache` | 翻译结果缓存 | 进程内 LRU（MVP）→ Redis（生产） |
| `proxy/executor` | 连接 Redshift 并执行 | [`jackc/pgx`](https://github.com/jackc/pgx) |
| `proxy/resultmap` | Postgres 结果集 → MySQL 结果集（类型映射） | 自写，~200 行 |
| `proxy/schema` | 表结构缓存（喂给 LLM prompt） | 启动时 `information_schema` 拉一次，定期刷新 |
| `proxy/safety` | 安全护栏（只允许 SELECT、AST 校验） | [`sqlglot`](https://github.com/tobymao/sqlglot)（Python subprocess）或 Go vitess parser |

### 3.3 LLM Agent 伪代码

```go
func handleQuery(mysqlSQL string) (ResultSet, error) {
    normalized := normalize(mysqlSQL)  // WHERE id=123 → WHERE id=?

    // 1. 缓存查
    if cached, ok := cache.Get(normalized); ok {
        return redshift.Exec(bindParams(cached.redshiftSQL, mysqlSQL))
    }

    // 2. LLM 翻译（Haiku）
    redshiftSQL := haiku.Translate(mysqlSQL, schemaContext)

    // 3. 安全校验（只允许 SELECT）
    if !isSelectOnly(redshiftSQL) {
        return nil, ErrDisallowed
    }

    // 4. 执行 + 错误重试（升级到 Sonnet）
    result, err := redshift.Exec(redshiftSQL)
    for attempt := 0; err != nil && attempt < 3; attempt++ {
        redshiftSQL = sonnet.Fix(redshiftSQL, err.Error(), mysqlSQL, schemaContext)
        result, err = redshift.Exec(redshiftSQL)
    }
    if err != nil {
        return nil, err  // 3 次修复失败 → 抛给客户端
    }

    // 5. 写缓存（只缓存成功的）
    cache.Set(normalized, redshiftSQL)
    return result, nil
}
```

### 3.4 LLM Prompt 结构

```
System: 你是 MySQL → Redshift SQL 翻译器。严格输出可执行的单条 Redshift SQL，不要解释。
        Redshift 基于 PostgreSQL 8.0，支持窗口函数、CTE、LISTAGG，不支持 ON DUPLICATE KEY、
        GROUP_CONCAT、反引号。

Schema:
  TABLE orders (id BIGINT, user_id BIGINT, amount DECIMAL(10,2), created_at TIMESTAMP)
  TABLE users  (id BIGINT, name VARCHAR(64), email VARCHAR(128))

[错误修复模式时追加:]
Previous attempt failed:
  SQL: SELECT GROUP_CONCAT(name) FROM users
  Error: function group_concat does not exist
Fix the SQL.

User: {mysql_sql}
```

---

## 4. 关键设计决策

### 4.1 缓存（最重要）
- **Key**：`MD5(normalize(sql))`。normalize 负责把字面量参数化（`WHERE id=123` → `WHERE id=?`），大幅提升命中率。
- **Value**：翻译后的 SQL 模板 + 参数占位。
- 命中率目标 ≥ 90%——生产环境 BI/报表查询高度重复。
- MVP 用进程内 LRU，生产换 Redis 共享缓存。

### 4.2 两级模型策略
- 首次翻译走 **Haiku 4.5**（$1/$5 per MTok，足够处理 80% 常见 SQL）。
- Redshift 执行报错后，**升级到 Sonnet 4.6** 带错误信息修复（复杂场景推理更强）。
- 分级能把成本压在 Haiku 档位，同时保留复杂场景的正确率。

### 4.3 安全护栏
- **MVP 阶段只允许 SELECT**——DDL/DML 风险大（LLM 误译 `DROP`/`UPDATE` 损失不可逆）。
- LLM 输出用 sqlglot / vitess parser 解析 AST，顶层非 SELECT 直接拒绝。
- Schema 注入不要放敏感字段注释（防止 prompt 泄露业务信息）。

### 4.4 错误重试上限
- 最多 3 次 LLM 修复。超过 3 次仍失败 → 把原始 Redshift 报错包装成 MySQL 错误码返回给客户端。
- 避免死循环烧钱。

### 4.5 正确性验证（生产必做）
- **影子双跑**：关键查询同时发 MySQL + Redshift，结果集 diff，日常监控。
- LLM 翻译"能跑但语义不对"（如 `INNER JOIN` 译成 `LEFT JOIN`）是最大风险，必须有外部校验。

---

## 5. 成本估算

### 5.1 LLM 调用成本（假设缓存命中率 90%）
- 每次未命中的调用：≈ 500 tokens in + 200 tokens out
- Haiku 4.5 单次：≈ $0.0015
- 日均 100k query × 10% 未命中 = 10k 次 LLM 调用
- **Haiku 主力 ≈ $15/天 ≈ $450/月**
- 全量 Sonnet ≈ $2000/月（不推荐）

### 5.2 基础设施
- Proxy 服务：单实例 c7g.large（2 vCPU / 4 GB）即可，~$50/月
- Redis 缓存（可选）：cache.t4g.micro，~$15/月

---

## 6. 实施范围（MVP vs 生产）

### 6.1 MVP 范围（~800 行 Go）
- ✅ MySQL wire protocol 服务端（用 go-mysql-server 库）
- ✅ LLM 翻译（只 Haiku，不做升级兜底）
- ✅ 进程内 LRU 缓存
- ✅ Redshift 执行（pgx 直连）
- ✅ 基础类型映射（INT/VARCHAR/DECIMAL/TIMESTAMP）
- ✅ **只支持 SELECT**
- ❌ Prepared statements
- ❌ 事务
- ❌ 影子双跑

### 6.2 生产补齐
- Prepared statement 支持
- Redis 共享缓存 + 缓存预热
- Haiku → Sonnet 两级降级
- 影子双跑 + 结果 diff 监控
- 认证透传（Proxy 自己管用户 vs 透传到 Redshift）
- 连接池、限流、熔断
- 指标上报（翻译成功率、缓存命中率、LLM 调用量、P99 延迟）
- DDL/DML 支持（加人工审核闸门）

---

## 7. 风险与 Tradeoff

| 风险 | 缓解 |
|---|---|
| LLM 翻译语义错误（能跑但结果不对） | 影子双跑 + 结果 diff；核心查询走人工审核白名单 |
| 首次查询延迟 2-5s | 业务可接受（延迟不敏感）；可做启动时缓存预热 |
| LLM 成本不可控 | 缓存命中率监控；未命中率飙升触发告警；硬上限保护 |
| Prompt injection 注入恶意 SQL | AST 校验只允许 SELECT；LLM 输出必须是单条语句 |
| Redshift 不支持 MySQL 某些特性（如 ON DUPLICATE KEY） | 翻译失败直接报错，不静默吞掉；由业务显式改写或加白名单 |
| 客户端驱动差异（MySQL 8.0 caching_sha2_password） | go-mysql-server 已支持；必要时强制客户端用 `mysql_native_password` |

---

## 8. 下一步

1. 搭 MVP 骨架：Go proxy 能接 MySQL 客户端，写死一条 SQL 路由到 Redshift 并返回结果
2. 接入 Haiku 翻译：用少量真实业务 SQL 验证翻译准确率
3. 加缓存 + 错误重试
4. 选 10~20 条高频 SQL 做影子双跑，验证语义一致性
5. 根据准确率数据决定是否上 Sonnet 兜底、是否放开 DML

---

## 9. 项目规划（本次实施）

### 9.1 实施补充与约束（用户确认）
- **LLM 模型**：统一用 `us.anthropic.claude-sonnet-4-6`（Sonnet 4.6，通过 AWS Bedrock），不再分级；延迟不敏感场景优先正确率
- **源库**：**Aurora MySQL Serverless v2**（按需扩缩容）
- **目标库**：**Redshift Serverless**（按工作负载扩缩容）
- **测试数据**：schema 见 `table-schema.xlsx`（Sheet `Results`），真实表 `dw.ads_thor_fin_payment_iap_data_new` 的 100 行样本 + 24 列
- **冒烟 SQL**（来自用户）：
  ```sql
  select uid,fpid,transaction_id,new_app_id as app_id,base_price,amount,
         payment_processor,order_id, ts
  from dw.ads_thor_fin_payment_iap_data_new
  where event_time >= '2026-01-01 00:00:00'
    and event_time <= '2026-02-01 00:00:00'
    and game_name='ss' and uid='1111' and amount > 0
    and payment_processor not in ('centurygames_pay','centurygames_store',
                                  'centurygames','kg3rdpartypayment','pc')
  order by ts limit 1000 offset 0
  ```
  这条 SQL 在 MySQL/Redshift 语法基本一致（`LIMIT N OFFSET M` 两边都支持），但带库名前缀 `dw.xxx`——**需确认 Redshift 端用 schema `dw` 还是改成库名**。

### 9.2 目标表结构（从 xlsx 推导）

表：`dw.ads_thor_fin_payment_iap_data_new`（IAP 支付流水）

| 列 | MySQL 类型 | Redshift 类型 | 说明 |
|---|---|---|---|
| game_name | VARCHAR(64) | VARCHAR(64) | 游戏代号 |
| uid | VARCHAR(32) | VARCHAR(32) | 用户 ID |
| transaction_id | VARCHAR(64) | VARCHAR(64) | 交易号（如 `GPA.xxx`） |
| event_time | DATETIME | TIMESTAMP | 事件时间 |
| fpid | BIGINT | BIGINT | Funplus ID |
| app_id | VARCHAR(16) | VARCHAR(16) | 应用 ID |
| device_platform | VARCHAR(16) | VARCHAR(16) | android/ios |
| country_code | VARCHAR(8) | VARCHAR(8) | ISO 国家码 |
| gameserver_id | VARCHAR(16) | VARCHAR(16) | 游戏服 ID |
| app_language | VARCHAR(8) | VARCHAR(8) | 可空 |
| device_level | SMALLINT | SMALLINT | 设备档次 |
| city_level | SMALLINT | SMALLINT | 城市层级 |
| amount | DECIMAL(12,4) | DECIMAL(12,4) | 金额（美元等值） |
| is_white_user | TINYINT | SMALLINT | 白名单标记 |
| new_app_id | VARCHAR(16) | VARCHAR(16) | 新应用 ID |
| payment_processor | VARCHAR(32) | VARCHAR(32) | 支付渠道 |
| iap_product_id | VARCHAR(128) | VARCHAR(128) | 商品 ID |
| iap_product_name | VARCHAR(128) | VARCHAR(128) | 商品名 |
| base_price | VARCHAR(16) | VARCHAR(16) | 牌价（字符串，保留样本） |
| iap_product_name_cn | VARCHAR(128) | VARCHAR(128) | 中文商品名 |
| app_version | VARCHAR(32) | VARCHAR(32) | 版本号 |
| currency | VARCHAR(8) | VARCHAR(8) | ISO 货币码 |
| order_id | VARCHAR(64) | VARCHAR(64) | 订单号 |
| ts | BIGINT | BIGINT | 毫秒时间戳 |

> `base_price` 样本全是字符串（`"4.99"`），保持原类型以免翻译引入数值转换风险。
> Redshift 不支持 `TINYINT`，`is_white_user` 映射成 `SMALLINT`。
> Redshift 的 schema 需要提前 `CREATE SCHEMA dw`，否则客户端的 `dw.xxx` 表名会失败。

### 9.3 测试数据策略
- **小数据集**：直接用 xlsx 里的 100 行，双写到 Aurora MySQL + Redshift，用于冒烟和结果 diff
- **放大数据集**（后续）：用样本为种子 × 时间维度铺开到 10w ~ 1M 行，验证大结果集的 streaming 回包
- **加载方式**：
  - Aurora MySQL：xlsx → CSV → `LOAD DATA LOCAL INFILE` 或直接 `INSERT`
  - Redshift：xlsx → CSV → 上传 S3 → `COPY ... FROM 's3://...'`

### 9.4 AWS 基建清单（待用户确认 region/VPC）

| 资源 | 规格 | 说明 |
|---|---|---|
| Aurora MySQL Serverless v2 | 0.5 ~ 4 ACU | 数据库名 `dw`，一个只读端点 |
| Redshift Serverless | Base RPU 8 | Namespace `<RS_NAMESPACE>`，Workgroup `<RS_WORKGROUP>`，schema `dw` |
| Bedrock | Sonnet 4.6 模型 | Region 需开通 `us.anthropic.claude-sonnet-4-6` |
| Proxy EC2 | c7g.large | 跑 Go proxy；或 Fargate 起 1 个 task |
| S3 bucket | — | Redshift COPY 中转 |
| Secrets Manager | 两个 secret | Aurora 与 Redshift 的用户名/密码 |
| IAM Role | 1 个 | Proxy 访问 Bedrock + Secrets Manager + S3 + Redshift |

### 9.5 实施步骤（端到端）

**阶段 0：环境准备（~0.5 天）**
1. 选定 region（建议 `ap-northeast-1` 与现有音乐项目一致，或切到 `us-east-1` 方便 Bedrock）
2. 建 VPC 子网 + Aurora MySQL Serverless v2 + Redshift Serverless，Proxy 放同一 VPC
3. 开通 Bedrock `us.anthropic.claude-sonnet-4-6` 访问权限
4. 生成样本 CSV（`scripts/xlsx_to_csv.py`）

**阶段 1：数据初始化（~0.5 天）**
5. 在 Aurora MySQL 建库 `dw`，建表 `ads_thor_fin_payment_iap_data_new`，导入 100 行
6. 在 Redshift 建 schema `dw`，建表（类型映射按 9.2），COPY 导入 100 行
7. 在两边分别跑冒烟 SQL（把 `uid='1111'` 换成真实样本 uid 如 `'11543922'`），确认都能返回结果

**阶段 2：Proxy MVP（~2 天）**
8. Go 项目骨架：`proxy/main.go` + `translator/` + `executor/` + `cache/` + `config/`
9. 集成 `go-mysql-server` 起 MySQL 3306 端口，能 accept 握手和查询
10. 集成 `jackc/pgx` 连 Redshift，能透传 SQL 执行
11. 集成 Bedrock Sonnet 4.6：`POST /model/us.anthropic.claude-sonnet-4-6/invoke`
12. 接入 Schema 上下文：启动时从 Redshift `information_schema.columns` 拉表结构，拼进 prompt
13. 实现"翻译 → 执行 → 错误回喂重试 ≤ 3 次"闭环
14. 结果类型映射（Redshift 类型 → MySQL 协议类型）

**阶段 3：冒烟测试（~0.5 天）**
15. `mysql -h <proxy> -P 3306` 直连 Proxy，跑用户的冒烟 SQL，对比直连 Redshift 结果
16. 再跑 5~10 条故意含 MySQL 方言的 SQL（`IFNULL`/`DATE_FORMAT`/反引号/`LIMIT a,b`），验证翻译

**阶段 4：缓存与可观测（~1 天）**
17. 加进程内 LRU 缓存（normalize → hash → 译文）
18. 打日志：原 SQL / 译文 / LLM tokens / Redshift 延迟 / 重试次数 / 缓存命中
19. CloudWatch metrics：`translation_hit`、`translation_miss`、`llm_retry`、`p99_latency`

**阶段 5：影子双跑（~1 天）**
20. Proxy 增加影子模式：开关打开后，每条 SELECT 同时发给 Aurora 和 Redshift，结果集 diff
21. diff 报告写到 S3 + CloudWatch 告警

### 9.6 里程碑与验收

| 里程碑 | 验收标准 |
|---|---|
| M1：数据就绪 | Aurora + Redshift 两边都能各自跑冒烟 SQL 返回结果 |
| M2：Proxy 透传 | `mysql -h proxy` 能执行 `SELECT 1` 并拿到结果 |
| M3：LLM 翻译跑通 | 冒烟 SQL 经 Proxy → Redshift 返回结果与直连一致 |
| M4：方言覆盖 | 10 条 MySQL 方言测试 SQL 至少 8 条翻译成功 |
| M5：影子一致性 | 10 条 SQL 的 Aurora/Redshift 结果 diff 为空（数据一致前提下） |

### 9.7 确认决策

| # | 选项 | 决策 | 影响 |
|---|---|---|---|
| 1 | AWS region | **ap-northeast-1**，用 **AWS CDK** 部署 | CDK 可参数化 region，便于复用到 us-east-1 |
| 2 | 表名 `dw.` 前缀 | **去掉**，翻译时 rewrite 为裸表名 | 翻译器需要做 schema strip；Redshift 侧把表直接建在 `public` 或默认 schema |
| 3 | 部署形态 | **本机 Docker** 起 Proxy 做 demo | 不上 EC2/Fargate，Proxy 通过 VPN/公网访问 Aurora/Redshift（需放开 security group） |
| 4 | 认证 | **Proxy 自管** MySQL 账号密码 | 硬编码一个 demo 账号 `demo/<DEMO_PASSWORD>`，后端连 Aurora/Redshift 走 Secrets Manager |
| 5 | LLM 入口 | **Bedrock Invoke API**（`bedrock-runtime:InvokeModel`） | Go SDK `aws-sdk-go-v2/service/bedrockruntime`，IAM 鉴权 |
| 6 | 数据规模 | **只用 xlsx 的 100 行**做 demo，不压测 | 简化 M5 验收；Aurora 和 Redshift 双写 100 行 |
| 7 | LLM 预算熔断 | **不设** | Demo 阶段不做熔断逻辑；后续生产版再加 |

### 9.8 因决策调整的点（覆盖 9.4 / 9.5）

**基建清单（调整）**：
| 资源 | 调整后 |
|---|---|
| Proxy 部署 | 本机 `docker run`，暴露 3306 端口 |
| Aurora MySQL Serverless v2 | 通过 CDK 部署到 ap-northeast-1，**需要公网可达或通过 EC2 bastion 穿透**（本机 Docker 要能连上） |
| Redshift Serverless | 同上，**Enhanced VPC Routing 关闭 + Publicly accessible 开启** 以便本机连 |
| Bedrock | 确认 ap-northeast-1 开通了 `us.anthropic.claude-sonnet-4-6`（若未开通，代码里 region 走 `us-east-1` 即可，其他资源留在东京） |
| IAM | 本机通过 `aws configure` 或 `AWS_PROFILE` 拿凭证（而非 Role） |

**实施步骤（调整）**：
- **阶段 0**（环境准备）增加：**写 CDK stack** 一次性部署 Aurora + Redshift + Security Group（放开本机 IP）；生成样本 CSV
- **阶段 2**（Proxy MVP）增加：**Dockerfile** + `docker-compose.yml`，一键起 Proxy 容器
- **阶段 1**（数据初始化）调整：表名去掉 `dw.` 前缀，Redshift 建在 `public` schema
- **阶段 3**（冒烟）调整：用户给的 SQL 里的 `dw.ads_thor_fin_payment_iap_data_new` → Proxy 翻译时改写成 `ads_thor_fin_payment_iap_data_new`

### 9.9 SQL 翻译规则：schema 前缀处理

Proxy 翻译逻辑在 Sonnet prompt 里加入明确指令：

```
Remove any schema prefix like `dw.` from table names — 
in Redshift the tables live in the default `public` schema.

Example:
  INPUT:  SELECT * FROM dw.ads_thor_fin_payment_iap_data_new
  OUTPUT: SELECT * FROM ads_thor_fin_payment_iap_data_new
```

### 9.10 最终架构：两容器拆分（Proxy + Agent）

> 2026-04-25 用户确认：不用 CDK、不用 go-mysql-server 自研全链路，转为**两个容器 + 进程间 HTTP**。

```
                        ┌──────────────────────────────────────────────┐
MySQL client            │ Docker host (本机 EC2)                        │
(mysql / JDBC / app)    │                                              │
    │                   │  ┌────────────────────┐   HTTP/JSON          │
    │ MySQL wire        │  │ mysql-redshift-    │  ◀──────────────┐   │
    └───tcp:3306───────►│  │ proxy  (Go)        │                 │   │
                        │  │                    │  POST /translate│   │
                        │  │ - go-mysql-server  │ ───────────────►│   │
                        │  │ - LRU cache        │                 │   │
                        │  │ - pgx → Redshift   │  ┌──────────────┴─┐ │
                        │  │ - result mapping   │  │ db-convertor-  │ │
                        │  └────────┬───────────┘  │ agent (Python) │ │
                        │           │              │                │ │
                        │           │              │ - Strands SDK  │ │
                        │           │              │ - Sonnet 4.6   │ │
                        │           │              │ - @tool skills │ │
                        │           │              │ - MD refs      │ │
                        │           │              └──────┬─────────┘ │
                        └───────────┼─────────────────────┼───────────┘
                                    │ pgx (Postgres wire) │ Bedrock invoke
                                    ▼                     ▼
                             Redshift Serverless    Bedrock (Sonnet 4.6)
                              (ap-northeast-1)       (ap-northeast-1)
```

#### 9.10.1 容器 1：`mysql-redshift-proxy`（Go）
| 组件 | 职责 |
|---|---|
| `server/` | MySQL wire protocol 服务端（`go-mysql-server` 库） |
| `cache/` | LRU（key = `md5(normalize(sql))`） |
| `convertor_client/` | HTTP client → 调用 Agent 容器的 `POST /translate` |
| `executor/` | `jackc/pgx` 执行 Redshift SQL |
| `resultmap/` | Postgres 结果集 → MySQL 协议包 |
| `main.go` | 整合：accept → cache → agent → execute → 失败回喂 agent 重试 ≤ 3 次 |

Proxy **不直接调 Bedrock**，所有 LLM 相关逻辑隔离在 Agent 容器。

#### 9.10.2 容器 2：`db-convertor-agent`（Python，Strands SDK）
参考用户的 [`game-cs-agent/runtime/main.py`](https://github.com/HanqingAWS/game-cs-agent) 结构：

```python
from strands import Agent
from strands.models import BedrockModel
from strands.tools import tool

MODEL_ID = "us.anthropic.claude-sonnet-4-6"
REGION = "ap-northeast-1"

SYSTEM_PROMPT = """你是一个 SQL 方言翻译器，把 MySQL SQL 翻译成 Redshift SQL。

规则：
1. 输出必须是单条可执行的 Redshift SQL，不要解释、不要 markdown 围栏
2. 去掉 schema 前缀（如 dw.xxx → xxx）
3. 反引号 → 双引号（MySQL `col` → Redshift "col"）
4. 遇到不确定的函数/语法，先调 lookup_dialect_rule 查规则库
5. 若用户给了前一次执行的错误信息，据此修正 SQL
"""

@tool
def lookup_dialect_rule(keyword: str) -> str:
    """从 references/ 目录检索方言差异规则（如 GROUP_CONCAT / DATE_FORMAT）"""
    # 简单 pre-filter：SQL 中命中关键词时注入对应 .md 片段
    return read_markdown_chunk(f"references/{keyword}.md")

@tool
def get_table_schema(table_name: str) -> str:
    """从缓存返回表的列定义"""
    return SCHEMA_CACHE.get(table_name, "")

# FastAPI 包一层：POST /translate {sql, error?} → {redshift_sql}
# Proxy 通过这个 HTTP 端口调用
```

暴露两个 HTTP 接口：
- `POST /translate` — body: `{"sql": "...", "error": null}` → `{"redshift_sql": "..."}`
- `POST /translate` — body: `{"sql": "...", "error": "function group_concat does not exist", "prev_sql": "..."}` → 带错误修正

#### 9.10.3 references 目录（方言知识库）
```
db-convertor-agent/references/
├── schema_prefix.md        # dw.xxx → xxx
├── backticks.md            # `col` → "col"
├── limit_offset.md         # LIMIT a,b → LIMIT b OFFSET a
├── ifnull.md               # IFNULL → COALESCE
├── group_concat.md         # GROUP_CONCAT → LISTAGG
├── date_format.md          # DATE_FORMAT → TO_CHAR + 格式符映射表
├── date_arith.md           # DATE_ADD/SUB → INTERVAL
├── on_duplicate_key.md     # ON DUPLICATE KEY → MERGE 或拒绝
├── types.md                # TINYINT → SMALLINT 等
└── _index.md               # 关键词 → 文件的映射（pre-filter 用）
```

Agent 用关键词检索：SQL 里出现 `GROUP_CONCAT` → 读 `group_concat.md` → 注入 prompt。**不用向量库**，简单字符串匹配。

---

### 9.11 数据迁移策略（Aurora → Redshift）

用户确认：A（demo 快速）+ B（生产规范方案）组合，**B 必须做技术验证**。

#### 方案 A：Python 脚本（demo 用）
```
scripts/migrate_simple.py
  读 Aurora MySQL → pandas DataFrame
  写 S3 CSV
  Redshift COPY ... FROM 's3://.../data.csv' FORMAT AS CSV
```
特点：10 秒内跑完 100 行；方便反复重建；不模拟生产流程。

#### 方案 B：Aurora S3 Export (Parquet) → Redshift COPY（生产规范）

**需要验证的技术点**（本次实施会实操验证）：
1. Aurora MySQL Serverless v2 是否支持 `start-export-task`（历史上 Aurora Serverless v1 不支持，v2 理论支持但要确认）
2. Export 到 S3 的 Parquet 目录结构（预期：`<prefix>/<cluster>/<db>/<schema>/<table>/part-*.parquet`）
3. Redshift `COPY FROM PARQUET` 对 Aurora 导出的类型映射（如 MySQL `DECIMAL(12,4)` → Parquet decimal → Redshift DECIMAL 是否无损）
4. IAM 角色（Aurora 需 S3 写权限，Redshift 需 S3 读权限 + KMS 解密权限——Export 默认加密）
5. 耗时基线（100 行样本 snapshot→export 大约多少分钟）

**验证后产出**：
- `scripts/migrate_prod_like.sh` 完整命令序列（snapshot + export + COPY）
- `docs/migration_prod.md` 记录耗时、类型映射坑点、IAM 最小权限集

---

### 9.12 最终目录结构（覆盖 9.10 旧版）

```
mysql-redshift-proxy/
├── DESIGN.md                          # 本文档
├── table-schema.xlsx                  # 表结构 + 100 行样本
├── docker-compose.yml                 # 一键起两容器
│
├── scripts/                           # 数据准备 + 迁移
│   ├── xlsx_to_csv.py                 # xlsx → Aurora 导入用 CSV
│   ├── load_aurora.sh                 # mysql CLI 批量导入
│   ├── migrate_simple.py              # 【方案 A】Python 一步迁移
│   └── migrate_prod_like.sh           # 【方案 B】Aurora Export → Redshift COPY
│
├── infra/                             # AWS CLI 脚本（暂不用 CDK）
│   ├── 01_create_aurora.sh            # create-db-cluster (serverless v2)
│   ├── 02_create_redshift.sh          # create-namespace + create-workgroup
│   ├── 03_create_s3_bucket.sh
│   ├── 04_authorize_sg.sh             # 自引用 <SG-ID>
│   └── README.md                      # 执行顺序与参数
│
├── proxy/                             # 容器 1：Go MySQL-Redshift Proxy
│   ├── cmd/proxy/main.go
│   ├── internal/
│   │   ├── server/                    # go-mysql-server 封装
│   │   ├── cache/                     # LRU
│   │   ├── convertor_client/          # HTTP → agent
│   │   ├── executor/                  # pgx → Redshift
│   │   ├── resultmap/                 # PG → MySQL 结果集
│   │   └── config/
│   ├── go.mod
│   └── Dockerfile
│
├── agent/                             # 容器 2：db-convertor-agent (Python + Strands)
│   ├── app.py                         # FastAPI + Strands Agent
│   ├── tools/
│   │   ├── lookup_dialect_rule.py     # 查 references/
│   │   └── get_table_schema.py        # 从 Redshift information_schema 拉
│   ├── references/                    # 方言知识库（Markdown）
│   │   ├── _index.md
│   │   ├── schema_prefix.md
│   │   ├── backticks.md
│   │   ├── limit_offset.md
│   │   ├── ifnull.md
│   │   ├── group_concat.md
│   │   ├── date_format.md
│   │   ├── date_arith.md
│   │   ├── on_duplicate_key.md
│   │   └── types.md
│   ├── requirements.txt               # strands-agents, fastapi, uvicorn, boto3
│   └── Dockerfile
│
└── test/
    ├── smoke.sh                       # mysql -h localhost 跑用户的 iap SQL
    └── dialect_cases.sql              # 10 条方言测试
```

---

### 9.13 部署/运行环境（最终确定）

| 项 | 值 |
|---|---|
| AWS Account | `<ACCOUNT-ID>` |
| AWS Region | `ap-northeast-1` |
| Current EC2 | `i-0d41f6f4b1230043e`（cc 实例，运行 Claude Code） |
| VPC | `<VPC-ID>` |
| Subnet | `<SUBNET-1A>`（`ap-northeast-1a`） |
| SecurityGroup | `<SG-ID>`（现全通，新资源**复用**这个 SG，不开公网） |
| SSH Key | `/home/ec2-user/projects/cc/testkey.pem`（必要时跳其他 EC2） |
| IAM | EC2 Instance Role `ec2-admin-role`（已可调 Bedrock/RDS/Redshift/S3） |
| Bedrock Model | `us.anthropic.claude-sonnet-4-6`（ap-northeast-1 确认有 INFERENCE_PROFILE） |
| Docker | 本机 Docker（Go proxy 容器 + Python agent 容器） |

**不开放任何公网端口**——所有 AWS 资源都放 `<SG-ID>`，Proxy 容器在本机跑，通过内网 VPC 直接连 Aurora/Redshift。

---

### 9.14 实施里程碑（最终版，覆盖 9.5）

| Step | 内容 | 预期耗时 |
|---|---|---|
| S0 | 检查 Docker / Go / Python 环境；创建项目骨架目录 | 10 min |
| S1 | `infra/01-04_*.sh`：CLI 创建 Aurora MySQL Serverless v2 + Redshift Serverless + S3 bucket；复用现有 SG | 30-45 min（Aurora 创建慢） |
| S2 | `scripts/xlsx_to_csv.py` + `load_aurora.sh`：把 xlsx 的 100 行写入 Aurora | 15 min |
| S3 | `scripts/migrate_simple.py`（方案 A）：Aurora → S3 CSV → Redshift COPY；两边各跑用户 SQL 确认一致 | 30 min |
| S4 | `agent/` 容器：Strands + Sonnet 4.6 + references + FastAPI `/translate`；单测几条 SQL | 1 h |
| S5 | `proxy/` 容器：go-mysql-server + pgx + HTTP 调 agent + 错误回喂重试；整体能跑通用户 SQL | 2 h |
| S6 | `docker-compose.yml`：两容器一键起；`test/smoke.sh` 直连 `mysql -h localhost -P 3306` 验证 | 30 min |
| S7 | 【方案 B 验证】`migrate_prod_like.sh`：Aurora snapshot + export + Redshift COPY；记录耗时/坑点 | 1-2 h（等 snapshot） |
| S8 | 10 条方言测试 SQL 过一遍，补 references | 30 min |

总计 demo 能跑起来：约 **5-7 小时纯工作时间**（其中 Aurora 创建 + snapshot export 是等待时间，可并行做其他事）。


---

## 附录 A：参考项目

- [go-mysql-server](https://github.com/dolthub/go-mysql-server) — MySQL 协议服务端框架
- [jackc/pgx](https://github.com/jackc/pgx) — Go Postgres 驱动（连 Redshift）
- [sqlglot](https://github.com/tobymao/sqlglot) — SQL AST 解析/方言转换（可作 LLM 的辅助校验）
- [Vitess](https://github.com/vitessio/vitess) — MySQL 协议解析的工程参考
- [Heimdall Data Proxy](https://www.heimdalldata.com/) — 商业方案参考（Postgres 驱动 + Redshift 优化）
