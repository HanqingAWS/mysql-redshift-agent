#!/bin/bash
# Generate 50M IAP orders and stream directly into Aurora via LOAD DATA LOCAL INFILE.
# No disk staging - pipe straight through.
set -euo pipefail
cd "$(dirname "$0")/.."

AURORA_HOST=$(grep ^AURORA_HOST .env | cut -d= -f2)
AURORA_USER=$(grep ^AURORA_USER .env | cut -d= -f2)
AURORA_PASSWORD=$(grep ^AURORA_PASSWORD .env | cut -d= -f2)
AURORA_DB=$(grep ^AURORA_DB .env | cut -d= -f2)

LOG=/home/ec2-user/bulkgen/load.log
mkdir -p /home/ec2-user/bulkgen

echo "[$(date)] starting 50M load into $AURORA_HOST/$AURORA_DB.iap_orders_5000w" | tee "$LOG"

# Truncate existing (idempotent)
mysql -h "$AURORA_HOST" -u "$AURORA_USER" -p"$AURORA_PASSWORD" "$AURORA_DB" \
    -e "TRUNCATE TABLE iap_orders_5000w;" 2>&1 | tee -a "$LOG"

# Use a named pipe so both processes run concurrently without a disk stage.
FIFO=/home/ec2-user/bulkgen/orders.fifo
rm -f "$FIFO"
mkfifo "$FIFO"

# Stage 1: generator -> fifo (background)
python3 scripts/gen_50m.py --uids 2280000 --target-rows 50000000 > "$FIFO" 2>>"$LOG" &
GEN_PID=$!

# Stage 2: LOAD DATA consumes fifo
mysql --local-infile=1 -h "$AURORA_HOST" -u "$AURORA_USER" -p"$AURORA_PASSWORD" "$AURORA_DB" \
    -e "SET SESSION unique_checks=0; SET SESSION foreign_key_checks=0;
        LOAD DATA LOCAL INFILE '$FIFO' INTO TABLE iap_orders_5000w
        FIELDS TERMINATED BY '\t' LINES TERMINATED BY '\n';" 2>&1 | tee -a "$LOG"

wait $GEN_PID
rm -f "$FIFO"

echo "[$(date)] LOAD complete" | tee -a "$LOG"
mysql -h "$AURORA_HOST" -u "$AURORA_USER" -p"$AURORA_PASSWORD" "$AURORA_DB" \
    -e "SELECT COUNT(*) AS rows_loaded FROM iap_orders_5000w;" 2>&1 | tee -a "$LOG"
