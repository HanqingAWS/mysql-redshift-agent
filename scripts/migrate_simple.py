#!/usr/bin/env python3
"""
【方案 A】Aurora → Redshift 快速迁移（演示用）
流程：
  1. 从 Aurora MySQL 全量 SELECT
  2. 写成 CSV 上传到 S3
  3. Redshift COPY FROM 's3://.../data.csv' FORMAT AS CSV

设计取舍：
- 演示阶段不搞增量、不搞 CDC，全量覆盖
- CSV 用 DictWriter，NULL 值输出空串（Redshift COPY 默认 '' 视为 NULL，加 EMPTYASNULL 选项）
- 为避免跨表迁移的重复代码，TABLES 列表可扩展
"""
import csv
import io
import os
import sys
import time
from pathlib import Path

import boto3
import pymysql
import psycopg2

# --- 环境变量 ---
REGION = os.environ["REGION"]
AURORA_HOST = os.environ["AURORA_HOST"]
AURORA_DB = os.environ["AURORA_DB"]
AURORA_USER = os.environ["AURORA_USER"]
AURORA_PASSWORD = os.environ["AURORA_PASSWORD"]
RS_HOST = os.environ["REDSHIFT_HOST"]
RS_PORT = int(os.environ.get("REDSHIFT_PORT", "5439"))
RS_DB = os.environ["REDSHIFT_DB"]
RS_USER = os.environ["REDSHIFT_USER"]
RS_PASSWORD = os.environ["REDSHIFT_PASSWORD"]
S3_BUCKET = os.environ["S3_BUCKET"]
IAM_ROLE = os.environ.get("REDSHIFT_IAM_ROLE", "")  # arn:aws:iam::...:role/redshift-copy-role

TABLES = ["ads_thor_fin_payment_iap_data_new"]


def dump_table_to_csv(my_conn, table: str) -> tuple[str, list[str]]:
    """从 Aurora 读出全表，返回本地 CSV 路径 + 列名列表"""
    out = Path(f"/tmp/{table}.csv")
    with my_conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(f"SELECT * FROM {table}")
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for r in rows:
            # Redshift COPY 不接受 python None，转成空串（配合 EMPTYASNULL）
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})

    print(f"[dump] {table}: {len(rows)} rows → {out}")
    return str(out), cols


def upload_to_s3(local_path: str, key: str) -> str:
    s3 = boto3.client("s3", region_name=REGION)
    s3.upload_file(local_path, S3_BUCKET, key)
    uri = f"s3://{S3_BUCKET}/{key}"
    print(f"[s3] uploaded → {uri}")
    return uri


def copy_into_redshift(rs_conn, table: str, s3_uri: str):
    """Redshift COPY from S3 CSV"""
    # IAM_ROLE 优先（推荐）；否则退到 session credentials（不推荐生产）
    if IAM_ROLE:
        creds = f"IAM_ROLE '{IAM_ROLE}'"
    else:
        session = boto3.Session()
        c = session.get_credentials().get_frozen_credentials()
        if c.token:
            creds = (
                f"ACCESS_KEY_ID '{c.access_key}' "
                f"SECRET_ACCESS_KEY '{c.secret_key}' "
                f"SESSION_TOKEN '{c.token}'"
            )
        else:
            creds = f"ACCESS_KEY_ID '{c.access_key}' SECRET_ACCESS_KEY '{c.secret_key}'"

    with rs_conn.cursor() as cur:
        cur.execute(f"TRUNCATE TABLE {table}")
        copy_sql = f"""
            COPY {table}
            FROM '{s3_uri}'
            {creds}
            FORMAT AS CSV
            IGNOREHEADER 1
            EMPTYASNULL
            BLANKSASNULL
            TIMEFORMAT 'YYYY-MM-DD HH:MI:SS'
            REGION '{REGION}'
        """
        print(f"[copy] COPY {table} FROM {s3_uri}")
        cur.execute(copy_sql)
        rs_conn.commit()

        cur.execute(f"SELECT COUNT(*) FROM {table}")
        n = cur.fetchone()[0]
        print(f"[copy] {table}: {n} rows loaded into Redshift")


def main():
    my_conn = pymysql.connect(
        host=AURORA_HOST, user=AURORA_USER, password=AURORA_PASSWORD,
        database=AURORA_DB, connect_timeout=10,
    )
    rs_conn = psycopg2.connect(
        host=RS_HOST, port=RS_PORT, user=RS_USER, password=RS_PASSWORD, dbname=RS_DB,
        connect_timeout=10,
    )

    try:
        for t in TABLES:
            local, _cols = dump_table_to_csv(my_conn, t)
            key = f"migrate/{t}/{int(time.time())}.csv"
            uri = upload_to_s3(local, key)
            copy_into_redshift(rs_conn, t, uri)
        print("\n[done] migration finished.")
    finally:
        my_conn.close()
        rs_conn.close()


if __name__ == "__main__":
    main()
