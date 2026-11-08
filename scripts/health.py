#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 运行健康检查
--------------------------
回答三个问题：跑了几天？有没有缺日？最近的抓取是否完整？

用法：python scripts/health.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "radar.db"


def main() -> int:
    if not DB_PATH.exists():
        print(f"库不存在：{DB_PATH}")
        return 1
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    days = [r["snap_date"] for r in conn.execute(
        "SELECT DISTINCT snap_date FROM fact_rank ORDER BY snap_date")]
    if not days:
        print("还没有任何榜单数据")
        return 1

    first, last = days[0], days[-1]
    span = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
    print(f"榜单日：{first} → {last}   共 {len(days)} 天 / 跨度 {span} 天")

    have = set(days)
    missing = [(date.fromisoformat(first) + timedelta(days=i)).isoformat()
               for i in range(span)
               if (date.fromisoformat(first) + timedelta(days=i)).isoformat() not in have]
    print(f"缺日：{len(missing)} 天" + (f"  → {missing}" if missing else "  ✓"))

    print("\n每日每国记录数：")
    for r in conn.execute("""
        SELECT snap_date, country, COUNT(*) n,
               SUM(app_id IS NULL) bad
        FROM fact_rank GROUP BY snap_date, country
        ORDER BY snap_date DESC, country LIMIT 12"""):
        flag = "✓" if r["n"] == 100 else "⚠"
        print(f"  {r['snap_date']}  {r['country']}  {r['n']:>3} 条 {flag}")

    print("\n最近 8 次任务：")
    for r in conn.execute("""
        SELECT run_at, country, chart, status, n_rank, n_detail, duration_s
        FROM job_log ORDER BY run_id DESC LIMIT 8"""):
        print(f"  {r['run_at']}  {r['country']}/{r['chart']}  {r['status']:<7} "
              f"{r['n_detail']}/{r['n_rank']}  {r['duration_s']}s")

    n_app = conn.execute("SELECT COUNT(*) FROM dim_app").fetchone()[0]
    n_map = conn.execute("SELECT COUNT(*) FROM dim_genre_map").fetchone()[0]
    # 注意：Unclassified 不是 NULL，若只数非 NULL 会得到 100% 的假象，必须单列
    n_unc = conn.execute("SELECT COUNT(*) FROM v_rank_sub WHERE sub_genre = 'Unclassified'").fetchone()[0]
    n_all = conn.execute("SELECT COUNT(*) FROM v_rank_sub").fetchone()[0]
    n_null = conn.execute("SELECT COUNT(*) FROM v_rank_sub WHERE sub_genre IS NULL").fetchone()[0]
    print(f"\n产品库 {n_app} 个；品类字典 {n_map} 条")
    print(f"子品类可分类 {n_all - n_unc - n_null}/{n_all}"
          f"；Unclassified {n_unc}（上游只返回顶级类，非字典缺失，不计入品类排名）"
          + (f"；NULL {n_null}（异常）" if n_null else ""))

    # 分店面维度覆盖率：缺失 = 该榜行没有 (产品 × 店面) 记录，age/评分数会取不到
    n_cc = conn.execute("SELECT COUNT(*) FROM dim_app_country").fetchone()[0]
    n_rank_missing = conn.execute(
        "SELECT COUNT(*) FROM fact_rank r "
        "LEFT JOIN dim_app_country ac ON ac.app_id = r.app_id AND ac.country = r.country "
        "WHERE ac.app_id IS NULL").fetchone()[0]
    print(f"分店面维度 dim_app_country：{n_cc} 行；榜单行缺记录 {n_rank_missing}/{n_all}"
          + ("  ✓" if n_rank_missing == 0 else "  ⚠ 跑 backfill_app_country.py"))

    # 新游口径 = 首次进榜（不是上架 30 天内）：窗口只有「榜日数 - 1」天，起点日左删失
    panel = conn.execute("SELECT * FROM v_panel").fetchone()
    if panel and panel["n_days"]:
        n_new = conn.execute("SELECT COUNT(*) FROM v_new_entrant").fetchone()[0]
        n_cen = conn.execute("SELECT COUNT(*) FROM v_entry WHERE is_censored = 1").fetchone()[0]
        print(f"新游（首次进榜）：可观测窗口 {panel['n_days'] - 1} 天，累计 {n_new} 条；"
              f"起点日在榜 {n_cen} 条属左删失，不计入")
    print(f"检查时间 {datetime.now().isoformat(timespec='seconds')}")
    conn.close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
