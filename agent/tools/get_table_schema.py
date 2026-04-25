"""
Strands Tool：从 Redshift `information_schema` 拉目标表的列定义，
并缓存进进程内字典。
"""
import os
from functools import lru_cache

import psycopg2
from strands.tools import tool

RS_HOST = os.environ.get("REDSHIFT_HOST", "")
RS_PORT = int(os.environ.get("REDSHIFT_PORT", "5439"))
RS_DB = os.environ.get("REDSHIFT_DB", "dev")
RS_USER = os.environ.get("REDSHIFT_USER", "admin")
RS_PASSWORD = os.environ.get("REDSHIFT_PASSWORD", "")


def _conn():
    return psycopg2.connect(
        host=RS_HOST, port=RS_PORT, dbname=RS_DB,
        user=RS_USER, password=RS_PASSWORD, connect_timeout=5,
    )


@lru_cache(maxsize=128)
def _fetch_schema(table: str) -> str:
    if not RS_HOST:
        return "(redshift not configured; skip schema)"
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type, character_maximum_length, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = %s
                 ORDER BY ordinal_position
                """,
                (table.lower(),),
            )
            rows = cur.fetchall()
        if not rows:
            return f"(table {table} not found in Redshift)"
        lines = [f"{name} {dtype}" + (f"({maxlen})" if maxlen else "") + ("" if nullable == "YES" else " NOT NULL")
                 for name, dtype, maxlen, nullable in rows]
        return f"TABLE {table} (\n  " + ",\n  ".join(lines) + "\n)"
    except Exception as e:
        return f"(failed to fetch schema for {table}: {e})"


@tool
def get_table_schema(table_name: str) -> str:
    """查询 Redshift 端指定表的列定义（含类型）。

    参数：
        table_name: 表名（不含 schema 前缀，如 'ads_thor_fin_payment_iap_data_new'）
    返回：类 DDL 的列定义字符串，供翻译时做类型参考。
    """
    # 兼容 "dw.xxx" 写法
    if "." in table_name:
        table_name = table_name.split(".", 1)[1]
    return _fetch_schema(table_name)
