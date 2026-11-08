#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 新游（首次进榜）报告
----------------------------------
口径：**新游 = 首次进榜**，不是「上架 30 天内」。
  依据 v_new_entrant（见 schema.sql 末尾的说明）：按（国家 × 榜 × 产品）取
  面板内的 MIN(snap_date)，并排除面板起点当天（那些是左删失「早就在榜」）。

为什么不用 releaseDate：
  - releaseDate 是分店面口径，dim_app 只存一列且被最后抓取国覆盖 → age 会串味；
  - 「上架 30 天内」测的是上架新，不是进榜新；且在美区恒为 0（最小 age 75 天）。

用法：
    python scripts/new_entrants.py                  # 全窗口，含明细
    python scripts/new_entrants.py --days 2         # 只看最近 2 天
    python scripts/new_entrants.py --country us     # 只看某个国家
    python scripts/new_entrants.py --no-detail      # 只看汇总

退出码：0 正常 / 1 库不存在或无数据
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "radar.db"

COUNTRIES = ["us", "cn", "jp", "kr"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只看最近 N 个榜日（0=全窗口）")
    ap.add_argument("--country", choices=COUNTRIES, help="只看某个国家")
    ap.add_argument("--no-detail", action="store_true", help="不打印明细")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"库不存在：{DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    panel = conn.execute("SELECT * FROM v_panel").fetchone()
    if not panel or not panel["panel_start"]:
        print("还没有任何榜单数据")
        return 1

    days = [r["snap_date"] for r in conn.execute(
        "SELECT DISTINCT snap_date FROM fact_rank ORDER BY snap_date DESC")]
    window = days[:args.days] if args.days > 0 else days
    window = sorted(window)
    entry_from = window[0]

    # 起点日的在榜产品是左删失，不能算新游
    n_censored = conn.execute(
        "SELECT COUNT(*) FROM v_entry WHERE is_censored = 1").fetchone()[0]

    print(f"面板：{panel['panel_start']} → {panel['panel_end']}"
          f"（{panel['n_days']} 个榜日）")
    print(f"口径：新游 = 首次进榜；起点日在榜 {n_censored} 条属左删失，不计入")
    if args.days > 0:
        print(f"窗口：最近 {args.days} 个榜日（{window[0]} → {window[-1]}）")
    print(f"可观测进榜窗口：{panel['n_days'] - 1} 天"
          + ("（不足 14 天，先别看存活率）" if panel["n_days"] < 14 else ""))

    # ── 每日新进榜数 ──────────────────────────────────────────
    print("\n每日新进榜（按国家）：")
    where = ["entry_date >= ?"]
    params: list = [entry_from]
    if args.country:
        where.append("country = ?")
        params.append(args.country)
    sql = f"""
        SELECT entry_date,
               SUM(country='us') us, SUM(country='cn') cn,
               SUM(country='jp') jp, SUM(country='kr') kr,
               COUNT(*) total
        FROM v_new_entrant WHERE {' AND '.join(where)}
        GROUP BY entry_date ORDER BY entry_date"""
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("  窗口内没有新进榜产品")
        return 0
    print(f"  {'日期':<12}{'us':>4}{'cn':>4}{'jp':>4}{'kr':>4}{'合计':>6}")
    for r in rows:
        print(f"  {r['entry_date']:<12}{r['us'] or 0:>4}{r['cn'] or 0:>4}"
              f"{r['jp'] or 0:>4}{r['kr'] or 0:>4}{r['total']:>6}")

    tot = conn.execute(
        f"SELECT COUNT(*) n, COUNT(DISTINCT app_id) uniq FROM v_new_entrant "
        f"WHERE {' AND '.join(where)}", params).fetchone()
    print(f"  累计 {tot['n']} 条（去重产品 {tot['uniq']} 个）")

    # ── 明细 ──────────────────────────────────────────────────
    if not args.no_detail:
        print("\n新进榜明细（按进榜日倒序；名次=首次被看到时的榜位）：")
        detail = conn.execute(f"""
            SELECT * FROM v_new_entrant WHERE {' AND '.join(where)}
            ORDER BY entry_date DESC, country, entry_rank""", params).fetchall()
        cur = None
        for r in detail:
            if r["country"] != cur:
                cur = r["country"]
                print(f"\n  [{cur}]")
            rc = r["rating_count"] if r["rating_count"] is not None else "?"
            # age_days 只作参考：release_date 可能是别的店面的上架日
            print(f"    {r['entry_date']}  #{r['entry_rank']:<3} 评价{str(rc):>8}  "
                  f"{r['name'][:44]}")

    # ── 提醒 ──────────────────────────────────────────────────
    still = conn.execute(f"""
        SELECT COUNT(*) n FROM v_new_entrant ne
        WHERE {' AND '.join(where)}
          AND EXISTS (SELECT 1 FROM fact_rank f
                      WHERE f.snap_date = (SELECT MAX(snap_date) FROM fact_rank)
                        AND f.country = ne.country AND f.app_id = ne.app_id)""",
        params).fetchone()["n"]
    print(f"\n其中 {still}/{tot['n']} 在最新榜日仍在榜"
          f"（其余已被挤出，属正常轮换；存活曲线要等面板够长）")
    conn.close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
