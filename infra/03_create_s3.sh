#!/bin/bash
# 创建 S3 bucket 用于 Aurora→Redshift 数据迁移中转
set -euo pipefail
cd "$(dirname "$0")" && . ./00_env.sh

aws s3api head-bucket --bucket $S3_BUCKET --region $REGION 2>/dev/null && {
  echo "[s3] bucket exists: $S3_BUCKET"
  exit 0
}

echo "[s3] creating bucket $S3_BUCKET ..."
aws s3api create-bucket \
  --bucket $S3_BUCKET \
  --region $REGION \
  --create-bucket-configuration LocationConstraint=$REGION \
  --output table

# 开启版本管理 + 服务端加密
aws s3api put-bucket-versioning --bucket $S3_BUCKET --versioning-configuration Status=Enabled --region $REGION
aws s3api put-bucket-encryption --bucket $S3_BUCKET --region $REGION \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

echo "[s3] ready: s3://$S3_BUCKET"
