#!/bin/bash
# 创建 Redshift Serverless (namespace + workgroup)
set -euo pipefail
cd "$(dirname "$0")" && . ./00_env.sh

# 1. Namespace（逻辑容器：DB、admin 账号、加密等）
aws redshift-serverless get-namespace --namespace-name $RS_NAMESPACE --region $REGION >/dev/null 2>&1 && {
  echo "[redshift] namespace exists: $RS_NAMESPACE"
} || {
  echo "[redshift] creating namespace $RS_NAMESPACE ..."
  aws redshift-serverless create-namespace \
    --namespace-name $RS_NAMESPACE \
    --admin-username $RS_USER \
    --admin-user-password "$RS_PASSWORD" \
    --db-name $RS_DB \
    --region $REGION \
    --output table --query 'namespace.[namespaceName,status]'
}

# 2. Workgroup（物理资源：容量、网络、SG）
aws redshift-serverless get-workgroup --workgroup-name $RS_WORKGROUP --region $REGION >/dev/null 2>&1 && {
  echo "[redshift] workgroup exists: $RS_WORKGROUP"
} || {
  echo "[redshift] creating workgroup $RS_WORKGROUP ..."
  aws redshift-serverless create-workgroup \
    --workgroup-name $RS_WORKGROUP \
    --namespace-name $RS_NAMESPACE \
    --base-capacity 8 \
    --subnet-ids $SUB_1A $SUB_1C $SUB_1D \
    --security-group-ids $SG \
    --publicly-accessible \
    --region $REGION \
    --output table --query 'workgroup.[workgroupName,status]'
    # publicly-accessible=true 仅用于 demo，生产应关闭
}

echo "[redshift] waiting for workgroup to become available..."
while true; do
  STATUS=$(aws redshift-serverless get-workgroup --workgroup-name $RS_WORKGROUP --region $REGION --query 'workgroup.status' --output text)
  [ "$STATUS" = "AVAILABLE" ] && break
  echo "  status: $STATUS (sleeping 20s)"
  sleep 20
done

ENDPOINT=$(aws redshift-serverless get-workgroup --workgroup-name $RS_WORKGROUP --region $REGION --query 'workgroup.endpoint.address' --output text)
PORT=$(aws redshift-serverless get-workgroup --workgroup-name $RS_WORKGROUP --region $REGION --query 'workgroup.endpoint.port' --output text)
echo ""
echo "[redshift] ready!"
echo "  endpoint : $ENDPOINT:$PORT"
echo "  database : $RS_DB"
echo "  user     : $RS_USER"
echo "  pass     : $RS_PASSWORD"
echo ""
echo "REDSHIFT_ENDPOINT=$ENDPOINT" >> /tmp/dw_endpoints.env
echo "REDSHIFT_PORT=$PORT" >> /tmp/dw_endpoints.env
