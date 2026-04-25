#!/bin/bash
# 创建 Aurora MySQL Serverless v2（≥2 AZ 的 subnet group）
set -euo pipefail
cd "$(dirname "$0")" && . ./00_env.sh

# 1. DB Subnet Group
aws rds describe-db-subnet-groups --db-subnet-group-name $AURORA_SUBNET_GROUP --region $REGION >/dev/null 2>&1 || \
aws rds create-db-subnet-group \
  --db-subnet-group-name $AURORA_SUBNET_GROUP \
  --db-subnet-group-description "dw aurora subnet group" \
  --subnet-ids $SUB_1A $SUB_1C $SUB_1D \
  --region $REGION

# 2. 创建 Aurora MySQL cluster (Serverless v2)
aws rds describe-db-clusters --db-cluster-identifier $AURORA_CLUSTER --region $REGION >/dev/null 2>&1 && {
  echo "[aurora] cluster already exists: $AURORA_CLUSTER"
} || {
  echo "[aurora] creating cluster $AURORA_CLUSTER ..."
  aws rds create-db-cluster \
    --db-cluster-identifier $AURORA_CLUSTER \
    --engine aurora-mysql \
    --engine-version 8.0.mysql_aurora.3.08.0 \
    --master-username $AURORA_USER \
    --master-user-password "$AURORA_PASSWORD" \
    --database-name $AURORA_DB \
    --db-subnet-group-name $AURORA_SUBNET_GROUP \
    --vpc-security-group-ids $SG \
    --serverless-v2-scaling-configuration MinCapacity=0.5,MaxCapacity=2 \
    --enable-cloudwatch-logs-exports '["error"]' \
    --region $REGION \
    --output table --query 'DBCluster.[DBClusterIdentifier,Status,Endpoint]' || exit 1
}

# 3. 创建 DB instance（serverless v2 需要一个 db.serverless instance 承载计算）
aws rds describe-db-instances --db-instance-identifier $AURORA_INSTANCE --region $REGION >/dev/null 2>&1 && {
  echo "[aurora] instance already exists: $AURORA_INSTANCE"
} || {
  echo "[aurora] creating instance $AURORA_INSTANCE (db.serverless)..."
  aws rds create-db-instance \
    --db-instance-identifier $AURORA_INSTANCE \
    --db-cluster-identifier $AURORA_CLUSTER \
    --engine aurora-mysql \
    --db-instance-class db.serverless \
    --region $REGION \
    --output table --query 'DBInstance.[DBInstanceIdentifier,DBInstanceStatus]'
}

echo "[aurora] waiting for cluster to become available (may take 5-10 min)..."
aws rds wait db-cluster-available --db-cluster-identifier $AURORA_CLUSTER --region $REGION
aws rds wait db-instance-available --db-instance-identifier $AURORA_INSTANCE --region $REGION

ENDPOINT=$(aws rds describe-db-clusters --db-cluster-identifier $AURORA_CLUSTER --region $REGION --query 'DBClusters[0].Endpoint' --output text)
echo ""
echo "[aurora] ready!"
echo "  endpoint : $ENDPOINT"
echo "  database : $AURORA_DB"
echo "  user     : $AURORA_USER"
echo "  pass     : $AURORA_PASSWORD"
echo ""
echo "AURORA_ENDPOINT=$ENDPOINT" > /tmp/dw_endpoints.env
