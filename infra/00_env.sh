#!/bin/bash
# Source this file: `. 00_env.sh`
# 环境变量：区域 / VPC / 子网 / SG / 账户
#
# ⚠️ 本项目所有敏感值都通过环境变量注入。提交到 GitHub 的版本**不含真实账号**。
# 使用前请：
#   1. 把 .env.example 复制为 .env 并填入你的账号信息
#   2. 在本脚本里填入你的 VPC / 子网 / SG ID
#   3. `set -a && . ./.env && set +a` 把 .env 注入环境

export REGION=${REGION:-ap-northeast-1}
export ACCOUNT=${ACCOUNT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null)}

# VPC / 网络（替换为你的 VPC）
# Aurora 需要 ≥2 个可用区，Redshift Serverless 需要 ≥3 个可用区
export VPC=${VPC:-vpc-xxxxxxxxxxxxxxxxx}
export SG=${SG:-sg-xxxxxxxxxxxxxxxxx}
export SUB_1A=${SUB_1A:-subnet-xxxxxxxxxxxxxxxxx}
export SUB_1C=${SUB_1C:-subnet-xxxxxxxxxxxxxxxxx}
export SUB_1D=${SUB_1D:-subnet-xxxxxxxxxxxxxxxxx}

# Aurora MySQL Serverless v2
export AURORA_CLUSTER=${AURORA_CLUSTER:-dw-aurora-cluster}
export AURORA_INSTANCE=${AURORA_INSTANCE:-dw-aurora-instance}
export AURORA_DB=${AURORA_DB:-dw}
export AURORA_USER=${AURORA_USER:-admin}
export AURORA_PASSWORD=${AURORA_PASSWORD:?"AURORA_PASSWORD 请在 .env 中设置"}
export AURORA_SUBNET_GROUP=${AURORA_SUBNET_GROUP:-dw-aurora-subnet-group}

# Redshift Serverless
export RS_NAMESPACE=${RS_NAMESPACE:-dw-ns}
export RS_WORKGROUP=${RS_WORKGROUP:-dw-wg}
export RS_DB=${RS_DB:-dev}
export RS_USER=${RS_USER:-admin}
export RS_PASSWORD=${RS_PASSWORD:?"RS_PASSWORD 请在 .env 中设置"}

# S3 bucket for data migration
export S3_BUCKET=${S3_BUCKET:-dw-migration-${ACCOUNT}-${REGION}}

# Bedrock model (Sonnet 4.6 cross-region inference profile)
# Tokyo 用 jp. 前缀；美区用 us. 前缀；查 `aws bedrock list-inference-profiles`
export BEDROCK_MODEL_ID=${BEDROCK_MODEL_ID:-jp.anthropic.claude-sonnet-4-6}

echo "[env] loaded: REGION=$REGION VPC=$VPC SG=$SG ACCOUNT=$ACCOUNT"
