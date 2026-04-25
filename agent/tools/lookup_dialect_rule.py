"""
Strands Tool：按 SQL 里的关键词，从 references/ 目录检索方言规则文档。

设计思路：
- 不走向量检索，直接字符串/正则匹配
- 返回匹配到的 .md 原文片段拼接
- Agent 在拿到 reference 后自行决定如何翻译
"""
import re
from pathlib import Path

from strands.tools import tool

# references 目录（相对 agent/ 根）
REFS_DIR = Path(__file__).resolve().parent.parent / "references"


# 关键词 → 参考文件（不依赖 _index.md 的表格解析，直接硬编码，可靠）
KEYWORD_RULES: list[tuple[str, str]] = [
    (r"\bdw\.", "schema_prefix.md"),
    (r"`", "backticks.md"),
    (r"\blimit\s+\d+\s*,\s*\d+", "limit_offset.md"),
    (r"\bIFNULL\s*\(", "ifnull.md"),
    (r"\bGROUP_CONCAT\s*\(", "group_concat.md"),
    (r"\bDATE_FORMAT\s*\(", "date_format.md"),
    (r"\b(DATE_ADD|DATE_SUB|ADDDATE|SUBDATE)\s*\(", "date_arith.md"),
    (r"\bON\s+DUPLICATE\s+KEY", "on_duplicate_key.md"),
    (r"\b(TINYINT|MEDIUMTEXT|DATETIME)\b", "types.md"),
    (r"\bSTR_TO_DATE\s*\(", "str_to_date.md"),
    (r"\b(UNIX_TIMESTAMP|FROM_UNIXTIME)\s*\(", "unix_timestamp.md"),
    (r"\bCONCAT_WS\s*\(", "concat_ws.md"),
]


def match_references(sql: str) -> list[str]:
    """根据 SQL 内容返回命中的 reference 文件名列表（去重、保序）"""
    hit: list[str] = []
    for pattern, fname in KEYWORD_RULES:
        if re.search(pattern, sql, flags=re.IGNORECASE) and fname not in hit:
            hit.append(fname)
    return hit


def load_reference(fname: str) -> str:
    p = REFS_DIR / fname
    if not p.exists():
        return f"(reference not found: {fname})"
    return p.read_text(encoding="utf-8")


@tool
def lookup_dialect_rule(sql: str) -> str:
    """查找与给定 SQL 相关的 MySQL → Redshift 方言差异规则。

    用法：在 SQL 翻译前调用本工具，把命中的规则文档拼接返回。
    参数：
        sql: 待翻译的 MySQL SQL 原文
    返回：命中的 reference markdown 原文拼接，或"no rules matched"。
    """
    hits = match_references(sql)
    if not hits:
        return "no dialect rules matched; translate by general MySQL→Redshift knowledge."
    sections = [f"# Rule: {fn}\n\n{load_reference(fn)}" for fn in hits]
    return "\n\n---\n\n".join(sections)


@tool
def list_all_rules() -> str:
    """列出全部已知方言规则文件名和简介，用于 Agent 自主探索。"""
    out = []
    for p in sorted(REFS_DIR.glob("*.md")):
        first_line = p.read_text(encoding="utf-8").splitlines()[0]
        out.append(f"- {p.name}: {first_line}")
    return "\n".join(out)
