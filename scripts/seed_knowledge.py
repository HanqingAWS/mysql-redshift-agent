#!/usr/bin/env python3
"""预热 pgvector 知识库：用少量已经人工验证过的样本作为种子。
调用 agent 的 /save_example 接口（不走 import_test，那条路径会实际执行，种子阶段直接写）。
"""
import json
import os
import sys
import urllib.request

AGENT_URL = os.environ.get("AGENT_URL", "http://localhost:8088")

# 人工梳理的 MySQL → Redshift 样本对（来自 dialect_tests，已确认 Redshift 能跑）
SEEDS = [
    {
        "mysql": "SELECT COUNT(*) AS total FROM dw.ads_thor_fin_payment_iap_data_new",
        "redshift": "SELECT COUNT(*) AS total FROM ads_thor_fin_payment_iap_data_new",
        "rules": ["schema_prefix.md"],
    },
    {
        "mysql": "SELECT `uid`, `amount` FROM dw.ads_thor_fin_payment_iap_data_new LIMIT 3",
        "redshift": 'SELECT "uid", "amount" FROM ads_thor_fin_payment_iap_data_new LIMIT 3',
        "rules": ["backticks.md", "schema_prefix.md"],
    },
    {
        "mysql": "SELECT uid, ts FROM dw.ads_thor_fin_payment_iap_data_new ORDER BY ts LIMIT 2, 3",
        "redshift": "SELECT uid, ts FROM ads_thor_fin_payment_iap_data_new ORDER BY ts LIMIT 3 OFFSET 2",
        "rules": ["limit_offset.md", "schema_prefix.md"],
    },
    {
        "mysql": "SELECT uid, IFNULL(app_version, 'unknown') AS ver FROM dw.ads_thor_fin_payment_iap_data_new LIMIT 3",
        "redshift": "SELECT uid, COALESCE(app_version, 'unknown') AS ver FROM ads_thor_fin_payment_iap_data_new LIMIT 3",
        "rules": ["ifnull.md", "schema_prefix.md"],
    },
    {
        "mysql": "SELECT DATE_FORMAT(event_time, '%Y-%m-%d') AS day, COUNT(*) c FROM dw.ads_thor_fin_payment_iap_data_new GROUP BY day ORDER BY day LIMIT 3",
        "redshift": "SELECT TO_CHAR(event_time, 'YYYY-MM-DD') AS day, COUNT(*) c FROM ads_thor_fin_payment_iap_data_new GROUP BY day ORDER BY day LIMIT 3",
        "rules": ["date_format.md", "schema_prefix.md"],
    },
    {
        "mysql": "SELECT payment_processor, GROUP_CONCAT(DISTINCT app_id) AS apps FROM dw.ads_thor_fin_payment_iap_data_new GROUP BY payment_processor",
        "redshift": "SELECT payment_processor, LISTAGG(DISTINCT app_id, ',') WITHIN GROUP (ORDER BY app_id) AS apps FROM ads_thor_fin_payment_iap_data_new GROUP BY payment_processor",
        "rules": ["group_concat.md", "schema_prefix.md"],
    },
]


def post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main():
    ok, fail = 0, 0
    for i, s in enumerate(SEEDS, 1):
        try:
            r = post(f"{AGENT_URL}/save_example", {
                "mysql_sql": s["mysql"],
                "redshift_sql": s["redshift"],
                "used_rules": s["rules"],
                "compare_mode": "skipped",
                "source": "seed",
            })
            if r.get("ok"):
                ok += 1
                print(f"[{i:2}/{len(SEEDS)}] ✓ id={r.get('id')}  {s['mysql'][:80]}")
            else:
                fail += 1
                print(f"[{i:2}/{len(SEEDS)}] ✗ save returned ok=false")
        except Exception as e:
            fail += 1
            print(f"[{i:2}/{len(SEEDS)}] ✗ {e}")
    print(f"\n=== {ok}/{len(SEEDS)} seeded, {fail} failed ===")
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()
