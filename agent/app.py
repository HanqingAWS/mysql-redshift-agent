"""
db-convertor-agent: Strands + Sonnet 4.6 的 SQL 方言翻译服务。

对外 HTTP:
  POST /translate
    body: {"sql": "...", "prev_error": "..."(optional), "prev_sql": "..."(optional)}
    resp: {"redshift_sql": "...", "used_rules": [...], "usage": {...}}

Proxy 第一次翻译不带 prev_error；若执行失败，Proxy 带错误重试调用本接口。
"""
import json
import os
import re
import time
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from strands import Agent
from strands.models import BedrockModel
from strands.tools import tool as strands_tool  # noqa: F401  (imported to be available to the agent)

from tools.lookup_dialect_rule import lookup_dialect_rule, list_all_rules, match_references
from tools.get_table_schema import get_table_schema
from tools import sql_knowledge, compare as sql_compare, executors


# ------------------------ config ------------------------
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("agent")

SYSTEM_PROMPT = """你是一个 MySQL → Redshift SQL 方言翻译器。

职责：把输入的 MySQL SQL 翻译成**语义等价**的 Redshift SQL，以便在 Redshift 上执行并返回和 MySQL 一致的结果集。

硬性要求（严格遵守）：
1. 输出必须是**单条可执行的 Redshift SQL**，**只能包含 SQL 本身**——
   不要任何解释、前缀、后缀、Markdown 围栏、思考过程、"Here is"/"当然"/"分析一下"之类语句。
   调用 tool 之后**也不要**写解释，直接给最终 SQL。
2. 不能改变原始 SQL 的语义（列、条件、过滤、排序、limit 都要一一对应）
3. 不要对数据做聚合/过滤/截断等"优化"——翻译，不重写
4. 若 SQL 含不可翻译的 MySQL 特性（比如 `ON DUPLICATE KEY`），返回单行文本：
   `-- UNTRANSLATABLE: <原因>`
5. 失败修正时：Redshift timeout / context deadline 等性能错误**不是语法错误**，
   原样返回之前的 SQL 即可，**不要**加 CAST 或 index hint "优化"。

关键差异须知：
- 表名去掉 `dw.` 前缀（Redshift 表在默认 schema `public` 下）
- 反引号 → 双引号（仅标识符；字符串仍用单引号）
- `LIMIT a, b` → `LIMIT b OFFSET a`
- `IFNULL(x, y)` → `COALESCE(x, y)`
- `GROUP_CONCAT(col)` → `LISTAGG(col, ',') WITHIN GROUP (ORDER BY col)`
- `DATE_FORMAT(d, '%Y-%m-%d')` → `TO_CHAR(d, 'YYYY-MM-DD')`

如果碰到没见过的函数或语法，调用 `lookup_dialect_rule` 工具查规则文档。
如果需要知道表的列类型，调用 `get_table_schema` 工具。

失败修正场景：
- 当用户消息里同时给出 `prev_sql`（上一次你翻译的 Redshift SQL）和 `prev_error`（Redshift 返回的错误）时，
  你的任务是**据错误修正 SQL**，输出一条新的可执行 SQL。
"""

# ------------------------ agent ------------------------
_agent: Optional[Agent] = None


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        log.info("initializing Bedrock model=%s region=%s", MODEL_ID, REGION)
        # Strands BedrockModel accepts either `region` or `region_name`;
        # also pass a pre-built boto3 session for explicit region binding.
        import boto3
        session = boto3.session.Session(region_name=REGION)
        model = BedrockModel(model_id=MODEL_ID, boto_session=session)
        _agent = Agent(
            model=model,
            system_prompt=SYSTEM_PROMPT,
            tools=[lookup_dialect_rule, list_all_rules, get_table_schema],
        )
    return _agent


# ------------------------ pre-filter ------------------------
CLEAN_PATTERNS = [
    # 去掉 ```sql ... ``` 围栏
    re.compile(r"^```(?:sql)?\s*\n", re.IGNORECASE),
    re.compile(r"\n?```\s*$"),
]


def strip_code_fence(text: str) -> str:
    t = text.strip()
    for p in CLEAN_PATTERNS:
        t = p.sub("", t)
    return t.strip()


# 识别 SQL 起始关键字，丢弃模型前置的 prose
_SQL_START = re.compile(
    r"(?is)\b(WITH|SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TRUNCATE|SHOW|EXPLAIN|BEGIN|COMMIT|COPY|UNLOAD)\b"
)


def extract_sql(text: str) -> str:
    """从模型输出里抽出 SQL：
    1. 先剥 ```sql 围栏
    2. 如果以 `-- UNTRANSLATABLE` 开头，原样返回
    3. 否则从第一个 SQL 关键字开始截断（丢掉前置解释文字）
    """
    t = strip_code_fence(text)
    if t.upper().startswith("-- UNTRANSLATABLE"):
        return t
    # 如果文本内包含 ```sql fence，取 fence 内部
    m = re.search(r"```(?:sql)?\s*\n(.*?)```", t, re.IGNORECASE | re.DOTALL)
    if m:
        t = m.group(1).strip()
    m = _SQL_START.search(t)
    if m:
        t = t[m.start():].strip()
    # 末尾分号清理
    return t.rstrip().rstrip(";").strip()


# ------------------------ HTTP ------------------------
class TranslateReq(BaseModel):
    sql: str
    prev_error: Optional[str] = None
    prev_sql: Optional[str] = None


class TranslateResp(BaseModel):
    redshift_sql: str
    used_rules: list[str]
    latency_ms: int
    attempt: str  # "initial" or "fix"
    examples_hit: int = 0
    examples: list[dict] = []


app = FastAPI(title="db-convertor-agent")


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": MODEL_ID, "region": REGION}


@app.post("/translate", response_model=TranslateResp)
def translate(req: TranslateReq):
    started = time.time()
    sql = req.sql.strip().rstrip(";")

    # pre-filter: 发现关键词 → 把 rule 直接拼进 user message（比等 agent 自己调 tool 快）
    used_rules = match_references(sql)
    rules_blob = lookup_dialect_rule(sql) if used_rules else ""

    # retrieval: 从知识库召回相似历史成功翻译，作为 few-shot
    examples = sql_knowledge.retrieve_similar(sql) if not req.prev_error else []
    examples_blob = sql_knowledge.format_examples_for_prompt(examples)

    if req.prev_error and req.prev_sql:
        attempt = "fix"
        user_msg = f"""The previous translation failed on Redshift.

ORIGINAL MySQL SQL:
{sql}

PREVIOUS Redshift SQL (broken):
{req.prev_sql}

REDSHIFT ERROR:
{req.prev_error}

{rules_blob}

Please fix the Redshift SQL. Output only the corrected SQL."""
    else:
        attempt = "initial"
        user_msg = f"""Translate the following MySQL SQL into Redshift SQL.

MySQL SQL:
{sql}

{rules_blob}

{examples_blob}

Output only the Redshift SQL (no explanation, no code fence)."""

    log.info("translate attempt=%s used_rules=%s examples_hit=%d sql=%r",
             attempt, used_rules, len(examples), sql[:200])

    try:
        result = get_agent()(user_msg)
        # Strands Agent() returns AgentResult; convert to string
        text = str(result).strip()
    except Exception as e:
        log.exception("agent failed")
        raise HTTPException(status_code=500, detail=f"agent error: {e}")

    redshift_sql = extract_sql(text)

    if redshift_sql.upper().startswith("-- UNTRANSLATABLE"):
        raise HTTPException(status_code=422, detail=redshift_sql)

    latency_ms = int((time.time() - started) * 1000)
    log.info("translated in %dms: %r", latency_ms, redshift_sql[:200])
    return TranslateResp(
        redshift_sql=redshift_sql,
        used_rules=used_rules,
        latency_ms=latency_ms,
        attempt=attempt,
        examples_hit=len(examples),
        examples=[{"similarity": e["similarity"], "mysql_sql": e["mysql_sql"][:200]} for e in examples],
    )


# ============ 知识库管理 API ============
class SaveReq(BaseModel):
    mysql_sql: str
    redshift_sql: str
    used_rules: list[str] = []
    row_count: int | None = None
    mysql_ms: int | None = None
    redshift_ms: int | None = None
    compare_mode: str = "strict"
    source: str = "runtime"


@app.post("/save_example")
def save_example_endpoint(req: SaveReq):
    rid = sql_knowledge.save_example(
        req.mysql_sql, req.redshift_sql,
        used_rules=req.used_rules,
        row_count=req.row_count,
        mysql_ms=req.mysql_ms,
        redshift_ms=req.redshift_ms,
        compare_mode=req.compare_mode,
        source=req.source,
    )
    return {"ok": rid is not None, "id": rid}


@app.get("/api/knowledge")
def api_knowledge_list(limit: int = 50, offset: int = 0, search: str = ""):
    total = sql_knowledge.count_entries(search)
    items = sql_knowledge.list_entries(limit=limit, offset=offset, search=search)
    # 序列化 datetime
    for it in items:
        for k in ("created_at", "last_used_at"):
            if it.get(k) is not None:
                it[k] = it[k].isoformat()
    return {"total": total, "items": items}


@app.delete("/api/knowledge/{entry_id}")
def api_knowledge_delete(entry_id: int):
    ok = sql_knowledge.delete_entry(entry_id)
    return {"ok": ok}


class ImportTestReq(BaseModel):
    mysql_sql: str
    compare_mode: str = "strict"  # strict / lenient / skipped
    force_save: bool = False       # A 选项：对比失败仍强制入库


class ImportTestResp(BaseModel):
    mysql_sql: str
    redshift_sql: str | None = None
    translate_ms: int | None = None
    used_rules: list[str] = []
    examples_hit: int = 0
    mysql_ok: bool = False
    mysql_ms: int | None = None
    mysql_rows: int | None = None
    mysql_error: str | None = None
    redshift_ok: bool = False
    redshift_ms: int | None = None
    redshift_rows: int | None = None
    redshift_error: str | None = None
    compare_ok: bool = False
    compare_reason: str = ""
    saved: bool = False
    saved_id: int | None = None


@app.post("/api/knowledge/import_test", response_model=ImportTestResp)
def api_knowledge_import_test(req: ImportTestReq):
    """翻译 + MySQL 执行 + Redshift 执行 + 结果对比 + 入库（按要求）。
    这是 webui "翻译并验证后入库" 流程的核心 endpoint。
    """
    resp = ImportTestResp(mysql_sql=req.mysql_sql)

    # 1. 翻译
    try:
        tr = translate(TranslateReq(sql=req.mysql_sql))
        resp.redshift_sql = tr.redshift_sql
        resp.translate_ms = tr.latency_ms
        resp.used_rules = tr.used_rules
        resp.examples_hit = tr.examples_hit
    except HTTPException as e:
        resp.compare_reason = f"翻译失败: {e.detail}"
        return resp
    except Exception as e:
        resp.compare_reason = f"翻译异常: {e}"
        return resp

    # 2. MySQL 执行
    try:
        r = executors.run_mysql(req.mysql_sql)
        resp.mysql_ok = True
        resp.mysql_ms = r["latency_ms"]
        resp.mysql_rows = len(r["rows"])
        my_cols, my_rows = r["columns"], r["rows"]
    except Exception as e:
        resp.mysql_error = str(e)[:500]
        resp.compare_reason = f"MySQL 执行失败: {resp.mysql_error}"
        return resp

    # 3. Redshift 执行
    try:
        r = executors.run_redshift(resp.redshift_sql)
        resp.redshift_ok = True
        resp.redshift_ms = r["latency_ms"]
        resp.redshift_rows = len(r["rows"])
        rs_cols, rs_rows = r["columns"], r["rows"]
    except Exception as e:
        resp.redshift_error = str(e)[:500]
        resp.compare_reason = f"Redshift 执行失败: {resp.redshift_error}"
        return resp

    # 4. 对比
    cmp = sql_compare.compare(my_rows, rs_rows, my_cols, rs_cols, mode=req.compare_mode)
    resp.compare_ok = cmp["ok"]
    resp.compare_reason = cmp["reason"]

    # 5. 入库（对比通过，或对比失败但 force_save）
    should_save = cmp["ok"] or req.force_save
    if should_save:
        rid = sql_knowledge.save_example(
            req.mysql_sql, resp.redshift_sql,
            used_rules=resp.used_rules,
            row_count=resp.mysql_rows,
            mysql_ms=resp.mysql_ms,
            redshift_ms=resp.redshift_ms,
            compare_mode=("override" if (not cmp["ok"] and req.force_save) else req.compare_mode),
            source="import",
        )
        resp.saved = rid is not None
        resp.saved_id = rid

    return resp


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8088, log_level="info")
