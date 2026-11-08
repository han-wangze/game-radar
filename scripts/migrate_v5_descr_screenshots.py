#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / v5 迁移：把 descr 与 screenshots 下移到「产品 × 店面」
------------------------------------------------------------------
为什么改（2026-11-05 抽检的副产物）：
  `dim_app.descr` 一个产品只有一行，抓取顺序 us→cn→jp→kr ⇒ 存的是最后写入国（kr）那份。
  实测同一款产品四国描述语言不同（Pokémon GO：us 英文 1,755 字符 / kr 韩文 1,773）；
  app 更新商店页还会改文本。后果两条：
    ① 打标输入语言不可控（同一批里混着韩文/日文/英文）；
    ② `direct` 证据是对着「当时那份文本」写的，文本一变就悬空（11-03 那批约 10%）。

这次改什么：
  1. `dim_app_country` 加 `descr` / `screenshots` / `descr_sha1`（该店面口径）
  2. `fact_tag` 加 `descr_country` / `descr_sha1`（证据对着哪国、哪份文本）
  3. 按国家重新 lookup 回填一遍（当前值，不是历史值——历史文本 Apple 不提供）

幂等：列已存在则跳过；回填总是覆盖为最新一次抓到的值。
用法：
    python scripts/migrate_v5_descr_screenshots.py             # 加列 + 回填
    python scripts/migrate_v5_descr_screenshots.py --no-backfill   # 只加列
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "radar.db"

LOOKUP_URL = "https://itunes.apple.com/lookup?id={ids}&country={cc}&entity=software"
UA = "Mozilla/5.0 (compatible; game-radar/0.1; +research)"
BATCH = 50


def log(msg: str) -> None:
    print(msg, flush=True)


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def http_json(url: str, retries: int = 3, timeout: int = 30) -> dict:
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求失败 {url}: {last}")


def ensure_column(conn: sqlite3.Connection, table: str, col: str, decl: str) -> bool:
    """SQLite 不支持 ADD COLUMN IF NOT EXISTS，用 PRAGMA 先查。返回是否真的加了。"""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    return True


def detect_lang(text: str) -> str:
    """粗略判语言，只为回填报告用（不做任何业务判断）。"""
    if not text:
        return "empty"
    n = len(text)
    hangul = sum(1 for c in text if "\uac00" <= c <= "\ud7af")
    kana = sum(1 for c in text if "\u3040" <= c <= "\u30ff")
    han = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    if hangul / n > 0.10:
        return "ko"
    if kana / n > 0.05:
        return "ja"
    if han / n > 0.10:
        return "zh"
    return "latin"


def backfill(conn: sqlite3.Connection) -> dict:
    """按国家分组，批量 lookup，写回 descr / screenshots / descr_sha1。"""
    groups: dict[str, list[str]] = {}
    for cc, aid in conn.execute(
            "SELECT country, app_id FROM dim_app_country ORDER BY country, app_id"):
        groups.setdefault(cc, []).append(aid)

    stats = {"ok": 0, "missing": 0, "empty_descr": 0, "no_shot": 0,
             "lang": {}, "by_country": {}}
    for cc, ids in sorted(groups.items()):
        got = 0
        for start in range(0, len(ids), BATCH):
            chunk = ids[start:start + BATCH]
            payload = http_json(LOOKUP_URL.format(ids=",".join(chunk), cc=cc))
            by_id = {str(r.get("trackId")): r for r in payload.get("results", [])}
            for aid in chunk:
                r = by_id.get(aid)
                if not r:
                    stats["missing"] += 1
                    continue
                descr = (r.get("description") or "").strip()
                shots = r.get("screenshotUrls") or []
                conn.execute(
                    "UPDATE dim_app_country SET descr=?, screenshots=?, descr_sha1=? "
                    "WHERE app_id=? AND country=?",
                    (descr or None,
                     json.dumps(shots, ensure_ascii=False) if shots else None,
                     sha1(descr) if descr else None,
                     aid, cc))
                got += 1
                stats["ok"] += 1
                if not descr:
                    stats["empty_descr"] += 1
                else:
                    lang = detect_lang(descr)
                    stats["lang"][lang] = stats["lang"].get(lang, 0) + 1
                if not shots:
                    stats["no_shot"] += 1
            if start + BATCH < len(ids):
                time.sleep(0.8)
        stats["by_country"][cc] = got
        log(f"  {cc}: 回填 {got}/{len(ids)}")
    conn.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--no-backfill", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    log("== 1. 加列（幂等）==")
    added = []
    for table, col, decl in [
        ("dim_app_country", "descr", "TEXT"),
        ("dim_app_country", "screenshots", "TEXT"),
        ("dim_app_country", "descr_sha1", "TEXT"),
        ("fact_tag", "descr_country", "TEXT"),
        ("fact_tag", "descr_sha1", "TEXT"),
    ]:
        if ensure_column(conn, table, col, decl):
            added.append(f"{table}.{col}")
    conn.commit()
    log(f"  新增列 {len(added)} 个：" + ("、".join(added) if added else "无（已是最新）"))

    if args.no_backfill:
        log("跳过回填（--no-backfill）")
        return 0

    log("== 2. 按店面回填 descr / screenshots ==")
    stats = backfill(conn)
    n_total = conn.execute("SELECT COUNT(*) FROM dim_app_country").fetchone()[0]
    n_descr = conn.execute("SELECT COUNT(*) FROM dim_app_country WHERE descr IS NOT NULL").fetchone()[0]
    n_shot = conn.execute("SELECT COUNT(*) FROM dim_app_country WHERE screenshots IS NOT NULL").fetchone()[0]
    log(f"  合计 {n_total} 个（产品×店面）；描述 {n_descr}；截图 {n_shot}")
    log(f"  失败 {stats['missing']}；空描述 {stats['empty_descr']}；无截图 {stats['no_shot']}")
    log(f"  描述语言分布：{stats['lang']}")

    log("== 3. 修复证据：同一产品四国描述确实不同（抽 3 个有 2 国以上记录的）==")
    rows = conn.execute("""
        SELECT app_id, name, COUNT(DISTINCT country) c FROM dim_app_country
         WHERE descr IS NOT NULL GROUP BY app_id HAVING c >= 2 LIMIT 3
    """).fetchall()
    for aid, name, _ in rows:
        vals = conn.execute(
            "SELECT country, LENGTH(descr), descr_sha1 FROM dim_app_country "
            "WHERE app_id=? AND descr IS NOT NULL ORDER BY country", (aid,)).fetchall()
        log(f"  {(name or '')[:28]:<28} " + " | ".join(f"{cc}:{ln}字/{h}" for cc, ln, h in vals))

    log("== 4. dim_app.descr 状态（应冻结，不再随抓取变化）==")
    log("  该列保留但不参与打标与校验；打标一律读 dim_app_country.descr")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
