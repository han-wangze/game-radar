#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 回填「产品 × 店面」维度（dim_app_country）
--------------------------------------------------------
背景：Apple 的 releaseDate / userRatingCount / price 都是**分店面**口径，
      同一 app 在四国返回不同值。原来这些字段只能塞进「一产品一行」的 dim_app，
      结果是最后抓取国（kr）覆盖前面所有国家 → 跨市场年龄分析会串味。

为什么可以一次性回填、不存在历史损失：
  `releaseDate` 是产品的**静态属性**（不像排名每天变），重新查一次就能拿到该店面的真实值，
  所以历史上的每个榜日都能被正确重算。

用法：
    python scripts/backfill_app_country.py              # 四国全量回填
    python scripts/backfill_app_country.py --dry-run    # 只查不写，打印差异统计
    python scripts/backfill_app_country.py --countries us

退出码：0 正常 / 1 库不存在
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_daily import (  # noqa: E402
    DB_PATH, SCHEMA_PATH, UPSERT_APP_COUNTRY, fetch_details,
)

COUNTRIES = ["us", "cn", "jp", "kr"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--countries", nargs="+", default=COUNTRIES, choices=COUNTRIES)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"库不存在：{DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))  # 确保新表/视图存在
    conn.row_factory = sqlite3.Row

    grand_total = {"pairs": 0, "filled": 0, "missing": 0, "changed": 0}

    for cc in args.countries:
        ids = [r["app_id"] for r in conn.execute(
            "SELECT DISTINCT app_id FROM fact_rank WHERE country = ? ORDER BY app_id", (cc,))]
        if not ids:
            print(f"[{cc}] 库里没有该国家的榜记录，跳过")
            continue

        spans = {r["app_id"]: (r["first_seen"], r["last_seen"]) for r in conn.execute(
            "SELECT app_id, first_seen, last_seen FROM v_entry WHERE country = ?", (cc,))}
        old = {r["app_id"]: r["release_date"] for r in conn.execute(
            "SELECT app_id, release_date FROM dim_app")}

        print(f"[{cc}] {len(ids)} 个 (产品 × 店面) 待回填，开始查 {cc} 店面…")
        detail = fetch_details(cc, ids)

        filled = missing = changed = 0
        for aid in ids:
            d = detail.get(aid)
            if not d:
                # 该 app 在这个店面取不到（通常是在该店面不可用）→ 不伪造，留空
                missing += 1
                if not args.dry_run:
                    fs, ls = spans.get(aid, (None, None))
                    conn.execute(UPSERT_APP_COUNTRY, (
                        aid, cc, None, None, None, None, None, None, None, None, None, fs, ls))
                continue
            filled += 1
            if old.get(aid) and d.get("release_date") and old[aid] != d["release_date"]:
                changed += 1
            if not args.dry_run:
                fs, ls = spans.get(aid, (None, None))
                conn.execute(UPSERT_APP_COUNTRY, (
                    aid, cc, d.get("release_date"), d.get("ver_date"), d.get("name"),
                    d.get("genre"), d.get("genres_all"), d.get("rating_count"),
                    d.get("rating_avg"), d.get("price"), d.get("currency"), fs, ls))

        print(f"[{cc}] 取到 {filled}/{len(ids)}；取不到 {missing}；"
              f"上架日与旧 dim_app 值不同 {changed} 条")
        for k, v in (("pairs", len(ids)), ("filled", filled),
                     ("missing", missing), ("changed", changed)):
            grand_total[k] += v

    if not args.dry_run:
        conn.commit()

    print(f"\n合计：{grand_total['pairs']} 个 (产品 × 店面)；取到 {grand_total['filled']}，"
          f"取不到 {grand_total['missing']}，上架日被纠正 {grand_total['changed']} 条"
          + ("（dry-run，未写库）" if args.dry_run else ""))

    # 覆盖率
    tot = conn.execute("SELECT COUNT(*) FROM fact_rank r "
                       "LEFT JOIN dim_app_country ac ON ac.app_id=r.app_id AND ac.country=r.country "
                       "WHERE ac.app_id IS NULL").fetchone()[0]
    n_rank = conn.execute("SELECT COUNT(*) FROM fact_rank").fetchone()[0]
    print(f"榜单行里仍缺 dim_app_country 记录的：{tot}/{n_rank}")
    conn.close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
