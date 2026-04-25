#!/bin/bash
# 把 sample_data.tsv 导入 Aurora MySQL
set -euo pipefail
cd "$(dirname "$0")" && . ../infra/00_env.sh

# 从创建 Aurora 脚本生成的文件读 endpoint（也可直接 describe 查）
AURORA_ENDPOINT=$(aws rds describe-db-clusters --db-cluster-identifier $AURORA_CLUSTER --region $REGION --query 'DBClusters[0].Endpoint' --output text)
echo "[load] Aurora endpoint: $AURORA_ENDPOINT"

# 建表
echo "[load] creating table..."
mysql -h $AURORA_ENDPOINT -u $AURORA_USER -p"$AURORA_PASSWORD" $AURORA_DB < schema.sql

# 导入数据（LOAD DATA LOCAL INFILE）
echo "[load] loading 100 rows..."
mysql -h $AURORA_ENDPOINT -u $AURORA_USER -p"$AURORA_PASSWORD" $AURORA_DB \
  --local-infile=1 \
  -e "LOAD DATA LOCAL INFILE 'data/sample_data.tsv'
      INTO TABLE ads_thor_fin_payment_iap_data_new
      FIELDS TERMINATED BY '\t' ENCLOSED BY ''
      LINES TERMINATED BY '\n'
      (game_name,uid,transaction_id,event_time,fpid,app_id,device_platform,
       country_code,gameserver_id,app_language,device_level,city_level,amount,
       is_white_user,new_app_id,payment_processor,iap_product_id,iap_product_name,
       base_price,iap_product_name_cn,app_version,currency,order_id,ts);"

# 校验
echo "[load] verification:"
mysql -h $AURORA_ENDPOINT -u $AURORA_USER -p"$AURORA_PASSWORD" $AURORA_DB -e "
SELECT COUNT(*) AS rows_total FROM ads_thor_fin_payment_iap_data_new;
SELECT uid, amount, payment_processor, event_time
  FROM ads_thor_fin_payment_iap_data_new
  ORDER BY ts DESC LIMIT 5;
"
