#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 用样例 CSV 重建只读面板
------------------------------------
读 `data/sample/MANIFEST.json` 与各表 CSV，按 `schema.sql` 建库并灌数，
得到一份能跑全部 SQL、能跑 `verify_numbers.py` 的本地库。

它不替代正式采集库：样例库不含商店介绍与截图 URL（版权与体积原因），
所以建页时卡片只有标签、没有展开层。要完整复现页面，得自己从头采集。

用法：
    python scripts/load_sample.py                 # → data/sample.db
    python scripts/load_sample.py --force         # 覆盖已存在的样例库
    python scripts/load_sample.py --out /tmp/x.db

退出码：0 正常 / 1 样例数据缺失 / 2 已有库未加 --force / 3 行数与 MANIFEST 不符
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "data" / "sample"
SCHEMA = ROOT / "schema.sql"
OUT_DB = ROOT / "data" / "sample.db"
NULL = "\\N"

# 依赖顺序（当前 schema 未声明外键，仍按维度→事实排，后续加约束时不用改这里）
ORDER = ["dim_genre_map", "dim_tag_def", "dim_app", "dim_app_country",
         "fact_rank", "fact_metric", "fact_tag", "job_log"]


def coerce(value: str, decl_type: str):
    """按声明类型还原 NULL 与数值。CSV 里一切都是字符串，不转会把 rank 当文本，
    `rank <= 20` 这类比较在 SQLite 里会静默变成假（INTEGER 恒小于 TEXT）。"""
    if value == NULL:
        return None
    t = (decl_type or "").upper()
    if "INT" in t:
        return int(value)
    if any(k in t for k in ("REAL", "FLOA", "DOUB", "NUM")):
        return float(value)
    return value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(SAMPLE_DIR))
    ap.add_argument("--out", default=str(OUT_DB))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    src, out = Path(args.dir), Path(args.out)
    mpath = src / "MANIFEST.json"
    if not mpath.exists():
        print(f"找不到 {mpath}；先跑 python scripts/export_sample.py")
        return 1
    manifest = json.loads(mpath.read_text(encoding="utf-8"))

    if out.exists():
        if not args.force:
            print(f"{out} 已存在；要重建加 --force")
            return 2
        out.unlink()
        for suffix in ("-wal", "-shm"):
            stale = out.with_name(out.name + suffix)
            if stale.exists():
                stale.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(out)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))

    declared = {t: {r[1]: r[2] for r in conn.execute(f"PRAGMA table_info({t})")}
                for t in ORDER}
    by_table = {t["table"]: t for t in manifest["tables"]}

    print(f"样例库 {out}")
    for table in ORDER:
        meta = by_table.get(table)
        if not meta:
            print(f"  ⚠ MANIFEST 缺 {table}，跳过")
            continue
        path = src / meta["file"]
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            cols = reader.fieldnames or []
            unknown = [c for c in cols if c not in declared[table]]
            if unknown:
                print(f"  ✗ {table} 有 schema 里不存在的列：{unknown}")
                return 3
            rows = [[coerce(r[c], declared[table][c]) for c in cols] for r in reader]
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})", rows)
        expect = meta["rows"]
        got = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        flag = "✓" if got == expect else "✗"
        print(f"  {flag} {table:<16} {got:>6} / {expect:>6} 行")
        if got != expect:
            conn.close()
            return 3

    conn.commit()
    p = manifest["panel"]
    checks = {
        "榜日数": "SELECT COUNT(DISTINCT snap_date) FROM fact_rank",
        "榜位行数": "SELECT COUNT(*) FROM fact_rank",
        "在榜产品数": "SELECT COUNT(DISTINCT app_id) FROM fact_rank",
        "产品×店面数": "SELECT COUNT(*) FROM (SELECT DISTINCT country, app_id FROM fact_rank)",
    }
    print()
    for label, sql in checks.items():
        got = conn.execute(sql).fetchone()[0]
        print(f"  {label:<12}{got:>7}")
    conn.close()

    print(f"\n样例数据来自 {p['panel_start']} → {p['panel_end']}"
          f"（{p['n_days']} 个榜日，快照版本 {p['snapshot_stamp']}）")
    print("下一步：")
    print(f"  python scripts/verify_numbers.py --db {out}")
    print(f"  python scripts/run_sql.py sql/07_cohort_survival.sql --db {out}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
