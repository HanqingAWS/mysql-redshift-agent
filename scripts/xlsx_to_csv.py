#!/usr/bin/env python3
"""
把 table-schema.xlsx 的 Results sheet 导出成 CSV
- sample_data.csv：纯数据，供 Aurora LOAD DATA 使用（tab 分隔 + 无 header）
- sample_data_header.csv：带 header 的 CSV（人看或 Redshift COPY 备用）
"""
import csv
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "table-schema.xlsx"
OUT_DIR = ROOT / "scripts" / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TSV_NO_HEADER = OUT_DIR / "sample_data.tsv"
CSV_WITH_HEADER = OUT_DIR / "sample_data.csv"


def main():
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb["Results"]
    rows = list(ws.iter_rows(values_only=True))
    header = rows[0]
    data = rows[1:]

    # 1) TSV (tab-separated, no header) for Aurora LOAD DATA
    with TSV_NO_HEADER.open("w", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        for row in data:
            # MySQL 习惯 \N 表示 NULL；LOAD DATA LOCAL INFILE 默认用 \N
            w.writerow(["\\N" if v is None else v for v in row])

    # 2) CSV (comma, with header) for Redshift COPY / human readable
    with CSV_WITH_HEADER.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in data:
            w.writerow(["" if v is None else v for v in row])

    print(f"[csv] wrote {len(data)} rows")
    print(f"  {TSV_NO_HEADER}  (Aurora LOAD DATA: tab, \\N for NULL, no header)")
    print(f"  {CSV_WITH_HEADER}  (Redshift COPY CSV: comma, with header)")
    print(f"\ncolumns ({len(header)}): {', '.join(header)}")


if __name__ == "__main__":
    main()
