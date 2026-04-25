# mysql-redshift-agent

让 **MySQL 客户端零改动**连接 **Amazon Redshift** 查询的代理。客户端看到的是标准 MySQL wire
protocol，背后由 Claude Sonnet 4.6（via Bedrock）实时把 MySQL 方言的 SQL 翻译成 Redshift 方言，
然后通过 pgx 到 Redshift 执行，再把结果集转回 MySQL 协议格式返回给客户端。

```
 mysql CLI / BI 工具 / ORM
        │  (MySQL wire protocol)
        ▼
 ┌────────────────────────┐         ┌─────────────────────────────┐
 │ mysql-redshift-proxy   │──HTTP──▶│ db-convertor-agent          │
 │ Go + go-mysql-org      │         │ Python + Strands            │
 │ LRU cache + pgx        │         │ Bedrock Sonnet 4.6          │
 └────────────────────────┘         │ Markdown reference prefilter│
        │                           └─────────────────────────────┘
        ▼
  Redshift Serverless
```

两个容器通过 HTTP 通信，docker-compose 一键起。

- Proxy (Go)：收 MySQL 连接 → 缓存查 → 问 agent 要 Redshift SQL → pgx 跑 Redshift →
  结果集转 MySQL wire 格式返回；执行失败把 error 回喂 agent 最多 `MaxAttempts=3` 次让它修
- Agent (Python)：`POST /translate`，调 Bedrock Sonnet 4.6 翻译；方言知识用
  **Markdown pre-filter** 注入（不是 RAG）

---

## 目录结构

```
mysql-redshift-agent/
├── README.md                     # 本文件
├── DESIGN.md                     # 事前设计文档（9 章）
├── REPORT.md                     # 事后完成报告（踩坑 / 验证）
├── docker-compose.yml            # 两容器一键起
├── .env.example                  # 环境变量模板（复制为 .env 后填值）
├── .gitignore                    # .env / data 不进 git
│
├── agent/                        # Python + Strands + FastAPI + Bedrock
│   ├── Dockerfile
│   ├── app.py                    # POST /translate；extract_sql() 去 prose
│   ├── requirements.txt
│   ├── tools/
│   │   ├── lookup_dialect_rule.py  # Strands @tool：按关键字查 rule md
│   │   └── get_table_schema.py     # Strands @tool：查 Redshift 列类型
│   └── references/               # 11 个方言 markdown（pre-filter 的知识源）
│
├── proxy/                        # Go + go-mysql-org v1.14.0 + pgx v5
│   ├── Dockerfile
│   ├── go.mod / go.sum
│   ├── cmd/proxy/main.go         # 启动入口
│   └── internal/
│       ├── config/               # FromEnv()
│       ├── cache/                # LRU + SQL normalize (md5)
│       ├── convertor_client/     # HTTP 调 agent
│       ├── executor/             # Redshift pgx 执行器
│       ├── resultmap/            # executor.Result → gomysql.Result
│       └── server/               # MySQL wire server + connHandler
│
├── infra/                        # AWS CLI 一次性资源脚本
│   ├── 00_env.sh                 # 公共环境变量（已占位化，使用前填值）
│   ├── 01_create_aurora.sh       # Aurora MySQL Serverless v2
│   ├── 02_create_redshift.sh     # Redshift Serverless namespace+workgroup
│   ├── 03_create_s3.sh           # 迁移 bucket
│   ├── 04_sg_self_ref.sh         # SG 自引用校验
│   ├── 05_create_redshift_role.sh # redshift-copy-role (S3/KMS access)
│   └── 06_planb_export.sh        # 方案 B：snapshot → Parquet export（一键）
│
├── scripts/
│   ├── schema.sql                # MySQL DDL
│   ├── schema_redshift.sql       # Redshift DDL（方案 A 用）
│   ├── xlsx_to_csv.py            # xlsx → TSV/CSV
│   ├── load_aurora.sh            # Aurora 导入
│   ├── migrate_simple.py         # 方案 A：Aurora → S3 CSV → Redshift COPY
│   └── dialect_tests.sh          # 10 个方言测试用例
│
└── docs/
    ├── dialect_tests.md          # 10 方言用例结论
    └── migration_prod.md         # 生产迁移两方案对比 + 坑点（Parquet/KMS/类型）
```

---

## 怎么用（详细步骤）

### 0. 前置条件

| 项 | 要求 |
|---|---|
| AWS 账号 | 本区域能建 Aurora / Redshift Serverless / S3 / KMS |
| VPC | 至少 **3 个 AZ** 的子网（Redshift Serverless 强制要求） |
| Bedrock | 在部署区域开通 **Anthropic Claude Sonnet 4.6** 的 inference profile |
| 本机 | Docker 20+ 和 `docker compose` v2+；mysql client |
| IAM | 本机 / EC2 instance profile 有 `rds:*` / `redshift-serverless:*` / `s3:*` / `bedrock:InvokeModel` / `iam:PassRole` |

> Bedrock 的 inference profile 按区域不同会有不同前缀：Tokyo 是 `jp.`，美区 `us.`，全球 `global.`。
> 查你区域的可用值：`aws bedrock list-inference-profiles --region <your-region>`

### 1. 克隆 + 配置环境

```bash
git clone https://github.com/HanqingAWS/mysql-redshift-agent.git
cd mysql-redshift-agent

# 复制模板并填入你的值
cp .env.example .env
vim .env
#   AWS_REGION=...
#   BEDROCK_MODEL_ID=jp.anthropic.claude-sonnet-4-6   # 或 us./global.
#   REDSHIFT_HOST=<workgroup>.<account>.<region>.redshift-serverless.amazonaws.com
#   REDSHIFT_PASSWORD=...
#   REDSHIFT_DSN=postgres://admin:<password>@<host>:5439/dev?sslmode=require
#   PROXY_MYSQL_USER=demo
#   PROXY_MYSQL_PASSWORD=<你自己选一个>

# 把 infra/00_env.sh 里的 VPC/SG/SUBNET 占位符替换成你的 ID
vim infra/00_env.sh
```

### 2. 创建 AWS 基建（一次性）

```bash
cd infra
bash 01_create_aurora.sh        # Aurora MySQL Serverless v2（~5–10 min）
bash 02_create_redshift.sh      # Redshift Serverless namespace + workgroup
bash 03_create_s3.sh            # 迁移 bucket
bash 05_create_redshift_role.sh # redshift-copy-role，关联到 workgroup
cd ..
```

完成后把 `.env` 里的 `REDSHIFT_HOST` / `REDSHIFT_DSN` 改成实际 endpoint（`aws redshift-serverless
get-workgroup --workgroup-name <rs-wg>` 查）。

### 3. 导入源数据 → 迁移到 Redshift（方案 A，小表）

```bash
# 把你的 xlsx 转成 TSV/CSV
python3 scripts/xlsx_to_csv.py

# 建 Aurora schema + LOAD DATA
bash scripts/load_aurora.sh

# Python pipeline：Aurora → S3 CSV → Redshift COPY
python3 scripts/migrate_simple.py
```

> 生产大表建议直接用 **方案 B**（RDS Snapshot Export → Parquet → COPY），
> 一键脚本：`bash infra/06_planb_export.sh`。详见 [docs/migration_prod.md](docs/migration_prod.md)。

### 4. 起 Proxy + Agent 两容器

```bash
# ⚠️ 关键：宿主机 shell 的 AWS_REGION 会覆盖 compose 默认，必须先把 .env 注入环境
set -a && . ./.env && set +a

# 起两容器
sudo -E docker compose up -d --build

# 健康检查
curl http://127.0.0.1:8088/healthz
# {"ok":true,"model":"jp.anthropic.claude-sonnet-4-6","region":"ap-northeast-1"}

docker ps
# agent   Up (healthy)   0.0.0.0:8088->8088/tcp
# proxy   Up             0.0.0.0:3307->3306/tcp    ← 对外端口 3307
```

### 5. 用 MySQL 客户端连 Proxy

```bash
# 端口 3307（避开本机已有 MySQL 的 3306）
mysql -h 127.0.0.1 -P 3307 -u demo -p<PROXY_MYSQL_PASSWORD> dw

mysql> SELECT COUNT(*) FROM dw.ads_thor_fin_payment_iap_data_new;
+-------+
| total |
+-------+
|   100 |
+-------+

mysql> SELECT uid, IFNULL(app_version, '-') AS v
    -> FROM dw.ads_thor_fin_payment_iap_data_new LIMIT 3;
```

- `dw.` 前缀会被自动剥掉（Redshift 表在 `public`）
- `IFNULL` 会被翻译成 `COALESCE`
- `LIMIT a, b` → `LIMIT b OFFSET a`
- 参见 [docs/dialect_tests.md](docs/dialect_tests.md) 的完整 10 用例

### 6. 跑方言测试

```bash
MYUSER=demo PASS=<PROXY_MYSQL_PASSWORD> bash scripts/dialect_tests.sh
```

---

## 关键组件说明

### Agent（`agent/app.py`）

- `POST /translate` 接受 `{sql, prev_error?, prev_sql?}`，返回 `{redshift_sql, used_rules, latency_ms, attempt}`
- 第一次翻译：`attempt=initial`；Proxy 侧执行失败后带 `prev_error` 和 `prev_sql` 再来：`attempt=fix`
- **Pre-filter**：在把请求送 Bedrock 之前，用正则扫 SQL，命中的方言 rule markdown 直接拼进 prompt（比等 agent 自己调 tool 更快更稳定）
- **extract_sql()**：Agent 在用 tool 之后有时会夹带思考性 prose，用正则从 `SELECT|WITH|INSERT|…` 起截断
- Strands `BedrockModel` 必须**显式传 boto3 session** 才能正确绑定 region，**不能**同时传 `region_name` 和 `boto_session`

### Proxy（`proxy/`）

- `cmd/proxy/main.go` 启动入口
- `internal/server/server.go` 实现 go-mysql-org/go-mysql 的 `Handler`。只支持 `HandleQuery`（一次性查询）；`HandleStmtPrepare` 返回 error（prepared statement 暂不支持）
- `internal/cache/cache.go` SQL 归一化（小写 + literal→?）+ md5 作 LRU key
- `internal/server/server.go::execWithRetry` 执行 + 失败回喂 agent，`MaxAttempts=3`
- `internal/resultmap/resultmap.go` 把 `[][]any` 转成 `gomysql.BuildSimpleTextResultset`，所有列当字符串回传（数字/日期格式化后）

### 方言 references（`agent/references/`）

Pre-filter 用的 11 个 markdown，覆盖 MySQL→Redshift 的常见方言差异：

- `schema_prefix.md` — 剥 `dw.` schema 前缀
- `backticks.md` — `` ` `` → `"`
- `limit_offset.md` — `LIMIT a, b` → `LIMIT b OFFSET a`
- `ifnull.md` — `IFNULL` → `COALESCE`
- `group_concat.md` — `GROUP_CONCAT` → `LISTAGG(...) WITHIN GROUP`
- `date_format.md` — `DATE_FORMAT(%Y-%m-%d)` → `TO_CHAR('YYYY-MM-DD')`
- `date_arith.md` — `DATE_ADD/SUB + INTERVAL` → `DATEADD`
- `on_duplicate_key.md` — `ON DUPLICATE KEY` → UNTRANSLATABLE（Redshift 无原生等价）
- `types.md` — TINYINT/MEDIUMTEXT/DATETIME 映射
- `str_to_date.md` / `unix_timestamp.md` / `concat_ws.md`

关键词匹配硬编码在 `agent/tools/lookup_dialect_rule.py::KEYWORD_RULES`。

---

## 重要环境变量

| 变量 | 作用 | 示例 |
|---|---|---|
| `AWS_REGION` | Bedrock + Redshift 所在区域 | `ap-northeast-1` |
| `BEDROCK_MODEL_ID` | Claude 模型的 inference profile | `jp.anthropic.claude-sonnet-4-6` |
| `REDSHIFT_HOST` | Serverless workgroup endpoint | `<wg>.<acct>.<region>.redshift-serverless.amazonaws.com` |
| `REDSHIFT_DSN` | Proxy pgx 连 Redshift | `postgres://admin:<pwd>@host:5439/dev?sslmode=require` |
| `PROXY_MYSQL_USER/PASSWORD/DB` | 客户端用这个账号连 Proxy | `demo / <pwd> / dw` |
| `AGENT_URL` | Proxy 找 agent 的地址（compose 内部） | `http://agent:8088` |
| `CACHE_SIZE` | LRU 条目数 | `1024` |
| `MAX_ATTEMPTS` | 执行失败回喂 agent 的最大次数 | `3` |

**敏感值**（`REDSHIFT_PASSWORD` / `PROXY_MYSQL_PASSWORD` / AWS credentials）**只在 `.env` 里配**，
`.gitignore` 已排除。**不要把 `.env` 提交到 git**。

---

## 完整文档

- [DESIGN.md](DESIGN.md) — 事前设计（架构、迁移方案、references 体系、里程碑）
- [REPORT.md](REPORT.md) — 事后总结（完成状态 + 11 个踩坑 + 解决方式）
- [docs/dialect_tests.md](docs/dialect_tests.md) — 10 方言用例 + 结果
- [docs/migration_prod.md](docs/migration_prod.md) — 方案 A vs 方案 B 对比 + 实测数据 + 关键坑（TINYINT→int32、KMS Decrypt）

---

## 已知局限（MVP 之后）

1. **Prepared statement 不支持**（`HandleStmtPrepare` 返回 error），ORM 场景要补
2. **类型映射粗糙**：所有列当字符串回传给 MySQL 客户端
3. **无 cost 熔断** — 生产要接 Bedrock usage 限流
4. **无影子双跑** — 生产建议先和 MySQL 对比一段时间再切流量
5. **Redshift Serverless 冷启动 ~60s** — Proxy 的 60s timeout 偶尔触发；生产拉大或加 wake-up ping
6. **单 agent 容器** — 高并发要改 queue + worker pool

---

## License

MIT
