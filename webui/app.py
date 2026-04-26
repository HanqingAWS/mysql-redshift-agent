"""Web DBMS for testing the MySQL-Redshift Proxy.

Three data sources:
- mysql: direct to Aurora MySQL (origin)
- redshift: direct to Redshift Serverless (via psycopg)
- proxy: via the Go proxy on :3306 (which internally talks to Redshift)

All three are queried with the same MySQL SQL from the browser. The UI
measures wall-clock latency + row count per request. For proxy calls,
we additionally ask the agent to translate the SQL so we can show the
Redshift-dialect SQL side-by-side.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx
import psycopg
import pymysql
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("webui")

AURORA_HOST = os.environ["AURORA_HOST"]
AURORA_PORT = int(os.environ.get("AURORA_PORT", "3306"))
AURORA_USER = os.environ["AURORA_USER"]
AURORA_PASSWORD = os.environ["AURORA_PASSWORD"]
AURORA_DB = os.environ.get("AURORA_DB", "dw")

RS_HOST = os.environ["REDSHIFT_HOST"]
RS_PORT = int(os.environ.get("REDSHIFT_PORT", "5439"))
RS_DB = os.environ.get("REDSHIFT_DB", "dev")
RS_USER = os.environ["REDSHIFT_USER"]
RS_PASSWORD = os.environ["REDSHIFT_PASSWORD"]

PROXY_HOST = os.environ.get("PROXY_HOST", "proxy")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "3306"))
PROXY_USER = os.environ["PROXY_MYSQL_USER"]
PROXY_PASSWORD = os.environ["PROXY_MYSQL_PASSWORD"]
PROXY_DB = os.environ.get("PROXY_MYSQL_DB", "dw")

AGENT_URL = os.environ.get("AGENT_URL", "http://agent:8088")

MAX_ROWS = int(os.environ.get("MAX_ROWS", "500"))

app = FastAPI(title="mysql-redshift-webui")


class QueryReq(BaseModel):
    source: str  # "mysql" | "redshift" | "proxy"
    sql: str


class QueryResp(BaseModel):
    source: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    latency_ms: int
    truncated: bool
    translated_sql: str | None = None  # only for proxy
    translate_ms: int | None = None
    error: str | None = None


def _stringify(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8", errors="replace")
        except Exception:
            return v.hex()
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _run_mysql(host: str, port: int, user: str, password: str, db: str, sql: str) -> dict:
    started = time.time()
    conn = pymysql.connect(
        host=host, port=port, user=user, password=password, database=db,
        connect_timeout=10, read_timeout=90, charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows_raw = cur.fetchmany(MAX_ROWS + 1) if cols else []
            truncated = len(rows_raw) > MAX_ROWS
            rows_raw = rows_raw[:MAX_ROWS]
            rows = [[_stringify(c) for c in r] for r in rows_raw]
            # get true count cheaply: if not truncated, len(rows); else say >MAX_ROWS
            row_count = len(rows_raw)
            if truncated:
                # pull the rest just to count (bounded by server-side limits in tests)
                more = cur.fetchall()
                row_count += len(more)
    finally:
        conn.close()
    return {
        "columns": cols,
        "rows": rows,
        "row_count": row_count,
        "latency_ms": int((time.time() - started) * 1000),
        "truncated": truncated,
    }


def _run_redshift(sql: str) -> dict:
    started = time.time()
    dsn = f"host={RS_HOST} port={RS_PORT} dbname={RS_DB} user={RS_USER} password={RS_PASSWORD} sslmode=require connect_timeout=10 client_encoding=utf8"
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [d.name for d in cur.description] if cur.description else []
            rows_raw = cur.fetchmany(MAX_ROWS + 1) if cols else []
            truncated = len(rows_raw) > MAX_ROWS
            rows_raw = rows_raw[:MAX_ROWS]
            rows = [[_stringify(c) for c in r] for r in rows_raw]
            row_count = len(rows_raw)
            if truncated:
                more = cur.fetchall()
                row_count += len(more)
    return {
        "columns": cols,
        "rows": rows,
        "row_count": row_count,
        "latency_ms": int((time.time() - started) * 1000),
        "truncated": truncated,
    }


def _translate(sql: str) -> tuple[str | None, int | None]:
    try:
        with httpx.Client(timeout=60.0) as c:
            r = c.post(f"{AGENT_URL}/translate", json={"sql": sql})
            if r.status_code != 200:
                return None, None
            j = r.json()
            return j.get("redshift_sql"), j.get("latency_ms")
    except Exception:
        log.exception("translate side-call failed")
        return None, None


@app.get("/healthz")
def healthz():
    return {"ok": True}


class ExplainResp(BaseModel):
    source: str
    plan: list[str]
    latency_ms: int
    translated_sql: str | None = None
    translate_ms: int | None = None
    error: str | None = None


def _explain_mysql(host: str, port: int, user: str, password: str, db: str, sql: str) -> dict:
    out = _run_mysql(host, port, user, password, db, f"EXPLAIN {sql}")
    cols = out["columns"]
    lines = []
    if cols:
        lines.append(" | ".join(cols))
        lines.append("-+-".join(["-" * max(3, len(c)) for c in cols]))
        for row in out["rows"]:
            lines.append(" | ".join("" if v is None else str(v) for v in row))
    return {"plan": lines, "latency_ms": out["latency_ms"]}


def _explain_redshift(sql: str) -> dict:
    out = _run_redshift(f"EXPLAIN {sql}")
    # Redshift EXPLAIN returns a single column "QUERY PLAN"
    lines = [str(row[0]) for row in out["rows"]]
    return {"plan": lines, "latency_ms": out["latency_ms"]}


@app.post("/api/explain", response_model=ExplainResp)
def api_explain(req: QueryReq):
    src = req.source.lower().strip()
    sql = req.sql.strip().rstrip(";")
    if not sql:
        raise HTTPException(status_code=400, detail="sql is empty")

    translated_sql = None
    translate_ms = None

    try:
        if src == "mysql":
            out = _explain_mysql(AURORA_HOST, AURORA_PORT, AURORA_USER, AURORA_PASSWORD, AURORA_DB, sql)
        elif src == "redshift":
            out = _explain_redshift(sql)
        elif src == "proxy":
            # 翻译 MySQL → Redshift，然后对 Redshift 执行 EXPLAIN
            translated_sql, translate_ms = _translate(sql)
            if not translated_sql:
                raise RuntimeError("translate failed (agent unreachable or returned empty)")
            out = _explain_redshift(translated_sql)
        else:
            raise HTTPException(status_code=400, detail=f"unknown source: {src}")
    except HTTPException:
        raise
    except Exception as e:
        log.exception("explain failed src=%s", src)
        return ExplainResp(
            source=src, plan=[], latency_ms=0,
            translated_sql=translated_sql, translate_ms=translate_ms,
            error=f"{type(e).__name__}: {e}",
        )

    return ExplainResp(
        source=src,
        plan=out["plan"],
        latency_ms=out["latency_ms"],
        translated_sql=translated_sql,
        translate_ms=translate_ms,
    )


@app.post("/api/query", response_model=QueryResp)
def api_query(req: QueryReq):
    src = req.source.lower().strip()
    sql = req.sql.strip().rstrip(";")
    if not sql:
        raise HTTPException(status_code=400, detail="sql is empty")

    translated_sql = None
    translate_ms = None

    try:
        if src == "mysql":
            out = _run_mysql(AURORA_HOST, AURORA_PORT, AURORA_USER, AURORA_PASSWORD, AURORA_DB, sql)
        elif src == "redshift":
            out = _run_redshift(sql)
        elif src == "proxy":
            # Fire translate async-ish: translate runs server-side, then query runs via proxy.
            # We do translate first so we can show even if query errors out.
            translated_sql, translate_ms = _translate(sql)
            out = _run_mysql(PROXY_HOST, PROXY_PORT, PROXY_USER, PROXY_PASSWORD, PROXY_DB, sql)
        else:
            raise HTTPException(status_code=400, detail=f"unknown source: {src}")
    except HTTPException:
        raise
    except Exception as e:
        log.exception("query failed src=%s", src)
        return QueryResp(
            source=src, columns=[], rows=[], row_count=0,
            latency_ms=0, truncated=False,
            translated_sql=translated_sql, translate_ms=translate_ms,
            error=f"{type(e).__name__}: {e}",
        )

    return QueryResp(
        source=src,
        columns=out["columns"],
        rows=out["rows"],
        row_count=out["row_count"],
        latency_ms=out["latency_ms"],
        truncated=out["truncated"],
        translated_sql=translated_sql,
        translate_ms=translate_ms,
    )


# ---------- built-in samples ----------

SAMPLES = [
    {
        "group": "50M 订单大表 · MySQL慢 vs Redshift快（iap_orders_5000w）",
        "items": [
            {
                "title": "全表 COUNT(*)",
                "sql": "SELECT COUNT(*) AS total FROM iap_orders_5000w;",
                "note": "MySQL 行存需要扫 5000w 行；Redshift 列存 zone-map 瞬秒",
            },
            {
                "title": "按支付渠道分组金额汇总",
                "sql": (
                    "SELECT payment_processor, COUNT(*) AS cnt, SUM(amount) AS total_amount\n"
                    "FROM iap_orders_5000w\n"
                    "GROUP BY payment_processor\n"
                    "ORDER BY total_amount DESC;"
                ),
                "note": "分组聚合：MySQL 需全表扫+hash agg，Redshift 按 payment_processor 列并行扫",
            },
            {
                "title": "大R TOP100 消费用户",
                "sql": (
                    "SELECT uid, SUM(amount) AS revenue, COUNT(*) AS orders\n"
                    "FROM iap_orders_5000w\n"
                    "GROUP BY uid\n"
                    "ORDER BY revenue DESC\n"
                    "LIMIT 100;"
                ),
                "note": "≈228w uid 去重聚合+排序，MySQL 预计 20–40s，Redshift 2–4s",
            },
            {
                "title": "单月收入按平台/国家聚合",
                "sql": (
                    "SELECT device_platform, country_code,\n"
                    "       COUNT(*) AS orders, SUM(amount) AS revenue\n"
                    "FROM iap_orders_5000w\n"
                    "WHERE event_time >= '2023-06-01' AND event_time < '2023-07-01'\n"
                    "GROUP BY device_platform, country_code\n"
                    "ORDER BY revenue DESC;"
                ),
                "note": "时间切片+两列 group by，MySQL 全表过滤，Redshift SORTKEY(event_time) 命中",
            },
            {
                "title": "反向：未命中白名单 → 自动走 MySQL",
                "sql": "SELECT COUNT(*) FROM ads_thor_fin_payment_iap_data_new;",
                "note": "选 Proxy 跑：这张表也在白名单，会走 Redshift。把表名改成任意其他表，Proxy 会直连 MySQL",
            },
        ],
    },
    {
        "group": "订单大表（100 行 demo 表：ads_thor_fin_payment_iap_data_new）",
        "items": [
            {
                "title": "订单流水总行数",
                "sql": "SELECT COUNT(*) AS total FROM ads_thor_fin_payment_iap_data_new;",
                "note": "最基础的聚合——切换数据源对比延迟",
            },
            {
                "title": "按支付渠道分组金额",
                "sql": (
                    "SELECT payment_processor, COUNT(*) AS cnt, SUM(amount) AS total_amount\n"
                    "FROM ads_thor_fin_payment_iap_data_new\n"
                    "GROUP BY payment_processor\n"
                    "ORDER BY total_amount DESC;"
                ),
                "note": "分组聚合——Redshift 列存 vs MySQL 行存",
            },
            {
                "title": "月度 Top 用户（综合复杂查询）",
                "sql": (
                    "SELECT uid, app_id, SUM(amount) AS revenue, COUNT(*) AS orders\n"
                    "FROM ads_thor_fin_payment_iap_data_new\n"
                    "WHERE event_time >= '2023-09-01' AND event_time < '2023-11-01'\n"
                    "  AND payment_processor NOT IN ('centurygames_pay','centurygames_store','pc')\n"
                    "GROUP BY uid, app_id\n"
                    "ORDER BY revenue DESC LIMIT 10;"
                ),
                "note": "WHERE+NOT IN+GROUP BY+ORDER BY+LIMIT 综合",
            },
        ],
    },
    {
        "group": "方言翻译样例（走 Proxy）",
        "items": [
            {
                "title": "反引号标识符",
                "sql": "SELECT `uid`, `amount` FROM dw.ads_thor_fin_payment_iap_data_new LIMIT 3;",
                "note": "MySQL 反引号 → Redshift 双引号",
            },
            {
                "title": "LIMIT a, b 语法",
                "sql": "SELECT uid, ts FROM dw.ads_thor_fin_payment_iap_data_new ORDER BY ts LIMIT 2, 3;",
                "note": "MySQL `LIMIT 2, 3` → Redshift `LIMIT 3 OFFSET 2`",
            },
            {
                "title": "IFNULL",
                "sql": "SELECT uid, IFNULL(app_version, 'unknown') AS ver\nFROM dw.ads_thor_fin_payment_iap_data_new LIMIT 5;",
                "note": "IFNULL → COALESCE",
            },
            {
                "title": "GROUP_CONCAT",
                "sql": (
                    "SELECT payment_processor, GROUP_CONCAT(DISTINCT app_id) AS apps\n"
                    "FROM dw.ads_thor_fin_payment_iap_data_new\n"
                    "GROUP BY payment_processor;"
                ),
                "note": "GROUP_CONCAT → LISTAGG WITHIN GROUP",
            },
        ],
    },
    {
        "group": "直连测试（走 MySQL / Redshift 对比）",
        "items": [
            {
                "title": "MySQL 原始 COUNT(*)",
                "sql": "SELECT COUNT(*) AS total FROM ads_thor_fin_payment_iap_data_new;",
                "note": "直连 MySQL：行存，全表扫",
            },
            {
                "title": "Redshift 原始 COUNT(*)",
                "sql": "SELECT COUNT(*) AS total FROM public.ads_thor_fin_payment_iap_data_new;",
                "note": "直连 Redshift：列存，zone map 优化",
            },
            {
                "title": "同一聚合两边对跑",
                "sql": (
                    "SELECT payment_processor, SUM(amount) AS total\n"
                    "FROM ads_thor_fin_payment_iap_data_new\n"
                    "GROUP BY payment_processor ORDER BY total DESC;"
                ),
                "note": "切换 source 下拉框分别跑 MySQL / Redshift / Proxy 对比耗时",
            },
        ],
    },
]


@app.get("/api/samples")
def api_samples():
    return {"groups": SAMPLES}


# ---------- static SPA ----------

app.mount("/static", StaticFiles(directory="static"), name="static")
# CDN behavior /dbms/* keeps the prefix when forwarding; mount an alias so
# https://<cdn>/dbms/static/report.html hits the same files as /static/.
app.mount("/dbms/static", StaticFiles(directory="static"), name="dbms_static")


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/knowledge")
def knowledge_page():
    return FileResponse("static/knowledge.html")


@app.get("/dbms/knowledge")
def knowledge_page_cdn_alias():
    return FileResponse("static/knowledge.html")


@app.get("/dbms/")
@app.get("/dbms")
def index_cdn_alias():
    return FileResponse("static/index.html")


# ============ 知识库管理代理路由（都转发到 agent） ============
@app.get("/api/knowledge")
def api_knowledge_list(limit: int = 50, offset: int = 0, search: str = ""):
    try:
        with httpx.Client(timeout=30.0) as c:
            r = c.get(f"{AGENT_URL}/api/knowledge",
                      params={"limit": limit, "offset": offset, "search": search})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.exception("list knowledge")
        raise HTTPException(status_code=502, detail=f"agent error: {e}")


@app.delete("/api/knowledge/{entry_id}")
def api_knowledge_delete(entry_id: int):
    try:
        with httpx.Client(timeout=30.0) as c:
            r = c.delete(f"{AGENT_URL}/api/knowledge/{entry_id}")
            r.raise_for_status()
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"agent error: {e}")


class ImportOneReq(BaseModel):
    mysql_sql: str
    compare_mode: str = "strict"
    force_save: bool = False


@app.post("/api/knowledge/import_test")
def api_knowledge_import_test(req: ImportOneReq):
    """单条 SQL 导入：翻译 → 双边执行 → 对比 → 入库。
    前端会对上传文件里的每条 SQL 循环调用这个 endpoint，实现进度条。
    """
    try:
        with httpx.Client(timeout=180.0) as c:
            r = c.post(f"{AGENT_URL}/api/knowledge/import_test", json=req.dict())
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.exception("import_test")
        raise HTTPException(status_code=502, detail=f"agent error: {e}")


# CDN aliases: /dbms/api/* → /api/*
@app.get("/dbms/api/knowledge")
def api_knowledge_list_cdn(limit: int = 50, offset: int = 0, search: str = ""):
    return api_knowledge_list(limit=limit, offset=offset, search=search)


@app.delete("/dbms/api/knowledge/{entry_id}")
def api_knowledge_delete_cdn(entry_id: int):
    return api_knowledge_delete(entry_id=entry_id)


@app.post("/dbms/api/knowledge/import_test")
def api_knowledge_import_test_cdn(req: ImportOneReq):
    return api_knowledge_import_test(req)


@app.post("/dbms/api/query")
def api_query_cdn(req: "QueryReq"):
    return api_query(req)


@app.post("/dbms/api/explain")
def api_explain_cdn(req: "QueryReq"):
    return api_explain(req)


@app.get("/dbms/api/samples")
def api_samples_cdn():
    return api_samples()
