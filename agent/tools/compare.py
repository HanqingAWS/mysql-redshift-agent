"""MySQL vs Redshift 结果集对比（严格/宽松模式）。

strict：列数+行数+逐行逐字段完全一致（字符串 trim + 数值 epsilon + NULL 对齐）
lenient：仅行数一致（用于回执无法严格对齐的场景）
"""
from __future__ import annotations

import decimal
import math
from datetime import date, datetime
from typing import Any


def _normalize(v: Any) -> Any:
    """把一个字段归一化到可比较的形态。"""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8", errors="replace").strip()
        except Exception:
            return v.hex()
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v


def _eq(a: Any, b: Any) -> bool:
    a = _normalize(a)
    b = _normalize(b)
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    # 浮点：1e-9 epsilon
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        try:
            return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
        except Exception:
            return a == b
    return a == b


def compare(rows_my: list[tuple], rows_rs: list[tuple],
            cols_my: list[str], cols_rs: list[str],
            mode: str = "strict") -> dict:
    """对比两侧结果。返回 {ok: bool, reason: str, diff_rows: int}。"""
    if mode == "skipped":
        return {"ok": True, "reason": "compare skipped by user"}

    if len(rows_my) != len(rows_rs):
        return {
            "ok": False,
            "reason": f"行数不一致：MySQL={len(rows_my)} Redshift={len(rows_rs)}",
            "diff_rows": abs(len(rows_my) - len(rows_rs)),
        }

    if mode == "lenient":
        return {"ok": True, "reason": f"行数一致 ({len(rows_my)} 行)"}

    # strict
    if len(cols_my) != len(cols_rs):
        return {"ok": False, "reason": f"列数不一致：MySQL={len(cols_my)} Redshift={len(cols_rs)}"}
    # 列名大小写不敏感对齐
    if [c.lower() for c in cols_my] != [c.lower() for c in cols_rs]:
        return {"ok": False, "reason": f"列名不一致：{cols_my} vs {cols_rs}"}

    # 按行排序（防止 ORDER BY 缺失导致顺序差异）
    try:
        key = lambda r: tuple(str(_normalize(v)) for v in r)
        sorted_my = sorted(rows_my, key=key)
        sorted_rs = sorted(rows_rs, key=key)
    except Exception as e:
        return {"ok": False, "reason": f"排序失败（可能含不可排序类型）：{e}"}

    diff_count = 0
    first_diff = None
    for i, (rm, rr) in enumerate(zip(sorted_my, sorted_rs)):
        if len(rm) != len(rr):
            return {"ok": False, "reason": f"第 {i+1} 行列数不一致"}
        for j, (a, b) in enumerate(zip(rm, rr)):
            if not _eq(a, b):
                diff_count += 1
                if first_diff is None:
                    first_diff = (i + 1, cols_my[j], a, b)
                break  # 每行只计一次

    if diff_count == 0:
        return {"ok": True, "reason": f"严格一致 ({len(rows_my)} 行)"}
    else:
        i, col, a, b = first_diff
        return {
            "ok": False,
            "reason": f"{diff_count}/{len(rows_my)} 行有差异；首差：行{i} 列{col!r} MySQL={a!r} Redshift={b!r}",
            "diff_rows": diff_count,
        }
