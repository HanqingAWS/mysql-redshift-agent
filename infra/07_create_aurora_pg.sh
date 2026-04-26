#!/bin/bash
# 创建 Aurora PostgreSQL Serverless v2（作为 pgvector 知识库后端）
# 复用 Aurora MySQL 已有的 subnet group / SG，零网络配置
set -euo pipefail
cd "$(dirname "$0")" && . ./00_env.sh

# ——— 配置 ———
PG_CLUSTER=${PG_CLUSTER:-fpdw-pg-cluster}
PG_INSTANCE=${PG_INSTANCE:-fpdw-pg-instance}
PG_DB=${PG_DB:-knowledge}
PG_USER=${PG_USER:-dbadmin}   # "admin" is reserved in PostgreSQL
PG_PASSWORD=${PG_PASSWORD:-$AURORA_PASSWORD}   # 复用 Aurora MySQL 密码
PG_SUBNET_GROUP=${PG_SUBNET_GROUP:-fpdw-aurora-subnet-group}   # 复用
PG_SG=${PG_SG:-sg-0dbb9d5bcd5e199f4}                           # 复用

# ——— 1. Cluster ———
if aws rds describe-db-clusters --db-cluster-identifier $PG_CLUSTER --region $REGION >/dev/null 2>&1; then
  echo "[pg] cluster already exists: $PG_CLUSTER"
else
  echo "[pg] creating cluster $PG_CLUSTER ..."
  aws rds create-db-cluster \
    --db-cluster-identifier $PG_CLUSTER \
    --engine aurora-postgresql \
    --engine-version 16.4 \
    --master-username $PG_USER \
    --master-user-password "$PG_PASSWORD" \
    --database-name $PG_DB \
    --db-subnet-group-name $PG_SUBNET_GROUP \
    --vpc-security-group-ids $PG_SG \
    --serverless-v2-scaling-configuration MinCapacity=1,MaxCapacity=18 \
    --region $REGION \
    --output table --query 'DBCluster.[DBClusterIdentifier,Status,Endpoint]'
fi

# ——— 2. Instance ———
if aws rds describe-db-instances --db-instance-identifier $PG_INSTANCE --region $REGION >/dev/null 2>&1; then
  echo "[pg] instance already exists: $PG_INSTANCE"
else
  echo "[pg] creating instance $PG_INSTANCE (db.serverless)..."
  aws rds create-db-instance \
    --db-instance-identifier $PG_INSTANCE \
    --db-cluster-identifier $PG_CLUSTER \
    --engine aurora-postgresql \
    --db-instance-class db.serverless \
    --region $REGION \
    --output table --query 'DBInstance.[DBInstanceIdentifier,DBInstanceStatus]'
fi

echo "[pg] waiting for cluster to become available (5-10 min)..."
aws rds wait db-cluster-available --db-cluster-identifier $PG_CLUSTER --region $REGION
aws rds wait db-instance-available --db-instance-identifier $PG_INSTANCE --region $REGION

ENDPOINT=$(aws rds describe-db-clusters --db-cluster-identifier $PG_CLUSTER --region $REGION --query 'DBClusters[0].Endpoint' --output text)
echo ""
echo "[pg] ready!"
echo "  endpoint : $ENDPOINT"
echo "  database : $PG_DB"
echo "  user     : $PG_USER"
echo ""
echo "# Append to .env:"
echo "AURORA_PG_HOST=$ENDPOINT"
echo "AURORA_PG_PORT=5432"
echo "AURORA_PG_DB=$PG_DB"
echo "AURORA_PG_USER=$PG_USER"
echo "AURORA_PG_PASSWORD=$PG_PASSWORD"
