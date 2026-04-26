# 部署指南

> 本项目的**生产目标区是 us-west-2**。当前 demo 在 ap-northeast-1（东京）跑通，
> 迁到任何其他 region 的流程都是本文档的"一组命令里换一个 `$REGION`"。
>
> 下文用 `$REGION` 作为 region 占位符。**默认取 `us-west-2`**。在东京 demo 里
> 把 `$REGION` 读作 `ap-northeast-1` 即可。

## 一句话总结

**所有 AWS 侧资源（Aurora MySQL / Aurora PG / Redshift / S3 / KMS / IAM Role）在
`$REGION` 中创建一次；环境变量把 region、endpoint、DSN 换成新区域的值。Bedrock
Claude 和 Cohere embedding 的模型 ID 不用改**（前者用 `global.` 前缀跨区推理，
后者由 boto3 按当前 `AWS_REGION` 自动路由）。

---

## 1. 设定目标区域

先在 shell 里导出目标区。后续所有命令都读这个变量。

```bash
export REGION=us-west-2          # ← 默认；东京 demo 时设为 ap-northeast-1
export ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
```

## 2. 基础设施依次创建

`infra/00_env.sh` 已经用 `${REGION:-ap-northeast-1}` 等方式参数化了，**只要
export REGION 就能换区**。

```bash
cd infra
. ./00_env.sh                    # 读环境变量
bash 01_create_aurora.sh         # Aurora MySQL Serverless v2
bash 02_create_redshift.sh       # Redshift Serverless workgroup
bash 03_create_s3.sh             # S3 migration bucket
bash 04_sg_self_ref.sh           # 自引用 SG（内网全通）
bash 05_create_redshift_role.sh  # redshift-copy-role（含 KMS Decrypt 权限）
bash 07_create_aurora_pg.sh      # Aurora PostgreSQL（pgvector 知识库）
# 数据迁移时再跑
bash 06_planb_export.sh          # 方案 B：snapshot → Parquet export
```

**注意**：
- `$REGION` **至少 3 个 AZ 的子网**（Redshift Serverless 要求）
- 走 CloudFront 公网暴露 webui 时，SG 要放行 CloudFront prefix list 访问 8081

## 3. 不需要改的东西

### 3.1 Bedrock Claude 模型 ID

```
BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-4-6
```

`global.` 前缀自动就近路由：`$REGION` 调用 → `$REGION` 附近的推理端点。**一个 ID
所有区通用**。

> 成本提示：`global.*` 比区域专属 ID（`us.*` / `jp.*`）贵 ~10%。量大时可评估切
> 区域专属；量小时 global 最省心。

### 3.2 Cohere embedding 模型 ID

```
EMBEDDING_MODEL_ID=cohere.embed-multilingual-v3
```

Cohere 没有跨区变体，但 boto3 SDK 自动用 `AWS_REGION` 指向当前 region 的端点。在
`$REGION` 的 EC2 上，这个 ID 自然调 `$REGION` 的 Cohere 推理。**代码零改动**。

### 3.3 三个容器镜像

业务代码与 region 无关，直接 `docker compose build` 重建即可，或从 ECR 拉现成镜像。

## 4. .env 改动点

把值里的 region / endpoint 换成 `$REGION` 对应的新值。下面 diff 展示从东京 demo
改到默认生产 `us-west-2`：

```diff
- AWS_REGION=ap-northeast-1
+ AWS_REGION=us-west-2

- REDSHIFT_HOST=<workgroup>.<account>.ap-northeast-1.redshift-serverless.amazonaws.com
+ REDSHIFT_HOST=<workgroup>.<account>.us-west-2.redshift-serverless.amazonaws.com

- AURORA_HOST=<cluster>.cluster-xxxxx.ap-northeast-1.rds.amazonaws.com
+ AURORA_HOST=<cluster>.cluster-xxxxx.us-west-2.rds.amazonaws.com

- AURORA_PG_HOST=<pg-cluster>.cluster-xxxxx.ap-northeast-1.rds.amazonaws.com
+ AURORA_PG_HOST=<pg-cluster>.cluster-xxxxx.us-west-2.rds.amazonaws.com

- S3_BUCKET=<your-bucket>-<account>-ap-northeast-1
+ S3_BUCKET=<your-bucket>-<account>-us-west-2
```

其他变量（用户名 / 密码 / port / DB name）保持原值。**Bedrock / Cohere 模型 ID
不动**。

## 5. 数据迁移预案

### 方案 A：冷迁移（推荐新区建 demo）

重跑 `scripts/load_aurora.sh` 在 `$REGION` 导入源数据，再走 `infra/06_planb_export.sh`
把 Aurora → Parquet → Redshift COPY 一遍。全新全量。

### 方案 B：跨区拷贝（已有 region 有数据要搬）

1. Aurora MySQL：跨区 read replica → promote → 接入
2. Redshift：在源区 `UNLOAD` 到跨区 S3 → `$REGION` 的 Redshift `COPY`
3. S3：`aws s3 sync` 跨区复制
4. Aurora PG（知识库）：`pg_dump | pg_restore` 跨区（量小，几秒钟）

## 6. IAM 角色

- `redshift-copy-role` 在 `$REGION` 重建，trust policy 改为允许该区 Redshift 假设
- KMS key 在 `$REGION` 重建；Redshift role 上授 `kms:Decrypt` 这一批权限
- EC2 instance role 用账户级别，跨区复用即可

## 7. 验证清单

上线前至少跑完：

```bash
# 1. 容器健康
curl http://localhost:8088/healthz     # agent
curl http://localhost:8081/healthz     # webui
mysql -h 127.0.0.1 -P 3307 -u demo -p  # proxy

# 2. 方言回归（10 条）
bash scripts/dialect_tests.sh

# 3. 知识库空库召回
#    清空 pgvector 表 → 跑 dialect_tests → 应看 examples_hit=0
#    第二遍再跑 → 应看 examples_hit > 0

# 4. 双边对比流程
#    打开 /dbms/knowledge，批量导入 3 条 SQL
#    观察 MySQL vs Redshift 结果对齐
```

## 8. 切区当天的操作（如果从东京切到 us-west-2）

约 2 小时窗口：

```
T+0:00    冻结 MySQL 写入（维护窗口）
T+0:15    触发 Aurora snapshot 跨区复制
T+0:45    等 Snapshot 在 $REGION available
T+1:00    run infra/06_planb_export.sh 启动 Parquet export
T+1:30    Redshift COPY
T+1:45    切 DNS 指向 $REGION 的 proxy 端点
T+2:00    验证客户端查询、开放写入
```

## 9. 成本预估

us-west-2 和东京价格差 ±5%，主要成本项（demo 量级）：

| 服务 | 月成本 |
|---|---|
| Aurora MySQL Serverless v2 | $20–40 |
| Aurora PostgreSQL Serverless v2 (MinACU=1) | $80 |
| Redshift Serverless (8 RPU, 按秒计) | $10–30 |
| S3 | < $1 |
| Bedrock Sonnet (global) | $5–20 |
| Bedrock Cohere embedding | < $1 |
| **合计** | **$120–180 / 月** |

## 10. 常见坑点

1. **Aurora PG MinACU 配置**：≥0.5 才不会 scale-to-zero 冷启动；本方案 MinACU=1
   就是为了避免首查 10-15s 延迟
2. **Parquet 类型坑**：TINYINT/SMALLINT → Redshift DDL 用 INTEGER（详见
   `docs/migration_prod.md`）
3. **KMS Decrypt**：Redshift COPY Parquet 需要把 KMS key 授权给 `redshift-copy-role`
4. **CDN origin**：切区后走原 CloudFront，origin DomainName 改成新 EC2
5. **Bedrock 跨区价格**：`global.*` 前缀比区域专属贵 ~10%，量大时可评估切专区
6. **Embedding 成本**：量小可忽略；如果翻译 QPS 很高，考虑本地跑 `bge-small-zh` 替
   代 Bedrock Cohere
