#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 导出可公开提交的样例数据
------------------------------------
把本地面板导成 `data/sample/*.csv`，让拿到仓库的人不依赖原库也能复算报告数字。

为什么不是整库导出
------------------
两列必须排除：`dim_app_country.descr`（该店面的商店完整介绍）与
`dim_app_country.screenshots`（截图 URL 数组）。前者是各开发者与 Apple 的
文案，公开分发不合适；后者体积大且随时会变。统计与建页都不需要它们
（`dim_app_country.descr_sha1` 保留，足以核对某条标签的证据后来变没变）。

`dim_app.descr` 本来就不导：该列已于 2026-11-05 废弃，只冻结着迁移时的
最后写入国文本。

NULL 的写法
-----------
空字符串与 NULL 在 CSV 里无法区分，这里统一写 `\\N`（Postgres 约定）。
`scripts/load_sample.py` 按 `PRAGMA table_info` 的声明类型还原 NULL 与数值。

用法：
    python scripts/export_sample.py                 # 默认 data/radar.db → data/sample/
    python scripts/export_sample.py --db x.db --out y/

退出码：0 正常 / 1 库不存在 / 2 导出失败
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "radar.db"
OUT_DIR = ROOT / "data" / "sample"

NULL = "\\N"

# (文件名, 表名, 列, 排序键, 本表不导出的列)
# 排序键固定 → 二进制相同的库导出的 CSV 逐字节可比，改动才看得见。
TABLES: list[tuple[str, str, list[str], str, list[str]]] = [
    ("dim_app", "dim_app", [
        "app_id", "bundle_id", "name", "seller", "genre", "genres_all",
        "release_date", "ver_date", "rating_avg", "rating_count", "price",
        "currency", "first_seen", "last_seen"], "app_id",
     ["descr", "summary"]),
    ("dim_app_country", "dim_app_country", [
        "app_id", "country", "release_date", "ver_date", "name", "genre",
        "genres_all", "rating_count", "rating_avg", "price", "currency",
        "descr_sha1", "first_seen", "last_seen"], "app_id, country",
     ["descr", "screenshots"]),
    ("fact_rank", "fact_rank", [
        "snap_date", "country", "chart", "rank", "app_id"],
     "snap_date, country, chart, rank", []),
    ("fact_metric", "fact_metric", [
        "snap_date", "country", "app_id", "rating_count", "rating_avg", "price"],
     "snap_date, country, app_id", []),
    ("dim_genre_map", "dim_genre_map", ["raw", "canonical", "is_top"], "raw", []),
    ("dim_tag_def", "dim_tag_def", [
        "tag_key", "name_cn", "definition", "value_domain", "max_len",
        "source", "multi_value", "ver", "changed_at", "note"], "tag_key", []),
    ("fact_tag", "fact_tag", [
        "app_id", "tag_key", "value", "evidence_strength", "method", "model",
        "tag_ver", "evidence", "descr_country", "descr_sha1", "need_review",
        "tagged_at"], "app_id, tag_key", ["confidence"]),
    ("job_log", "job_log", [
        "run_id", "run_at", "snap_date", "country", "chart", "status",
        "n_rank", "n_detail", "http_code", "duration_s", "note"], "run_id", []),
]

EXCLUDED_REASON = {
    "dim_app.descr": "2026-11-05 起废弃，冻结着最后写入国的文本，任何分析都不读",
    "dim_app.summary": "榜单接口给的一句话简介，与商店介绍同源（单行最长 2KB 以上），页面不读它",
    "dim_app_country.descr": "该店面的商店完整介绍，版权归各开发者与 Apple，不随仓库分发",
    "dim_app_country.screenshots": "截图 URL 数组，体积大且随时会变，页面已按需外链",
    "fact_tag.confidence": "模型自评，无外部基准，schema 注释已声明「不对外使用」",
}


def cell(v):
    if v is None:
        return NULL
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()

    db, out = Path(args.db), Path(args.out)
    if not db.exists():
        print(f"库不存在：{db}")
        return 1
    out.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    manifest_tables = []
    try:
        for fname, table, cols, order, dropped in TABLES:
            sql = f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}"
            cur = conn.execute(sql)
            path = out / f"{fname}.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as fh:
                w = csv.writer(fh, lineterminator="\n")
                w.writerow(cols)
                n = 0
                for row in cur:
                    w.writerow([cell(v) for v in row])
                    n += 1
            manifest_tables.append({
                "table": table,
                "file": path.name,
                "rows": n,
                "columns": cols,
                "excluded_columns": dropped,
                "bytes": path.stat().st_size,
            })
            print(f"  {path.name:<22} {n:>6} 行  {path.stat().st_size / 1024:>7.1f} KB")

        cut = conn.execute("SELECT MAX(snap_date) FROM fact_rank").fetchone()[0]
        stamp = conn.execute("SELECT MAX(run_at) FROM job_log").fetchone()[0]
        panel_start = conn.execute("SELECT MIN(snap_date) FROM fact_rank").fetchone()[0]
        n_days = conn.execute(
            "SELECT COUNT(DISTINCT snap_date) FROM fact_rank").fetchone()[0]
        n_apps = conn.execute("SELECT COUNT(DISTINCT app_id) FROM fact_rank").fetchone()[0]
        n_pairs = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT country, app_id FROM fact_rank)"
        ).fetchone()[0]
    finally:
        conn.close()

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generated_by": "scripts/export_sample.py",
        "source": "data/radar.db（本地采集库，不随仓库分发）",
        "null_token": NULL,
        "panel": {
            "panel_start": panel_start,
            "panel_end": cut,
            "n_days": n_days,
            "n_rank_rows": next((t["rows"] for t in manifest_tables
                                 if t["table"] == "fact_rank"), None),
            "n_apps": n_apps,
            "n_app_country_pairs": n_pairs,
            "snapshot_stamp": stamp,
        },
        "tables": manifest_tables,
        "excluded_reason": EXCLUDED_REASON,
        "load": "python scripts/load_sample.py",
    }
    mpath = out / "MANIFEST.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8", newline="\n")
    print(f"\n已写 {mpath}")
    print(f"面板 {panel_start} → {cut}，{n_days} 个榜日；快照版本 {stamp}")
    print("下一步：python scripts/load_sample.py")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
