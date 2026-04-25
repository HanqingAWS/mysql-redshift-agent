#!/usr/bin/env bash
# 方案 B：Aurora snapshot → S3 Parquet export
# 依赖：00_env.sh 的 REGION / AURORA_CLUSTER / S3_BUCKET
set -eu
cd "$(dirname "$0")"
. ./00_env.sh

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ROLE_NAME=dw-rds-export-role
ROLE_ARN="arn:aws:iam::$ACCOUNT:role/$ROLE_NAME"
KEY_ALIAS=alias/dw-export-key

echo "[1/5] IAM role"
if ! aws iam get-role --role-name "$ROLE_NAME" > /dev/null 2>&1; then
    aws iam create-role --role-name "$ROLE_NAME" \
        --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"export.rds.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
        > /dev/null
fi
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name s3-access \
    --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject*\",\"s3:ListBucket\",\"s3:GetObject*\",\"s3:DeleteObject*\",\"s3:GetBucketLocation\"],\"Resource\":[\"arn:aws:s3:::$S3_BUCKET\",\"arn:aws:s3:::$S3_BUCKET/*\"]}]}"

echo "[2/5] KMS key"
if ! aws kms describe-key --key-id "$KEY_ALIAS" --region "$REGION" > /dev/null 2>&1; then
    KID=$(aws kms create-key --description "dw RDS export key" --region "$REGION" --query KeyMetadata.KeyId --output text)
    aws kms create-alias --alias-name "$KEY_ALIAS" --target-key-id "$KID" --region "$REGION"
fi
KMS_ID=$(aws kms describe-key --key-id "$KEY_ALIAS" --region "$REGION" --query KeyMetadata.KeyId --output text)

echo "[3/5] Aurora cluster snapshot"
SNAP_ID="dw-snap-$(date +%Y%m%d-%H%M)"
aws rds create-db-cluster-snapshot --db-cluster-identifier "$AURORA_CLUSTER" \
    --db-cluster-snapshot-identifier "$SNAP_ID" --region "$REGION" --query 'DBClusterSnapshot.Status' --output text
while true; do
    st=$(aws rds describe-db-cluster-snapshots --db-cluster-snapshot-identifier "$SNAP_ID" --region "$REGION" --query 'DBClusterSnapshots[0].Status' --output text 2>/dev/null)
    echo "   snap status=$st"
    [ "$st" = "available" ] && break
    sleep 20
done

echo "[4/5] start RDS export task"
EXPORT_ID="dw-parquet-$(date +%Y%m%d-%H%M)"
aws rds start-export-task \
    --export-task-identifier "$EXPORT_ID" \
    --source-arn "arn:aws:rds:$REGION:$ACCOUNT:cluster-snapshot:$SNAP_ID" \
    --s3-bucket-name "$S3_BUCKET" \
    --s3-prefix "planb-export/" \
    --iam-role-arn "$ROLE_ARN" \
    --kms-key-id "$KMS_ID" \
    --region "$REGION" --query 'Status' --output text

echo "[5/5] poll"
T0=$(date +%s)
while true; do
    st=$(aws rds describe-export-tasks --export-task-identifier "$EXPORT_ID" --region "$REGION" --query 'ExportTasks[0].Status' --output text 2>/dev/null)
    pct=$(aws rds describe-export-tasks --export-task-identifier "$EXPORT_ID" --region "$REGION" --query 'ExportTasks[0].PercentProgress' --output text 2>/dev/null)
    el=$(( $(date +%s) - T0 ))
    echo "   [${el}s] export=$st ${pct}%"
    case "$st" in COMPLETE|FAILED|CANCELED) break;; esac
    sleep 30
done

echo
echo "=== export done ==="
aws s3 ls "s3://$S3_BUCKET/planb-export/$EXPORT_ID/" --region "$REGION" --recursive | tail
echo
echo "Next: run COPY on Redshift:"
echo "  COPY public.ads_thor_fin_payment_iap_data_new"
echo "  FROM 's3://$S3_BUCKET/planb-export/$EXPORT_ID/$AURORA_CLUSTER/dw.ads_thor_fin_payment_iap_data_new/'"
echo "  IAM_ROLE 'arn:aws:iam::$ACCOUNT:role/redshift-copy-role'"
echo "  FORMAT AS PARQUET;"
