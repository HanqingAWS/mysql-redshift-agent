# MySQL-Redshift LLM Proxy — 完成报告

**日期**：2026-04-25
**状态**：端到端跑通；全部里程碑 S0–S8 完成，方案 B (Parquet export → Redshift COPY) 已实测成功（100 行一致落库）
**项目**：`/home/ec2-user/projects/mysql-redshift-proxy/`

---

## 做了什么

让 MySQL 客户端**零改动**连接 Redshift 做查询。客户端看到的是标准 MySQL wire protocol，背后由两个容器协作：

```
 mysql CLI
   │  (MySQL wire)
   ▼
┌──────────────────────────┐         ┌────────────────────────────┐
│ mysql-redshift-proxy     │         │ db-convertor-agent         │
│ Go + go-mysql-org/server │ ──HTTP──▶ Python + Strands + Sonnet 4.6│
│ pgx to Redshift          │         │ Bedrock (jp.anthropic...)   │
│ LRU cache (SQL 规范化)    │         │ Markdown references prefill │
└──────────────────────────┘         └────────────────────────────┘
          │                                        │
          ▼                                        ▼
    Redshift Serverless                     (翻译返回 SQL)
```

- Proxy 收到 MySQL SQL → 归一化缓存 key → 问 agent 要 Redshift SQL → 用 pgx 跑 Redshift → 结果集转成 MySQL wire 格式回客户端
- 执行失败回喂 agent 最多 `MaxAttempts=3` 次让它修正
- Agent 用 Sonnet 4.6（`jp.anthropic.claude-sonnet-4-6`），调用 Bedrock ConverseStream
- 方言知识库用 **Markdown pre-filter 注入**（不是 RAG）：关键词命中直接把对应的 `.md` 文件塞进 prompt

---

## 目录结构

```
mysql-redshift-proxy/
├── DESIGN.md              # 9 章设计文档（事前写的）
├── REPORT.md              # 本文件（事后总结）
├── .env.example           # 环境变量模板
├── docker-compose.yml     # 两容器一键起
│
├── agent/                 # Python + Strands + FastAPI
│   ├── Dockerfile
│   ├── app.py             # POST /translate；extract_sql() 去 prose
│   ├── requirements.txt
│   ├── tools/
│   │   ├── lookup_dialect_rule.py   # Strands @tool：按关键字查 rule md
│   │   └── get_table_schema.py      # Strands @tool：查 Redshift 列类型
│   └── references/        # 11 个方言 markdown（pre-filter 的知识源）
│
├── proxy/                 # Go + go-mysql-org v1.14.0 + pgx v5
│   ├── Dockerfile
│   ├── go.mod
│   ├── cmd/proxy/main.go
│   └── internal/
│       ├── config/        # FromEnv()
│       ├── cache/         # LRU + SQL normalize (md5(规范化 SQL))
│       ├── convertor_client/  # HTTP 调 agent
│       ├── executor/      # Redshift pgx 执行器
│       ├── resultmap/     # executor.Result → gomysql.Result
│       └── server/        # MySQL wire server + connHandler
│
├── infra/                 # AWS CLI 一次性资源脚本
│   ├── 00_env.sh          # 公共环境变量
│   ├── 01_create_aurora.sh
│   ├── 02_load_aurora.sh
│   ├── 03_create_redshift.sh
│   ├── 04_copy_to_redshift.sh
│   ├── 05_create_redshift_role.sh
│   └── 06_planb_export.sh # 方案 B：snapshot → Parquet export
│
├── scripts/
│   ├── schema.sql           # MySQL DDL
│   ├── schema_redshift.sql  # Redshift DDL（TINYINT→SMALLINT etc）
│   ├── xlsx_to_csv.py       # xlsx → TSV/CSV
│   ├── load_aurora.sh       # Aurora 导入
│   ├── migrate_simple.py    # 方案 A：Aurora → S3 CSV → Redshift COPY
│   └── dialect_tests.sh     # 10 个方言测试用例
│
└── docs/
    ├── dialect_tests.md     # 方言测试说明
    └── migration_prod.md    # 生产迁移两方案对比 + 坑点记录
```

---

## 里程碑完成情况

| # | 里程碑 | 说明 | 状态 |
|---|--------|------|------|
| S0 | 项目骨架 | 目录 + .env.example + docker-compose | ✅ |
| S1 | AWS 基建 | Aurora MySQL Serverless v2 + Redshift Serverless + S3 + IAM role | ✅ |
| S2 | 源数据 | xlsx → TSV → Aurora LOAD DATA 100 行 | ✅ |
| S3 | 方案 A 迁移 | Python Aurora → S3 CSV → Redshift COPY，行数对齐 | ✅ |
| S4 | agent 容器 | Python + Strands + Bedrock Sonnet 4.6 + 11 markdown rules | ✅ |
| S5 | proxy 容器 | Go + go-mysql server + pgx + LRU cache + 回喂重试 | ✅ |
| S6 | 集成 + 冒烟 | docker-compose 双容器 + `SELECT 1` + 用户样例 SQL 跑通 | ✅ |
| S7 | 方案 B 验证 | Snapshot (3 min) → RDS Export Parquet (8.5 min) → Redshift COPY (2 s)，100 行落库一致；踩到 TINYINT→int32 必须用 INTEGER 的坑 | ✅ 见 docs/migration_prod.md |
| S8 | 方言测试 | 10 个用例全绿（schema prefix / 反引号 / LIMIT a,b / IFNULL / DATE_FORMAT / GROUP_CONCAT / INTERVAL / UNIX_TIMESTAMP / CONCAT_WS / 用户样例） | ✅ 见 docs/dialect_tests.md |

---

## 怎么跑

```bash
cd /home/ec2-user/projects/mysql-redshift-proxy

# 1. 基建（一次性）
bash infra/01_create_aurora.sh
bash infra/02_load_aurora.sh
bash infra/03_create_redshift.sh
bash infra/05_create_redshift_role.sh
bash infra/04_copy_to_redshift.sh

# 2. 起服务
set -a && . ./.env && set +a    # 关键！shell 的 AWS_REGION 会覆盖 compose
sudo -E docker compose up -d --build

# 3. 用
mysql -h 127.0.0.1 -P 3307 -u demo -p<DEMO_PASSWORD> dw -e \
  "select * from dw.ads_thor_fin_payment_iap_data_new limit 3"

# 4. 跑方言测试
./scripts/dialect_tests.sh
```

---

## 核心验证结果

### 用户给的样例 SQL

```sql
select uid,fpid,transaction_id,new_app_id as app_id,base_price,amount,
       payment_processor,order_id, ts
from dw.ads_thor_fin_payment_iap_data_new
where event_time >= '2025-09-01 00:00:00'
  and event_time <= '2025-10-01 00:00:00'
  and game_name='1' and amount > 0
  and payment_processor not in ('centurygames_pay','centurygames_store',
      'centurygames','kg3rdpartypayment','pc')
order by ts limit 1000 offset 0
```

Proxy → Agent 翻译后（agent 耗时 ~1.5s）：

```sql
select uid,fpid,transaction_id,new_app_id as app_id,base_price,amount,
       payment_processor,order_id, ts
from ads_thor_fin_payment_iap_data_new
where event_time >= '2025-09-01 00:00:00'
  and event_time <= '2025-10-01 00:00:00'
  and game_name='1' and amount > 0
  and payment_processor not in ('centurygames_pay','centurygames_store',
      'centurygames','kg3rdpartypayment','pc')
order by ts limit 1000 offset 0
```

— 只做了一件事：**剥掉 `dw.` 前缀**。其他语义 100% 保留。结果集与 Aurora 一致。

### 10 个方言用例

见 `docs/dialect_tests.md`，全部绿。覆盖的重要转换：

- `` ` `` → `"` （反引号转双引号）
- `LIMIT a, b` → `LIMIT b OFFSET a`
- `IFNULL` → `COALESCE`
- `DATE_FORMAT(%Y-%m-%d)` → `TO_CHAR('YYYY-MM-DD')`
- `GROUP_CONCAT(DISTINCT x)` → `LISTAGG(DISTINCT x, ',') WITHIN GROUP (ORDER BY x)`
- `DATE_SUB(d, INTERVAL n DAY)` → `DATEADD(day, -n, d)`
- `UNIX_TIMESTAMP(ts)` → `EXTRACT(EPOCH FROM ts)`

---

## 走过的坑（都已修复 / 记录）

| # | 坑 | 解决 |
|---|----|------|
| 1 | Bedrock model id `us.anthropic.claude-sonnet-4-6` 在 Tokyo 不可用 | 换 `jp.anthropic.claude-sonnet-4-6`（`aws bedrock list-inference-profiles` 查到的） |
| 2 | Strands `BedrockModel` 同时传 `region_name` 和 `boto_session` 会报错 | 只传 `boto_session=Session(region_name=...)`，其中包含 region |
| 3 | Container env `AWS_REGION=us-east-1` 从 host shell 继承进来 | `docker compose` 前必须 `set -a && . ./.env && set +a` |
| 4 | Agent 调用 `get_table_schema` tool 后在 SQL 前夹带思考 prose | `extract_sql()` 从 `SELECT|WITH|INSERT|...` 关键字开始截断 + system prompt 强化"tool 后不要写解释" |
| 5 | VPC 只有 1 AZ，Aurora 要 ≥2 AZ / Redshift 要 3 AZ | 额外建 subnet `10.0.2.0/24` (1c)、`10.0.3.0/24` (1d) 并挂公共路由表 |
| 6 | `docker compose` CLI 插件 dnf 装不上 | 手动下载 aarch64 二进制到 `/usr/local/lib/docker/cli-plugins/` |
| 7 | go-mysql v1.14.0 重命名了 auth API | 用 `NewInMemoryAuthenticationHandler` + `NewCustomizedConn` 新签名 |
| 8 | Redshift Serverless 冷启动 ~60s，proxy 的 60s timeout 会触发 | 生产要拉大 timeout 或加 wake-up ping；demo 再跑一次就好 |
| 9 | RDS Snapshot Export 需要客户 managed KMS，不能用 `aws/s3` | 建了 `alias/dw-export-key` |
| 10 | Parquet COPY 列类型严格等宽：TINYINT/SMALLINT → int32 → 要用 INTEGER | Redshift DDL 凡是 MySQL TINYINT/SMALLINT 一律写 INTEGER |
| 11 | Redshift COPY PARQUET 从 KMS 加密 S3 读取要 `kms:Decrypt` | 给 `redshift-copy-role` 加了 KMSDecrypt inline policy |

---

## 已知局限 / MVP 之后要做

1. **Prepared statement 不支持**（`HandleStmtPrepare` 直接返回 error）。ORM 大多数用 prepared，生产要补
2. **类型映射粗糙**：Redshift 所有列当成字符串回传给 MySQL 客户端。数字/日期类型用户自己转
3. **无 cost 熔断**：demo 跑到天亮没人拦；生产要接 Bedrock usage 限流
4. **无影子双跑**：现在是"Agent 说什么 Redshift 就跑什么"；生产要和 MySQL 对比结果一段时间再切流
5. **Cache 规范化太简单**：`lowercase + literal→?` 能覆盖 80% 但 `IN (a, b, c)` 变 `IN (?, ?, ?)` 可能造成语义不同的 SQL 共用 key。目前 demo 无影响
6. **单容器 agent**：HTTP 同步阻塞，大量并发要排队。生产用 queue + worker pool

---

## 文件速查

- `DESIGN.md` — 事前设计（架构、迁移方案、references 体系、里程碑）
- `REPORT.md` — 本文件（事后总结）
- `docs/dialect_tests.md` — 10 用例测试结论
- `docs/migration_prod.md` — 两方案迁移对比 + 生产建议

所有资源都在 AWS 账号 `<ACCOUNT-ID>` / region `ap-northeast-1`，VPC `<VPC-ID>`，复用 CC EC2 实例的安全组，**不对公网开放任何端口**。
