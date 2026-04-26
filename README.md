# mysql-redshift-agent

让 **MySQL 客户端零改动**连接 **Amazon Redshift** 查询的代理。客户端看到的是标准 MySQL wire
protocol，Proxy 在 SQL 层按表白名单分流：**命中白名单的大表分析走 Redshift**（由 Claude
Sonnet 4.6 via Bedrock 做 SQL 转换，再通过 pgx 执行），**未命中的查询直连 MySQL 透传**。
结果集统一转回 MySQL 协议返回给客户端。

Agent 内置两层知识系统共同提升 SQL 转换准确率：
1. **Markdown 规则前置**（12 条硬编码正则 → 规则文档）—— 处理 IFNULL、LIMIT a,b 这类
   确定性方言差异
2. **pgvector 知识库召回**（Aurora PG Serverless v2）—— 历史成功样本 few-shot 注入，
   Cohere multilingual v3 做 1024 维 embedding，相似度 ≥0.85 的 top-3 直接贴到 prompt；
   成功翻译 + 结果对比一致后自动回写，**越用越准**

```
 mysql CLI / BI 工具 / ORM
        │  (MySQL wire protocol，零改造)
        ▼
 ┌────────────────────────────────────┐
 │  mysql-redshift-proxy (Go)         │
 │  1. pingcap parser 提表名          │
 │  2. 白名单精确匹配                 │
 │     命中 ─────┐   未命中 ─────┐    │
 └───────────────│────────────────│───┘
                 │                │
                 ▼ 命中            ▼ 未命中
  ┌─────────────────────────────┐   ┌─────────────────────┐
  │ db-convertor-agent (Python) │   │ Aurora MySQL        │
  │ ① Markdown 规则前置         │   │（业务库原样）       │
  │ ② pgvector 召回 top-3 ◀─────┼──┐│  ┃                  │
  │   Cohere multilingual v3    │  ││  ┃ 每月 Snapshot    │
  │ ③ Bedrock Sonnet 4.6 翻译   │  ││  ┃ → Parquet Export │
  │ ④ 成功样本回写 KB ──────────┼──┘│  ┃ → COPY 全量刷新  │
  └──────┬──────────────────────┘   │  ▼                  │
         │ Redshift SQL             │  ═══════════════════╪═══▶ Redshift Serverless
         ▼                          │                     │
   ┌──────────────┐                 └─────────────────────┘
   │ Redshift     │◀────────────────────────────────────────┐
   │ Serverless   │                                         │
   └──────────────┘                                         │
                                                            │
            ┌───────────────────────┐                       │
            │ Aurora PG Serverless  │  pgvector 知识库       │
            │ (MinACU=1, MaxACU=18) │  成功样本回写 + 召回    │
            └───────────────────────┘                       │
```

三个容器（proxy / agent / webui）通过 HTTP 通信，docker-compose 一键起。

- **Proxy (Go)**：收 MySQL 连接 → pingcap parser 提表名 → 白名单路由
  - **命中白名单** → 缓存查 → 问 agent 要 Redshift SQL → pgx 跑 Redshift；执行失败把
    error 回喂 agent 最多 `MaxAttempts=3` 次让它修；成功后 **fire-and-forget 异步回写
    知识库**
  - **未命中** → `database/sql` 直接透传到 Aurora MySQL，不经 agent
  - 结果集统一转 MySQL wire 格式返回
- **Agent (Python)**：`POST /translate`，调 Bedrock Sonnet 4.6 做 MySQL→Redshift SQL 转换
  - **前置 1：Markdown 规则**（12 条正则 → 规则文档，不是 RAG）
  - **前置 2：pgvector 召回**（历史成功样本 few-shot，Cohere multilingual v3 embedding）
  - 还暴露 `/api/knowledge/*` 系列接口供 webui 管理知识库
- **Webui (FastAPI + Alpine.js)**：三数据源 DBMS + **知识库管理页**（批量导入 + 双边执行
  + 结果对比 + 强制入库），访问 `:8081/knowledge`
- **数据同步（月度脚本，不在代码里）**：Aurora 快照 → RDS Export 成 Parquet 落 S3 →
  Redshift `COPY FORMAT AS PARQUET` 刷新目标表；详见 `infra/06_planb_export.sh`

---

## 请求时序（一条命中白名单的查询）

从客户端发 SQL 到结果集返回，只走"同步主链路"；**知识库回写在客户端拿到结果之后
异步完成**，不拖慢响应。

```
客户端                 Proxy                    Agent                pgvector
  │                     │                         │                     │
  │  SELECT ...         │                         │                     │
  ├────────────────────▶│                         │                     │
  │                     │  白名单命中 → 翻译请求   │                     │
  │                     ├────────────────────────▶│                     │
  │                     │                         │  ① 前置规则注入      │
  │                     │                         │  ② 召回 top-3        │
  │                     │                         ├────────────────────▶│
  │                     │                         │◀────────────────────┤
  │                     │                         │  ③ Bedrock 翻译     │
  │                     │  Redshift SQL            │                     │
  │                     │◀────────────────────────┤                     │
  │                     │  pgx 跑 Redshift        │                     │
  │                     │  (rows + 耗时)           │                     │
  │                     │                         │                     │
  │  结果集              │                         │                     │
  │◀────────────────────┤                         │                     │
  │ ────── 客户端已经拿到结果（同步主链路到此结束） ──────────            │
  │                     │                         │                     │
  │                     │  ④ SaveAsync → channel   │                     │
  │                     │        │                │                     │
  │                     │        ▼                │                     │
  │                     │   worker goroutine       │                     │
  │                     │        │  POST /save_example (async)           │
  │                     │        ├────────────────▶│                    │
  │                     │                         │  embed(sql) Cohere   │
  │                     │                         │  UPSERT pgvector ───▶│
```

**关键设计**：
- `SaveAsync` 通过 buffered channel（容量 256）把回写请求交给 worker goroutine，
  调用方纳秒级返回，**不增加客户端延迟**
- Channel 满就丢（宁可偶尔错过一次回写，也不阻塞主链路）
- 召回 + 回写都是增强：Aurora PG 不可用时系统无感降级到"规则 + LLM"

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
│   ├── app.py                    # POST /translate；知识库管理 API
│   ├── requirements.txt
│   ├── tools/
│   │   ├── lookup_dialect_rule.py  # Strands @tool：按关键字查 rule md
│   │   ├── get_table_schema.py     # Strands @tool：查 Redshift 列类型
│   │   ├── embedding.py            # Cohere multilingual v3 客户端（1024 维）
│   │   ├── sql_knowledge.py        # pgvector 召回/回写/CRUD
│   │   ├── compare.py              # 结果集 strict/lenient/skipped 对比
│   │   └── executors.py            # 双边 MySQL/Redshift 执行器（agent 侧用）
│   └── references/                 # 11 个方言 markdown（pre-filter 的知识源）
│
├── proxy/                          # Go + go-mysql-org v1.14.0 + pgx v5 + pingcap parser
│   ├── Dockerfile
│   ├── go.mod / go.sum
│   ├── cmd/proxy/main.go           # 启动入口
│   └── internal/
│       ├── config/                 # FromEnv()
│       ├── router/                 # pingcap parser 提表名 + 白名单路由
│       ├── cache/                  # LRU + SQL normalize (md5)
│       ├── convertor_client/       # HTTP 调 agent /translate
│       ├── knowledge/              # HTTP 调 agent /save_example（fire-and-forget）
│       ├── executor/               # Redshift pgx + MySQL database/sql 执行器
│       ├── resultmap/              # executor.Result → gomysql.Result
│       └── server/                 # MySQL wire server + connHandler
│
├── webui/                          # FastAPI + Alpine.js DBMS + 知识库管理
│   ├── Dockerfile
│   ├── app.py                      # 三数据源执行 + KB 管理代理
│   ├── requirements.txt
│   └── static/
│       ├── index.html              # DBMS 主页
│       ├── knowledge.html          # 🧠 知识库管理页（批量导入 + 双边对比 + 强制入库）
│       └── report.html             # 📊 测试对比报告
│
├── infra/                          # AWS CLI 一次性资源脚本
│   ├── 00_env.sh                   # 公共环境变量（已占位化，使用前填值）
│   ├── 01_create_aurora.sh         # Aurora MySQL Serverless v2
│   ├── 02_create_redshift.sh       # Redshift Serverless namespace+workgroup
│   ├── 03_create_s3.sh             # 迁移 bucket
│   ├── 04_sg_self_ref.sh           # SG 自引用校验
│   ├── 05_create_redshift_role.sh  # redshift-copy-role (S3/KMS access)
│   ├── 06_planb_export.sh          # 方案 B：snapshot → Parquet export（一键）
│   └── 07_create_aurora_pg.sh      # Aurora PG Serverless v2（pgvector 知识库后端）
│
├── scripts/
│   ├── schema.sql                  # MySQL DDL
│   ├── schema_redshift.sql         # Redshift DDL（含 iap_orders_5000w 50M 表）
│   ├── schema_pgvector.sql         # Aurora PG sql_knowledge 表 + HNSW + md5 去重索引
│   ├── seed_knowledge.py           # 用 6 条已验证样本预热知识库（source=seed）
│   ├── xlsx_to_csv.py              # xlsx → TSV/CSV
│   ├── load_aurora.sh              # Aurora 导入
│   ├── gen_50m.py / load_50m.sh    # 生成 5000w 行 IAP 订单数据（压测用）
│   ├── migrate_simple.py           # 方案 A：Aurora → S3 CSV → Redshift COPY
│   └── dialect_tests.sh            # 10 个方言测试用例
│
└── docs/
    ├── dialect_tests.md            # 10 方言用例结论
    ├── migration_prod.md           # 生产迁移两方案对比 + 坑点（Parquet/KMS/类型）
    └── deploy.md                    # 通用部署指南（默认 $REGION=us-west-2，东京 demo 为 ap-northeast-1）
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

- `POST /translate` 接受 `{sql, prev_error?, prev_sql?}`，返回
  `{redshift_sql, used_rules, latency_ms, attempt, examples_hit, examples}`
- 第一次翻译：`attempt=initial`；Proxy 侧执行失败后带 `prev_error` 和 `prev_sql` 再来：`attempt=fix`
- **前置 1 · Pre-filter（规则文档）**：正则扫 SQL，命中的方言 rule markdown 直接拼进
  prompt（12 条硬编码关键字，完全确定性，无幻觉）
- **前置 2 · pgvector 召回（few-shot）**：`embed(sql)` → Aurora PG 向量检索 top-3 相似度
  ≥0.85 的历史成功样本 → 格式化成 few-shot 贴到 prompt；`examples_hit` 在响应里可见
- **后置 · 回写**：`POST /save_example` 把 Proxy 真正执行成功的 SQL 存回 pgvector（由
  Proxy 异步调用，fire-and-forget）；相同 `md5(mysql_sql)` 会 UPSERT 去重
- **知识库管理 API**：`GET /api/knowledge`、`DELETE /api/knowledge/{id}`、
  `POST /api/knowledge/import_test`（翻译+双边执行+结果对比+入库，Webui 批量导入用）
- **extract_sql()**：Agent 在用 tool 之后有时会夹带思考性 prose，用正则从 `SELECT|WITH|INSERT|…` 起截断
- Strands `BedrockModel` 必须**显式传 boto3 session** 才能正确绑定 region，**不能**同时传 `region_name` 和 `boto_session`

### Proxy（`proxy/`）

- `cmd/proxy/main.go` 启动入口
- `internal/server/server.go` 实现 go-mysql-org/go-mysql 的 `Handler`。只支持 `HandleQuery`（一次性查询）；`HandleStmtPrepare` 返回 error（prepared statement 暂不支持）
- `internal/router/router.go` pingcap parser 提表名 + 白名单路由（命中→Redshift，未命中→MySQL 透传）
- `internal/cache/cache.go` SQL 归一化（小写 + literal→?）+ md5 作 LRU key
- `internal/server/server.go::execWithRetry` 执行 + 失败回喂 agent，`MaxAttempts=3`；
  返回 `(result, winningSQL, elapsedMs, err)`，winningSQL 是真正跑通那版（用于回写）
- `internal/knowledge/client.go` buffered-channel worker，异步调 agent `/save_example`
  把成功的 MySQL→Redshift 翻译写进知识库（fire-and-forget）
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

### pgvector 知识库（`agent/tools/sql_knowledge.py` + `scripts/schema_pgvector.sql`）

存储后端：**Aurora PostgreSQL Serverless v2**（MinACU=1, MaxACU=18，复用 MySQL 的
VPC/SG/subnet group，内网直通）。

表结构：
```sql
sql_knowledge (
  id, mysql_sql, redshift_sql,
  embedding vector(1024),      -- Cohere multilingual v3
  used_rules TEXT[],           -- 命中的规则文件名，便于排障
  row_count, mysql_ms, redshift_ms,
  compare_mode,                -- strict / lenient / skipped / override
  source,                      -- runtime / import / seed
  hit_count, created_at, last_used_at
)
+ HNSW index on embedding (cosine)
+ UNIQUE index on md5(mysql_sql) （upsert 去重）
```

**写入路径**：
- Proxy 运行时成功 → `Client.SaveAsync()` → `POST /save_example`（source=runtime）
- Webui 批量导入 → `POST /api/knowledge/import_test` → 翻译+双边执行+对比 → 通过才写
  （source=import；对比失败可 force_save，标记 source=import + compare_mode=override）
- 种子 → `scripts/seed_knowledge.py`（source=seed）

**写入筛选**（runtime 源默认生效；seed/import 绕过）：
1. **价值密度过滤** —— 满足以下任一条件则 skip：
   - `row_count == 0`（空结果，可能是错的过滤或空表）
   - `redshift_sql` 长度 < 30（`SELECT 1` 之类调试 SQL）
   - 以 `SET/SHOW/USE/BEGIN/COMMIT/ROLLBACK` 开头（管理命令）
2. **向量去重**（`KNOWLEDGE_DEDUP_THRESHOLD`，默认 0.94）—— 与库里最相似一条的
   cosine 相似度 ≥ 阈值时，只 bump `hit_count` + `last_used_at`，不新增行。
   实测数据定阈值：
   - 同模板改 uid/时间/LIMIT：0.93–0.95（**应 dedup**）
   - 改 ORDER BY 字段：0.94（**应保留**）
   - `NOT IN` 改 `IN` 或白名单换不同值：0.91（**应保留**）
   - 完全不同聚合 SQL：0.60（显然保留）
   
   0.94 是"压重复 + 留业务变化"的拐点。

**召回路径**：
- Agent `/translate` 每次调用前 `embed(sql, search_query)` → 查 top-3 → 相似度
  `< KNOWLEDGE_THRESHOLD`（默认 0.85）过滤 → 命中的贴进 prompt 的 `examples_blob`
- 召回后同步 `UPDATE ... SET hit_count=hit_count+1, last_used_at=NOW()`，便于后续按
  热度淘汰

---

## 重要环境变量

| 变量 | 作用 | 示例 |
|---|---|---|
| `AWS_REGION` | Bedrock + Redshift 所在区域 | `ap-northeast-1` |
| `BEDROCK_MODEL_ID` | Claude 模型的 inference profile（推荐 `global.` 跨区） | `global.anthropic.claude-sonnet-4-6` |
| `EMBEDDING_MODEL_ID` | pgvector 知识库的 embedding 模型 | `cohere.embed-multilingual-v3` |
| `REDSHIFT_HOST` | Serverless workgroup endpoint | `<wg>.<acct>.<region>.redshift-serverless.amazonaws.com` |
| `REDSHIFT_DSN` | Proxy pgx 连 Redshift | `postgres://admin:<pwd>@host:5439/dev?sslmode=require` |
| `AURORA_HOST` / `AURORA_USER` / `AURORA_PASSWORD` / `AURORA_DB` | Aurora MySQL（业务源 + webui 直连 + agent 双边对比） | `fpdw-aurora-cluster...amazonaws.com` |
| `MYSQL_DSN` | Proxy 透传到 MySQL 用的 go-sql-driver DSN | `admin:pwd@tcp(host:3306)/fpdw?parseTime=true` |
| `TABLE_WHITELIST` | Redshift 分流白名单（逗号分隔） | `iap_orders_5000w,ads_thor_fin_payment_iap_data_new` |
| `AURORA_PG_HOST` / `AURORA_PG_USER` / `AURORA_PG_PASSWORD` / `AURORA_PG_DB` | Aurora PG Serverless（pgvector 知识库） | `fpdw-pg-cluster...amazonaws.com` / `dbadmin` / `knowledge` |
| `KNOWLEDGE_TOP_K` | 每次召回条数 | `3` |
| `KNOWLEDGE_THRESHOLD` | 召回余弦相似度阈值（低于不贴 prompt） | `0.85` |
| `KNOWLEDGE_DEDUP_THRESHOLD` | 写入去重阈值（高于只 bump hit_count） | `0.94` |
| `PROXY_MYSQL_USER/PASSWORD/DB` | 客户端用这个账号连 Proxy | `demo / <pwd> / fpdw` |
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
- [docs/deploy.md](docs/deploy.md) — 通用部署指南（`$REGION` 参数化，默认 us-west-2）

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
