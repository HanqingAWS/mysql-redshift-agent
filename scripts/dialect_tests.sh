#!/usr/bin/env bash
# 10 个方言测试 SQL，通过 proxy 验证 MySQL → Redshift 翻译正确性
set -u

HOST=${HOST:-127.0.0.1}
PORT=${PORT:-3307}
MYUSER=${MYUSER:-demo}
PASS=${PASS:-<DEMO_PASSWORD>}
DB=${DB:-dw}

run() {
    local name="$1"
    local sql="$2"
    echo "=========================================="
    echo "[$name]"
    echo "SQL: $sql"
    echo "------------------------------------------"
    mysql -h "$HOST" -P "$PORT" -u "$MYUSER" -p"$PASS" "$DB" -e "$sql" 2>&1 | head -12
    echo ""
}

# 1. 基础 schema prefix
run "01-schema-prefix" \
    "SELECT COUNT(*) AS total FROM dw.ads_thor_fin_payment_iap_data_new;"

# 2. 反引号标识符
run "02-backticks" \
    "SELECT \`uid\`, \`amount\` FROM dw.ads_thor_fin_payment_iap_data_new LIMIT 3;"

# 3. LIMIT a, b 两参数形式
run "03-limit-offset" \
    "SELECT uid, ts FROM dw.ads_thor_fin_payment_iap_data_new ORDER BY ts LIMIT 2, 3;"

# 4. IFNULL
run "04-ifnull" \
    "SELECT uid, IFNULL(app_version, 'unknown') AS ver FROM dw.ads_thor_fin_payment_iap_data_new LIMIT 3;"

# 5. DATE_FORMAT
run "05-date-format" \
    "SELECT DATE_FORMAT(event_time, '%Y-%m-%d') AS day, COUNT(*) c FROM dw.ads_thor_fin_payment_iap_data_new GROUP BY day ORDER BY day LIMIT 3;"

# 6. GROUP_CONCAT
run "06-group-concat" \
    "SELECT payment_processor, GROUP_CONCAT(DISTINCT app_id) AS apps FROM dw.ads_thor_fin_payment_iap_data_new GROUP BY payment_processor LIMIT 3;"

# 7. 日期加减（INTERVAL）
run "07-date-interval" \
    "SELECT uid FROM dw.ads_thor_fin_payment_iap_data_new WHERE event_time >= DATE_SUB('2025-09-20 00:00:00', INTERVAL 3 DAY) AND event_time < '2025-09-21' LIMIT 3;"

# 8. UNIX_TIMESTAMP / FROM_UNIXTIME
run "08-unix-timestamp" \
    "SELECT uid, UNIX_TIMESTAMP(event_time) AS et_unix FROM dw.ads_thor_fin_payment_iap_data_new LIMIT 3;"

# 9. CONCAT_WS
run "09-concat-ws" \
    "SELECT CONCAT_WS('-', uid, transaction_id) AS id FROM dw.ads_thor_fin_payment_iap_data_new LIMIT 3;"

# 10. 用户样例：复合条件 + ORDER BY + LIMIT OFFSET
run "10-user-sample" \
    "select uid,fpid,transaction_id,new_app_id as app_id,base_price,amount,payment_processor,order_id,ts from dw.ads_thor_fin_payment_iap_data_new where event_time >= '2025-09-01 00:00:00' and event_time <= '2025-10-01 00:00:00' and game_name='1' and amount > 0 and payment_processor not in ('centurygames_pay','centurygames_store','centurygames','kg3rdpartypayment','pc') order by ts limit 5 offset 0;"

echo "=========================================="
echo "done."
