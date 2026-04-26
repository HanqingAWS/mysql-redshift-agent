#!/usr/bin/env python3
"""Generate ~50M IAP payment rows with a realistic UID distribution and
stream them to Aurora MySQL via LOAD DATA LOCAL INFILE (from stdin).

UID distribution (requested):
  80% of UIDs → 1-5 rows   (normal players)
  15% of UIDs → 10-50 rows (mid whales)
  5%  of UIDs → 100-500 rows (large whales, stress bound)

Target: ~50,000,000 rows total.
We pick UID_COUNT so that the expected order count ≈ 50M.

Output:
  TSV via stdout (null-safe), pipe to `mysql --local-infile=1 ... -e "LOAD DATA LOCAL INFILE '/dev/stdin' INTO TABLE ..."`.

Column order (TAB-separated, matches iap_orders_5000w / ads_thor schema):
  game_name, uid, transaction_id, event_time, fpid, app_id,
  device_platform, country_code, gameserver_id, app_language,
  device_level, city_level, amount, is_white_user, new_app_id,
  payment_processor, iap_product_id, iap_product_name, base_price,
  iap_product_name_cn, app_version, currency, order_id, ts

Usage:
  python3 gen_50m.py --uids 2280000 > /tmp/orders.tsv
  # or stream directly:
  python3 gen_50m.py --uids 2280000 | mysql --local-infile=1 ...
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timedelta

PROCESSORS = ["googleplay", "applepay", "mc_googleplayiap", "mc_applepayiap",
              "centurygames_pay", "centurygames_store", "kg3rdpartypayment"]
APP_IDS = ["1", "2", "alpha.alpha_01.p", "alpha.global.pro", "beta.global"]
COUNTRIES = ["US", "JP", "KR", "CN", "DE", "FR", "BR", "RU", "IN", "ID", "TH", "VN"]
PLATFORMS = ["android", "ios"]
LANGS = ["en", "ja", "ko", "zh", "de", "fr", "pt", "ru"]
PRICE_POINTS = [0.99, 1.99, 2.99, 4.99, 9.99, 19.99, 49.99, 99.99]
VERSIONS = ["100.120.0", "100.121.0", "100.122.0", "100.123.0", "100.124.0"]
CURRENCIES = ["USD", "EUR", "JPY", "KRW", "CNY"]
GAME_NAMES = ["thor", "asgard", "valhalla"]
IAP_PRODUCTS = [
    ("weekly_pass_001", "Weekly Pass", "周卡"),
    ("monthly_pass_001", "Monthly Pass", "月卡"),
    ("gem_pack_s", "Gem Pack S", "小宝石包"),
    ("gem_pack_m", "Gem Pack M", "中宝石包"),
    ("gem_pack_l", "Gem Pack L", "大宝石包"),
    ("gem_pack_xl", "Gem Pack XL", "豪华宝石包"),
    ("starter_pack", "Starter Pack", "新手礼包"),
    ("vip_pack_1", "VIP Pack 1", "VIP礼包1"),
    ("vip_pack_2", "VIP Pack 2", "VIP礼包2"),
    ("special_offer", "Special Offer", "特惠礼包"),
]


def pick_order_count(rnd: random.Random) -> int:
    """80/15/5 tier distribution."""
    r = rnd.random()
    if r < 0.80:
        return rnd.randint(1, 5)
    if r < 0.95:
        return rnd.randint(10, 50)
    return rnd.randint(100, 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uids", type=int, default=2_280_000,
                    help="number of distinct UIDs (tune for target row count)")
    ap.add_argument("--target-rows", type=int, default=50_000_000,
                    help="stop after producing this many rows (hard cap)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--progress-every", type=int, default=5_000_000)
    args = ap.parse_args()

    rnd = random.Random(args.seed)
    # event_time spans Jan 1 2023 → Dec 31 2023 (1 year of orders)
    t_start = datetime(2023, 1, 1)
    t_end = datetime(2024, 1, 1)
    span_sec = int((t_end - t_start).total_seconds())

    out = sys.stdout
    write = out.write
    n = 0
    t0 = time.time()

    # Pre-pick tier per UID (deterministic)
    for u_idx in range(args.uids):
        if n >= args.target_rows:
            break
        uid = str(100_000_000 + u_idx)  # 9-digit uid
        fpid = rnd.randint(10**12, 10**13 - 1)
        country = rnd.choice(COUNTRIES)
        platform = rnd.choice(PLATFORMS)
        lang = rnd.choice(LANGS)
        device_level = rnd.randint(1, 5)
        city_level = rnd.randint(1, 7)
        version = rnd.choice(VERSIONS)
        currency = rnd.choice(CURRENCIES)
        is_white_user = 1 if rnd.random() < 0.05 else 0
        game_name = rnd.choice(GAME_NAMES)
        app_id = rnd.choice(APP_IDS)
        gameserver_id = str(rnd.randint(1, 200))

        order_count = pick_order_count(rnd)
        if n + order_count > args.target_rows:
            order_count = args.target_rows - n

        for _ in range(order_count):
            base_price = rnd.choice(PRICE_POINTS)
            # amount = base_price * (1 + VAT/fee jitter)
            amount = round(base_price * rnd.uniform(1.0, 1.15), 4)
            prod_id, prod_name, prod_name_cn = rnd.choice(IAP_PRODUCTS)
            processor = rnd.choice(PROCESSORS)
            dt = t_start + timedelta(seconds=rnd.randint(0, span_sec - 1))
            event_time = dt.strftime("%Y-%m-%d %H:%M:%S")
            ts_ms = int(dt.timestamp() * 1000)
            # transaction_id ~ fpid-style digits
            tx_id = str(rnd.randint(10**15, 10**16 - 1))
            order_id = f"{rnd.randint(10**25, 10**26 - 1)}"

            # TAB-separated. Use \N for NULL (none here).
            write(
                f"{game_name}\t{uid}\t{tx_id}\t{event_time}\t{fpid}\t{app_id}\t"
                f"{platform}\t{country}\t{gameserver_id}\t{lang}\t"
                f"{device_level}\t{city_level}\t{amount}\t{is_white_user}\t{app_id}\t"
                f"{processor}\t{prod_id}\t{prod_name}\t{base_price}\t"
                f"{prod_name_cn}\t{version}\t{currency}\t{order_id}\t{ts_ms}\n"
            )
            n += 1

        if args.progress_every and n // args.progress_every != (n - order_count) // args.progress_every:
            elapsed = time.time() - t0
            rate = n / elapsed if elapsed > 0 else 0
            sys.stderr.write(f"[gen] {n:,} rows  {elapsed:.0f}s  {rate/1e6:.2f}M rows/s\n")
            sys.stderr.flush()

    elapsed = time.time() - t0
    sys.stderr.write(f"[gen] DONE {n:,} rows in {elapsed:.0f}s ({n/elapsed/1e6:.2f}M rows/s)\n")


if __name__ == "__main__":
    main()
