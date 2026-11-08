#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / v5b：给历史 direct 标签回填「证据来源店面」
--------------------------------------------------------
**为什么不能用产品级 sha1 匹配**（第一版踩的）：
  曾用 `dim_app.descr` 的 sha1 去和四国 `descr_sha1` 比，命中哪个就把来源记成哪个。
  但 `dim_app.descr` 一直在被每日采集覆盖，**它现在的值不是打标时的值**。
  实测 Colorever：打标时是日文 1,084 字符，迁移时已被覆盖成英文 2,176 字符——
  按 sha1 匹配会记成 us（因为"当前 kr 那份"恰好等于 us 那份），而证据是日文。
  ⇒ 产品级匹配只能回答「当前等于哪国」，不能回答「打标时看的哪份」。

**这一版按证据匹配**：
  对每条 `evidence_strength='direct'` 的标签，把它引用的原文片段拿去四国描述里找：
    · 唯一命中某国  → 该国就是来源，写 descr_country / descr_sha1
    · 多国命中      → 文本相同，按 us > cn > jp > kr 取第一个（来源等价）
    · 四国都不命中  → 证据悬空，留 NULL，并列入清单等人处理
  仅处理 direct：inferred / none 没有子串校验，不需要来源。

幂等：重复跑结果相同。
用法：python scripts/migrate_v5b_backfill_tag_source.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from tag_apps import norm  # noqa: E402  口径必须与校验器一致

DB_PATH = ROOT / "data" / "radar.db"
DESCR_PREF = ("us", "cn", "jp", "kr")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    by_app: dict[str, dict[str, dict]] = {}
    for aid, cc, text, h in conn.execute(
            "SELECT app_id, country, descr, descr_sha1 FROM dim_app_country "
            "WHERE descr IS NOT NULL AND TRIM(descr) <> ''"):
        by_app.setdefault(aid, {})[cc] = {"text": text, "sha1": h}
    rank = {c: i for i, c in enumerate(DESCR_PREF)}

    rows = conn.execute("""
        SELECT app_id, tag_key, value, evidence FROM fact_tag
         WHERE evidence_strength='direct' AND evidence IS NOT NULL AND TRIM(evidence) <> ''
         ORDER BY app_id, tag_key""").fetchall()

    stat: Counter = Counter()
    unresolved: list[tuple[str, str, str, str]] = []
    for aid, k, v, ev in rows:
        variants = by_app.get(aid) or {}
        segs = [norm(s) for s in ev.split("|") if norm(s)]
        hits = [cc for cc, rec in variants.items()
                if segs and all(s in norm(rec["text"]) for s in segs)]
        if hits:
            cc = sorted(hits, key=lambda x: rank.get(x, 99))[0]
            conn.execute(
                "UPDATE fact_tag SET descr_country=?, descr_sha1=? WHERE app_id=? AND tag_key=?",
                (cc, variants[cc]["sha1"], aid, k))
            stat[f"hit_{cc}" if len(hits) == 1 else "hit_multi"] += 1
        else:
            conn.execute(
                "UPDATE fact_tag SET descr_country=NULL, descr_sha1=NULL WHERE app_id=? AND tag_key=?",
                (aid, k))
            name = conn.execute("SELECT name FROM dim_app WHERE app_id=?", (aid,)).fetchone()
            stat["unresolved"] += 1
            unresolved.append((aid, (name[0] if name else "")[:24], k, v[:26]))

    conn.commit()
    n_direct = len(rows)
    n_ok = n_direct - stat["unresolved"]
    print(f"direct 标签 {n_direct} 条；命中来源 {n_ok} 条（{100*n_ok/max(n_direct,1):.1f}%）；"
          f"四国文本都找不到 {stat['unresolved']} 条")
    print("按来源店面：")
    for k2 in [f"hit_{c}" for c in DESCR_PREF] + ["hit_multi"]:
        if stat.get(k2):
            print(f"  {k2:<12} {stat[k2]}")
    if unresolved:
        print(f"\n证据悬空（{len(unresolved)} 条，direct 但四国描述里都找不到）：")
        for aid, name, k, v in unresolved:
            print(f"  {aid} {name:<24} {k:<10} {v}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
