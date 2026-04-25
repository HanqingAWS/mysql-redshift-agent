#!/bin/bash
# 创建 Redshift COPY 用的 IAM role（访问 S3 读权限）
# 并 associate 到 Redshift Serverless namespace
set -euo pipefail
cd "$(dirname "$0")" && . ./00_env.sh

ROLE_NAME=redshift-copy-role

# 1. Trust policy (Redshift service principal)
cat > /tmp/rs_trust.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "redshift.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam get-role --role-name $ROLE_NAME >/dev/null 2>&1 && {
  echo "[iam] role exists: $ROLE_NAME"
} || {
  aws iam create-role --role-name $ROLE_NAME \
    --assume-role-policy-document file:///tmp/rs_trust.json \
    --description "Redshift Serverless COPY from S3" \
    --output text --query 'Role.Arn'
  echo "[iam] created role $ROLE_NAME"
}

# 2. S3 read policy（只读指定桶）
cat > /tmp/rs_policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "s3:GetObject", "s3:GetBucketLocation",
      "s3:ListBucket"
    ],
    "Resource": [
      "arn:aws:s3:::$S3_BUCKET",
      "arn:aws:s3:::$S3_BUCKET/*"
    ]
  }]
}
EOF
aws iam put-role-policy --role-name $ROLE_NAME \
  --policy-name S3ReadOnlyMigrationBucket \
  --policy-document file:///tmp/rs_policy.json
echo "[iam] attached inline policy"

ROLE_ARN=$(aws iam get-role --role-name $ROLE_NAME --query 'Role.Arn' --output text)
echo "[iam] role arn: $ROLE_ARN"

# 3. Associate IAM role to Redshift namespace
CURRENT_ROLES=$(aws redshift-serverless get-namespace --namespace-name $RS_NAMESPACE --region $REGION --query 'namespace.iamRoles' --output text 2>/dev/null || echo "")
if echo "$CURRENT_ROLES" | grep -q "$ROLE_ARN"; then
  echo "[iam] role already associated to namespace"
else
  echo "[iam] associating role to namespace $RS_NAMESPACE ..."
  aws redshift-serverless update-namespace \
    --namespace-name $RS_NAMESPACE \
    --iam-roles "$ROLE_ARN" \
    --default-iam-role-arn "$ROLE_ARN" \
    --region $REGION \
    --output table --query 'namespace.iamRoles'
fi

echo ""
echo "REDSHIFT_IAM_ROLE=$ROLE_ARN" >> /tmp/dw_endpoints.env
echo "[iam] done. role: $ROLE_ARN"
