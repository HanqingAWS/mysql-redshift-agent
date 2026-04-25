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

Output only the Redshift SQL (no explanation, no code fence)."""

    log.info("translate attempt=%s used_rules=%s sql=%r", attempt, used_rules, sql[:200])

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
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8088, log_level="info")
