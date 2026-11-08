#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 标签体系 v2 → v3 迁移
-----------------------------------
为什么需要单独一个脚本：schema.sql 里标签三表用的是 `CREATE TABLE IF NOT EXISTS`，
对**已存在**的表不会新增或修改列。v3 改了 dim_tag_def 的列（value_domain 允许 NULL、
新增 max_len / source）和 fact_tag 的主键与列（新增 evidence_strength），
光跑 schema.sql 会静默跳过——2026-10-27 在视图上踩过同一个坑，这里不重犯。

安全性：只重建标签三张表（dim_tag_def / fact_tag / fact_tag_review），
不碰 dim_app / dim_app_country / fact_rank / fact_metric / dim_genre_map / job_log。
fact_tag 非空时默认拒绝执行（v3 与 v2 的值域不兼容，混着会算错），要执行需显式 --force。

用法：
    python scripts/migrate_tag_v3.py --dry-run
    python scripts/migrate_tag_v3.py
    python scripts/migrate_tag_v3.py --force      # 已有标签数据仍然重建（会丢，先备份）
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "radar.db"
SCHEMA_PATH = ROOT / "schema.sql"
BACKUP_DIR = ROOT.parent / "game-radar-backups"
TAG_TABLES = ["fact_tag_review", "fact_tag", "dim_tag_def"]


def columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def pk(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[5]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    db = Path(args.db)
    if not db.exists():
        print(f"库不存在：{db}")
        return 1

    conn = sqlite3.connect(db)
    n_tag = conn.execute("SELECT COUNT(*) FROM fact_tag").fetchone()[0] \
        if "fact_tag" in [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")] else 0

    print("=== 迁移前 ===")
    for t in TAG_TABLES:
        try:
            print(f"  {t:<16}{conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>4} 行")
        except sqlite3.OperationalError:
            print(f"  {t:<16}  不存在")
    print(f"  fact_tag 列：{columns(conn, 'fact_tag')}")
    print(f"  fact_tag 主键：{pk(conn, 'fact_tag')}")

    if n_tag and not args.force:
        print(f"\nfact_tag 有 {n_tag} 条数据，v3 值域与 v2 不兼容。")
        print("确认要重来请加 --force（会先备份数据库）。")
        conn.close()
        return 1

    if args.dry_run:
        print("\n--dry-run：只检查，不做改动。")
        conn.close()
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    bak = BACKUP_DIR / f"radar.db.bak-v3-{datetime.now():%Y%m%d-%H%M%S}"
    conn.close()
    shutil.copy(db, bak)
    print(f"\n已备份 → {bak}")

    conn = sqlite3.connect(db)
    for t in TAG_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()

    print("\n=== 迁移后 ===")
    for t in TAG_TABLES:
        print(f"  {t:<16}{conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>4} 行")
    print(f"  fact_tag 列：{columns(conn, 'fact_tag')}")
    print(f"  fact_tag 主键：{pk(conn, 'fact_tag')}")
    print("\n下一步：python scripts/seed_tag_def.py")
    conn.close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
