# 生产级数据迁移（Aurora MySQL → Redshift）

两条路线：**方案 A（Python pipe）** 用于 demo/小表；**方案 B（RDS Export → Parquet → Redshift COPY）** 用于生产全量同步。

---

## 方案 A：Python 脚本

路径：`scripts/migrate_simple.py`

```
pymysql.SELECT * → CSV → boto3.upload_fileobj → S3 → psycopg2 COPY FROM s3://
```

| 项 | 说明 |
|---|---|
| 路径 | `mysql-redshift-proxy/scripts/migrate_simple.py` |
| 依赖 | pymysql, boto3, psycopg2-binary |
| 认证 | EC2 instance role（含 Redshift `IAM_ROLE` 权限）+ RDS 用户名密码 |
| 类型映射 | 手工在 `scripts/schema_redshift.sql` 维护：`TINYINT→SMALLINT`，`DATETIME→TIMESTAMP`，`DECIMAL(12,4)` 保留，VARCHAR 长度按源拷 |
| 时长 | 100 行 demo：< 2 秒。1 亿行估算 > 30 min（受 CSV 编解码 + 单文件上传限速）|
| 适用 | 仅用于 demo/冷启动；**不建议生产使用** |

### 已知坑
- 单 CSV 无压缩，S3 上行慢；大表需手动分片
- `decimal.Decimal` 转 CSV 时 Python 默认变成科学计数法，要 `str()` 强转
- Redshift `COPY` 要加 `DELIMITER ',' CSV IGNOREHEADER 1 TIMEFORMAT 'YYYY-MM-DD HH:MI:SS'`

---

## 方案 B：RDS Snapshot Export → Parquet → Redshift COPY

### 快速入口

本仓库提供了**一键脚本** `infra/06_planb_export.sh`：自动建 IAM role / KMS CMK / snapshot / export
task 并轮询到 COMPLETE。全部跑完再人工执行 Redshift COPY 就行。

```bash
# 前置：先 source .env 和 00_env.sh
set -a && . ./.env && set +a
. infra/00_env.sh

bash infra/06_planb_export.sh
# [1/5] IAM role
# [2/5] KMS key
# [3/5] Aurora cluster snapshot → wait ~3min
# [4/5] start RDS export task
# [5/5] poll → 一直到 COMPLETE 100%
# Next: run COPY on Redshift:
#   COPY public.<table> FROM 's3://...' IAM_ROLE '...' FORMAT AS PARQUET;
```

### 手工分解步骤

1. **Aurora 快照**
   ```bash
   aws rds create-db-cluster-snapshot \
       --db-cluster-identifier dw-aurora-cluster \
       --db-cluster-snapshot-identifier dw-snap-<ts>
   ```
   耗时：demo cluster ~3 min（8 RPU / ~100 行）；生产 500 GB 估 10–30 min

2. **IAM role for RDS export**（一次性）
   ```
   assume: export.rds.amazonaws.com
   inline: s3:Put/Get/List/DeleteObject* on $S3_BUCKET/*
   ```

3. **KMS CMK**（RDS Export 强制要求客户管理的 KMS，不能用 `aws/s3` 默认密钥）
   ```bash
   aws kms create-key --description "dw RDS export key"
   aws kms create-alias --alias-name alias/dw-export-key --target-key-id <id>
   ```

4. **启动 Export Task**
   ```bash
   aws rds start-export-task \
       --export-task-identifier dw-parquet-<ts> \
       --source-arn arn:aws:rds:$REGION:$ACCOUNT:cluster-snapshot:<snap-id> \
       --s3-bucket-name $S3_BUCKET \
       --s3-prefix planb-export/ \
       --iam-role-arn arn:aws:iam::$ACCOUNT:role/dw-rds-export-role \
       --kms-key-id <cmk-id>
   ```

5. **S3 Parquet 输出结构**
   ```
   s3://$S3_BUCKET/planb-export/<export-id>/<cluster-name>/<db>.<table>/<part>-<n>.parquet
   ```
   所有表各自一个前缀，每个 part 是 Snappy 压缩的 Parquet，天然分区（~256 MB/file），比方案 A 小一个数量级。

6. **Redshift 侧 DDL**（每张表一次，手工或脚本生成）
   — 同方案 A，见 `scripts/schema_redshift.sql`；Parquet 列顺序 = 源 MySQL 建表顺序。

7. **Redshift 侧角色授权 KMS Decrypt**（一次性）
   ```bash
   # 找到 Redshift Serverless 关联的 IAM role（或你自己指定的 COPY role）
   ROLE_NAME=$(aws redshift-serverless get-namespace \
       --namespace-name "$RS_NAMESPACE" --region "$REGION" \
       --query 'namespace.iamRoles[0]' --output text \
       | sed 's/.*iamRoleArn=\(.*\))/\1/' | awk -F/ '{print $NF}')

   KMS_ARN=$(aws kms describe-key --key-id alias/dw-export-key \
       --region "$REGION" --query KeyMetadata.Arn --output text)

   aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name KMSDecrypt \
       --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"kms:Decrypt\",\"kms:DescribeKey\"],\"Resource\":\"$KMS_ARN\"}]}"
   ```

8. **Redshift COPY**（按表）
   ```sql
   COPY public.<your_table>
   FROM 's3://<S3_BUCKET>/planb-export/<export-id>/<cluster-name>/dw.<your_table>/'
   IAM_ROLE 'arn:aws:iam::<ACCOUNT-ID>:role/redshift-copy-role'
   FORMAT AS PARQUET;
   ```
   — 不用指定 header / delimiter，列按名匹配。
   — ⚠️ 如果报 `UnauthorizedException error_type 138` → 回到第 7 步检查 KMS Decrypt 权限。
   — ⚠️ 如果报 `Spectrum Scan Error. incompatible Parquet schema` → 看第 5 条踩坑（类型宽度要对齐）。

### 实测时间（2026-04-25 demo cluster / 100 行 / 单表，**全链路跑通**）

| 步骤 | 耗时 | 备注 |
|---|---|---|
| Aurora cluster snapshot | ~3 min | 固定开销，跟数据量几乎无关 |
| RDS Export task (STARTING) | ~7 min | RDS 在后台拉起临时集群，100 行也是这个时间 |
| RDS Export task (IN_PROGRESS → COMPLETE) | ~1.5 min | 从 IN_PROGRESS 到 100%，这步受数据量影响 |
| **Export 总计** | **~8.5 min** | demo 场景固定起步时间 |
| S3 Parquet 输出 | 3 个 part-*.gz.parquet（~9 KB 每个） + `_SUCCESS` | MySQL 100 行被切成 3 片 |
| Redshift COPY | **2.05 s** | 100 行，3 个 Parquet 文件并行 load |

实验产物：
- 快照 ID：`dw-snap-20260425-0437`
- Export ID：`dw-parquet-20260425-0440`
- S3 路径：`s3://<S3_BUCKET>/planb-export/dw-parquet-20260425-0440/`
- Redshift 表：`public.planb_iap_data` (100 行，列对齐成功)

### 踩坑点（本次实测）
1. **KMS 必须自己建 CMK**。用 `aws/s3` AWS 托管密钥会报 `KMSKeyNotFoundFault` 或 `The specified KMS key is not customer-managed`
2. **Snapshot 是 DB Cluster Snapshot**（Aurora），不是 DB Instance Snapshot。source-arn 前缀是 `cluster-snapshot` 而非 `snapshot`
3. **Export role 的 Trust Policy** 必须是 `export.rds.amazonaws.com`，不是 `rds.amazonaws.com`，否则 `start-export-task` 会说 role 不能被 assume
4. **S3 bucket 同 region**。跨 region 会报 `The specified S3 bucket is not in the same AWS Region as the snapshot`
5. **⚠️ TINYINT → int32 → Redshift 要用 INTEGER 不是 SMALLINT**。实测报错：
   > Spectrum Scan Error. incompatible Parquet schema for column 'device_level'. Column type: SMALLINT, Parquet schema: optional int32
   
   即便源 MySQL 是 `TINYINT(1)`（1 字节），RDS Export 也把它导成 Parquet `int32`（4 字节），Redshift COPY PARQUET 模式只做**等宽匹配**，不自动窄化。所以 Redshift DDL 里凡是源 `TINYINT/SMALLINT/INT`，一律写 **`INTEGER`**。长整型 `BIGINT` → `BIGINT` 对齐。
   
   这是方案 A（CSV 文本）和方案 B（Parquet 二进制）的**关键差异**：CSV 是字符串，所有类型转换都发生在 Redshift 侧；Parquet 是强类型，类型必须精确对齐。
6. **Redshift IAM role 要 KMS Decrypt 权限**。Parquet 文件是用 CMK 加密的，Redshift COPY 会被 `UnauthorizedException error_type 138` 拒绝。给 `redshift-copy-role` 加 `kms:Decrypt`, `kms:DescribeKey` inline policy 即可（CMK default policy 已经允许账户 root，所以不用改 KMS key policy）。
7. **`stl_load_errors` 不可见**：Redshift Serverless 看错误详情要查 `sys_load_error_detail`，旧的 `stl_` 视图返回权限错误
8. **增量同步不行**：RDS Export 一次只能导整个 snapshot，不支持 CDC。要增量请走 DMS + Kinesis 或 Aurora Zero-ETL → Redshift

### 小结
| 维度 | 方案 A（Python） | 方案 B（RDS Export Parquet）|
|---|---|---|
| 启动开销 | 秒级 | **固定 15–25 min**（snapshot + export task） |
| 吞吐 | 单文件 CSV，慢 | Parquet + S3 多 part 并行，快 |
| 类型映射 | 手工 schema_redshift.sql（CSV 是字符串，Redshift 自动 cast） | **必须改 DDL**（TINYINT/SMALLINT → INTEGER，因为 Parquet int32 要等宽） |
| 增量 | 支持（WHERE event_time > 上次） | 不支持，需要 CDC 方案 |
| 生产建议 | 只用在冷启动小表 | 大表全量/首次迁移首选；日常用 Aurora Zero-ETL |
