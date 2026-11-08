#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / SQL 跑数器
------------------------
把 sql/*.sql 直接跑在 data/radar.db 上并打印结果，用于复现与验收。

用法：
    python scripts/run_sql.py sql/01_gap_check.sql
    python scripts/run_sql.py sql/02_rank_moves.sql --limit 20
    python scripts/run_sql.py sql/02_rank_moves.sql --csv out/rank_moves.csv
    python scripts/run_sql.py sql/01_gap_check.sql --db data/_tmp.db   # 跑副本，不动主库

约定：一个 .sql 文件只放一条语句（以 ; 结尾），结果按行打印。
退出码：0 正常 / 1 文件或库不存在 / 2 SQL 出错
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "radar.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sql_file")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--limit", type=int, default=0, help="只打印前 N 行（0=全部）")
    ap.add_argument("--csv", help="导出 CSV 的路径")
    ap.add_argument("--quiet", action="store_true", help="只打印行数，不打表")
    ap.add_argument("--cut", default=None,
                    help="数据截点（含）。含 :cut 的 SQL（如 07）缺省回退到库内最新日")
    args = ap.parse_args()

    sql_path = Path(args.sql_file)
    if not sql_path.exists():
        print(f"SQL 文件不存在：{sql_path}")
        return 1
    if not Path(args.db).exists():
        print(f"库不存在：{args.db}")
        return 1

    sql = sql_path.read_text(encoding="utf-8")
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    # 含 :cut 的 SQL 必须绑定；不传 --cut 时回退到库内最新日，保持旧行为（当前面板）
    params: dict = {}
    if ":cut" in sql:
        max_date = conn.execute(
            "SELECT MAX(snap_date) FROM fact_rank").fetchone()[0]
        params["cut"] = args.cut or max_date
    try:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    except sqlite3.Error as exc:
        print(f"SQL 出错：{exc}")
        return 2

    print(f"-- {sql_path.name}  |  {len(rows)} 行" + (f"  |  列：{', '.join(cols)}" if cols else ""))
    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
        print(f"-- 已导出 {out}")

    if args.quiet or not cols:
        conn.close()
        return 0

    show = rows[:args.limit] if args.limit else rows
    widths = [max(len(str(c)), *(len(str(r[i])) for r in show)) if show else len(str(c))
              for i, c in enumerate(cols)]
    print("  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cols)))
    print("  " + "  ".join("-" * widths[i] for i in range(len(cols))))
    for r in show:
        print("  " + "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(r)))
    if args.limit and len(rows) > args.limit:
        print(f"  … 另有 {len(rows) - args.limit} 行（用 --csv 导出全部）")
    conn.close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
