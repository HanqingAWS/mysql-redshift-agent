"""pgvector 知识库读写：相似 SQL 召回 + 成功样本回写。

表结构见 scripts/schema_pgvector.sql。
"""
from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from typing import Iterable, Optional

import psycopg
from pgvector.psycopg import register_vector

from .embedding import embed

_LOW_VALUE_PREFIX = re.compile(r"^\s*(SET|SHOW|USE|BEGIN|COMMIT|ROLLBACK)\b", re.I)

log = logging.getLogger("sql_knowledge")

PG_HOST = os.environ.get("AURORA_PG_HOST", "")
PG_PORT = int(os.environ.get("AURORA_PG_PORT", "5432"))
PG_DB = os.environ.get("AURORA_PG_DB", "knowledge")
PG_USER = os.environ.get("AURORA_PG_USER", "dbadmin")
PG_PASSWORD = os.environ.get("AURORA_PG_PASSWORD", "")

TOP_K = int(os.environ.get("KNOWLEDGE_TOP_K", "3"))
THRESHOLD = float(os.environ.get("KNOWLEDGE_THRESHOLD", "0.85"))
# 写入时的去重阈值：同模板不同字面值的实测相似度在 0.93-0.95，超过此值视为
# 近似重复，只 bump hit_count，不新增行。0.94 是刚好把"改 ORDER BY / 改 NOT IN"
# 这类业务语义变化保留下来的拐点（实测）。
DEDUP_THRESHOLD = float(os.environ.get("KNOWLEDGE_DEDUP_THRESHOLD", "0.94"))
# 最小 redshift_sql 长度；过短的多是 demo/调试 SQL
MIN_REDSHIFT_SQL_LEN = 30


def _dsn() -> str:
    return f"host={PG_HOST} port={PG_PORT} dbname={PG_DB} user={PG_USER} password={PG_PASSWORD} sslmode=require"


def is_enabled() -> bool:
    return bool(PG_HOST and PG_PASSWORD)


@contextmanager
def _conn():
    c = psycopg.connect(_dsn(), connect_timeout=5)
    try:
        register_vector(c)
        yield c
    finally:
        c.close()


def retrieve_similar(sql: str, top_k: int = TOP_K, threshold: float = THRESHOLD) -> list[dict]:
    """召回相似度 >= threshold 的 top_k 条历史样本。

    返回 [{id, mysql_sql, redshift_sql, similarity}, ...]。
    相似度 = 1 - cosine_distance，越大越像。
    """
    if not is_enabled():
        return []
    try:
        q_vec = embed(sql, input_type="search_query")
        with _conn() as c:
            rows = c.execute(
                """
                SELECT id, mysql_sql, redshift_sql,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM sql_knowledge
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (q_vec, q_vec, top_k),
            ).fetchall()
        out = []
        for rid, m, r, sim in rows:
            if sim < threshold:
                continue
            out.append({
                "id": rid,
                "mysql_sql": m,
                "redshift_sql": r,
                "similarity": float(sim),
            })
        # 异步 bump hit_count（简单起见同步执行，单条几毫秒）
        if out:
            ids = [x["id"] for x in out]
            with _conn() as c:
                c.execute(
                    "UPDATE sql_knowledge SET hit_count=hit_count+1, last_used_at=NOW() WHERE id = ANY(%s)",
                    (ids,),
                )
                c.commit()
        return out
    except Exception as e:
        log.warning("retrieve_similar failed: %s (falling back to no-examples)", e)
        return []


def _is_low_value(mysql_sql: str, redshift_sql: str, row_count: Optional[int]) -> Optional[str]:
    """价值密度过滤：筛掉 demo/调试/空结果/管理命令。命中返回原因，否则 None。"""
    if row_count == 0:
        return "row_count=0"
    if len(redshift_sql.strip()) < MIN_REDSHIFT_SQL_LEN:
        return f"redshift_sql too short ({len(redshift_sql.strip())} < {MIN_REDSHIFT_SQL_LEN})"
    if _LOW_VALUE_PREFIX.match(mysql_sql):
        return "SET/SHOW/USE/BEGIN/COMMIT/ROLLBACK"
    return None


def save_example(
    mysql_sql: str,
    redshift_sql: str,
    *,
    used_rules: Optional[list[str]] = None,
    row_count: Optional[int] = None,
    mysql_ms: Optional[int] = None,
    redshift_ms: Optional[int] = None,
    compare_mode: str = "strict",
    source: str = "runtime",
) -> Optional[int]:
    """UPSERT 一条样本。返回 row id（失败返回 None）。

    除 md5 精确去重外，额外做两层筛选：
    - 价值密度：空结果 / 过短 SQL / SET-SHOW-USE 管理命令一律 skip
    - 向量去重：与库里最相似样本的 cosine 相似度 ≥ DEDUP_THRESHOLD (默认 0.94)
      时不新增行，只 bump hit_count（同模板不同字面值的重复会被聚合）
    种子导入 (source=seed) 和批量导入 (source=import) 绕过这两层，直接入库。
    """
    if not is_enabled():
        return None

    # 种子 / 批量导入是显式人工决定的，不走价值/去重筛选
    bypass_filter = source in ("seed", "import")

    if not bypass_filter:
        reason = _is_low_value(mysql_sql, redshift_sql, row_count)
        if reason:
            log.info("save_example skip low-value (%s): %.80s", reason, mysql_sql)
            return None

    try:
        vec = embed(mysql_sql, input_type="search_document")

        # 向量去重：查最相似的一条
        if not bypass_filter:
            with _conn() as c:
                row = c.execute(
                    """
                    SELECT id, 1 - (embedding <=> %s::vector) AS sim
                    FROM sql_knowledge
                    ORDER BY embedding <=> %s::vector
                    LIMIT 1
                    """,
                    (vec, vec),
                ).fetchone()
            if row is not None:
                existing_id, sim = int(row[0]), float(row[1])
                if sim >= DEDUP_THRESHOLD:
                    with _conn() as c:
                        c.execute(
                            "UPDATE sql_knowledge SET hit_count=hit_count+1, last_used_at=NOW() WHERE id=%s",
                            (existing_id,),
                        )
                        c.commit()
                    log.info("save_example dedup sim=%.4f bump id=%d: %.80s",
                             sim, existing_id, mysql_sql)
                    return existing_id

        with _conn() as c:
            rid = c.execute(
                """
                INSERT INTO sql_knowledge
                    (mysql_sql, redshift_sql, embedding, used_rules,
                     row_count, mysql_ms, redshift_ms, compare_mode, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (md5(mysql_sql)) DO UPDATE SET
                    redshift_sql = EXCLUDED.redshift_sql,
                    embedding    = EXCLUDED.embedding,
                    used_rules   = EXCLUDED.used_rules,
                    row_count    = EXCLUDED.row_count,
                    mysql_ms     = EXCLUDED.mysql_ms,
                    redshift_ms  = EXCLUDED.redshift_ms,
                    compare_mode = EXCLUDED.compare_mode,
                    source       = EXCLUDED.source,
                    last_used_at = NOW()
                RETURNING id
                """,
                (mysql_sql, redshift_sql, vec, used_rules or [],
                 row_count, mysql_ms, redshift_ms, compare_mode, source),
            ).fetchone()[0]
            c.commit()
        return int(rid)
    except Exception as e:
        log.warning("save_example failed: %s", e)
        return None


def list_entries(limit: int = 50, offset: int = 0, search: str = "") -> list[dict]:
    if not is_enabled():
        return []
    with _conn() as c:
        if search:
            rows = c.execute(
                """
                SELECT id, mysql_sql, redshift_sql, used_rules, row_count,
                       mysql_ms, redshift_ms, compare_mode, source, hit_count,
                       created_at, last_used_at
                FROM sql_knowledge
                WHERE mysql_sql ILIKE %s OR redshift_sql ILIKE %s
                ORDER BY last_used_at DESC
                LIMIT %s OFFSET %s
                """,
                (f"%{search}%", f"%{search}%", limit, offset),
            ).fetchall()
        else:
            rows = c.execute(
                """
                SELECT id, mysql_sql, redshift_sql, used_rules, row_count,
                       mysql_ms, redshift_ms, compare_mode, source, hit_count,
                       created_at, last_used_at
                FROM sql_knowledge
                ORDER BY last_used_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            ).fetchall()
        cols = ("id mysql_sql redshift_sql used_rules row_count mysql_ms redshift_ms "
                "compare_mode source hit_count created_at last_used_at").split()
        return [dict(zip(cols, r)) for r in rows]


def count_entries(search: str = "") -> int:
    if not is_enabled():
        return 0
    with _conn() as c:
        if search:
            (n,) = c.execute(
                "SELECT COUNT(*) FROM sql_knowledge WHERE mysql_sql ILIKE %s OR redshift_sql ILIKE %s",
                (f"%{search}%", f"%{search}%"),
            ).fetchone()
        else:
            (n,) = c.execute("SELECT COUNT(*) FROM sql_knowledge").fetchone()
    return int(n)


def delete_entry(entry_id: int) -> bool:
    if not is_enabled():
        return False
    with _conn() as c:
        n = c.execute("DELETE FROM sql_knowledge WHERE id=%s", (entry_id,)).rowcount
        c.commit()
    return n > 0


def format_examples_for_prompt(examples: Iterable[dict]) -> str:
    """把召回结果格式化成 few-shot prompt 片段。"""
    examples = list(examples)
    if not examples:
        return ""
    parts = ["以下是历史成功翻译的相似样例（供参考，请结合当前 SQL 结构独立思考）："]
    for i, ex in enumerate(examples, 1):
        parts.append(
            f"\n示例 {i}（相似度 {ex['similarity']:.2f}）：\n"
            f"MySQL:\n{ex['mysql_sql']}\n"
            f"Redshift:\n{ex['redshift_sql']}\n"
        )
    return "\n".join(parts)
