#!/bin/bash
# 确保 SG 自引用（Aurora/Redshift/Proxy 之间互通）
# 假设 $SG 已"全通"（inbound 所有端口开给自身）；此脚本校验并补规则
set -euo pipefail
cd "$(dirname "$0")" && . ./00_env.sh

echo "[sg] inspecting $SG ingress rules..."
aws ec2 describe-security-groups --group-ids $SG --region $REGION \
  --query 'SecurityGroups[0].IpPermissions[*].[IpProtocol,FromPort,ToPort,IpRanges[0].CidrIp]' --output table

# 确保 SG 自引用（让同 SG 的资源互通所有端口）
if aws ec2 describe-security-groups --group-ids $SG --region $REGION \
  --query 'SecurityGroups[0].IpPermissions[?IpProtocol==`-1` && UserIdGroupPairs[?GroupId==`'$SG'`]]' --output text | grep -q .; then
  echo "[sg] self-reference already in place"
else
  echo "[sg] adding self-reference (-1/all from $SG to $SG)..."
  aws ec2 authorize-security-group-ingress --group-id $SG \
    --source-group $SG --protocol -1 --region $REGION || true
fi
