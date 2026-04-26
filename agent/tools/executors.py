"""给知识库管理用的双边执行器：跑同一条 SQL 在 MySQL 和 Redshift 上并回收结果。"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Any

import psycopg
import pymysql


MY_HOST = os.environ.get("AURORA_HOST", "")
MY_PORT = int(os.environ.get("AURORA_PORT", "3306"))
MY_USER = os.environ.get("AURORA_USER", "admin")
MY_PASSWORD = os.environ.get("AURORA_PASSWORD", "")
MY_DB = os.environ.get("AURORA_DB", "fpdw")

RS_HOST = os.environ.get("REDSHIFT_HOST", "")
RS_PORT = int(os.environ.get("REDSHIFT_PORT", "5439"))
RS_DB = os.environ.get("REDSHIFT_DB", "dev")
RS_USER = os.environ.get("REDSHIFT_USER", "admin")
RS_PASSWORD = os.environ.get("REDSHIFT_PASSWORD", "")


def run_mysql(sql: str, *, read_timeout: int = 120) -> dict:
    started = time.time()
    conn = pymysql.connect(
        host=MY_HOST, port=MY_PORT, user=MY_USER, password=MY_PASSWORD, database=MY_DB,
        connect_timeout=10, read_timeout=read_timeout, charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall() if cols else []
    finally:
        conn.close()
    return {"columns": cols, "rows": rows, "latency_ms": int((time.time()-started)*1000)}


def run_redshift(sql: str, *, timeout: int = 120) -> dict:
    started = time.time()
    dsn = (f"host={RS_HOST} port={RS_PORT} dbname={RS_DB} user={RS_USER} "
           f"password={RS_PASSWORD} sslmode=require connect_timeout=10 client_encoding=utf8")
    with psycopg.connect(dsn, connect_timeout=timeout) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [d.name for d in cur.description] if cur.description else []
            rows = cur.fetchall() if cols else []
    return {"columns": cols, "rows": rows, "latency_ms": int((time.time()-started)*1000)}
