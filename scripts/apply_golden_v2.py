#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""金标集 v2：把 2 条「存疑」按 seed_tag_def v4 新规则裁决后落到库里。

背景
----
2026-11-05 那次 95 条抽检有 2 条 verdict='unsure'，被 gen_tags.py 排除出盲测计分
（CLOSED_KEYS = subgenre, theme；unsure 对不参与比对）。
这两款的「存疑」发生在 subgenre 判据⑧（sort/brain 边界）之前，本质是「人类无规则可据」。
S0 把规则⑧写进 seed_tag_def v4 后，这两款已可裁决：

1. 6737022506 나사 풀기 대작전 / subgenre
   描述仅「按颜色点击移除螺丝、清空木板」，无入盒归位证据 → 规则⑧：不足以判 sort，
   记 puzzle.brain 为规则默认。翻 verdict=unsure→correct，保留 need_review=1（截图只确认有无盒）。
   它是闭集键，翻正后 S2 盲测才会把它计进 subgenre 一致率。

2. 6802383216 Parking Loop / play_desc
   开集键、从不计分；原值臆断「车」，描述全抽象，改为机制中性写法
   「规划移动顺序，把物体归位、避免卡死通关」。翻 verdict=unsure→correct，保留 need_review=1。

与 migrate_tag_v4.py 的关系
-------------------------
migrate_tag_v4.py 只动 fact_tag（决策层），不动 fact_tag_review（抽检记录层）。
本脚本补那一步：把已裁决的 verdict 写回 fact_tag_review，让盲测不再排除这两对；
并对 Parking Loop 的 fact_tag.play_desc 做机制中性改写（同款页面展示也受益）。
幂等，可重跑。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "radar.db"

# (app_id, tag_key) -> (fact_tag_review.verdict, fact_tag_review.note)
REVIEW_FIXES = {
    ("6737022506", "subgenre"): (
        "correct",
        "按规则⑧裁决：描述仅「按颜色点击移除螺丝、清空木板」，无入盒归位证据，不足以判 sort，"
        "记 puzzle.brain 为规则默认；need_review=1 待截图确认是否有入盒分拣。"
        "原 verdict=unsure（规则⑧未立时人类无法决），现依规则转 correct。",
    ),
    ("6802383216", "play_desc"): (
        "correct",
        "play_desc 为开集键、不计分；原值臆断「车」，描述全抽象，改为机制中性写法；"
        "need_review=1 待截图确认对象是车还是物品分拣。原 verdict=unsure 现依规则转 correct。",
    ),
}

# Parking Loop play_desc 机制中性改写（fact_tag 层）
PLAY_DESC_FIX = {
    "6802383216": "规划移动顺序，把物体归位、避免卡死通关",
}


def main() -> int:
    if not DB.exists():
        print(f"找不到库：{DB}")
        return 1
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    now = datetime.now().isoformat(timespec="seconds")
    changes = []

    # 1) fact_tag_review: 翻 verdict
    for (aid, key), (verdict, note) in REVIEW_FIXES.items():
        cur = conn.execute(
            "SELECT verdict, note FROM fact_tag_review WHERE app_id=? AND tag_key=?",
            (aid, key)).fetchone()
        if not cur:
            print(f"  [跳过] fact_tag_review 无此行 {aid}/{key}")
            continue
        if cur["verdict"] == verdict and (cur["note"] or "") == note:
            continue
        conn.execute(
            "UPDATE fact_tag_review SET verdict=?, note=? WHERE app_id=? AND tag_key=?",
            (verdict, note, aid, key))
        changes.append(("review裁决", aid, key, cur["verdict"], verdict))

    # 2) fact_tag: Parking Loop play_desc 中性改写
    for aid, val in PLAY_DESC_FIX.items():
        cur = conn.execute(
            "SELECT value FROM fact_tag WHERE app_id=? AND tag_key='play_desc'",
            (aid,)).fetchone()
        if not cur:
            print(f"  [跳过] fact_tag 无此行 {aid}/play_desc")
            continue
        if cur["value"] == val:
            continue
        conn.execute(
            "UPDATE fact_tag SET value=?, method='rule', need_review=1, evidence=?, tagged_at=? "
            "WHERE app_id=? AND tag_key='play_desc'",
            (val, "金标集 v2 裁决：开集键机制中性改写（原值臆断对象为车）", now, aid))
        changes.append(("play_desc改写", aid, "play_desc", cur["value"], val))

    conn.commit()

    print(f"{'类型':<14}{'app_id':<14}{'键':<10}{'原值':<22}新值")
    print("-" * 90)
    for t, aid, key, old, new in changes:
        print(f"{t:<14}{aid:<14}{key:<10}{str(old)[:20]:<22}{new}")
    print(f"\n共 {len(changes)} 处改动")

    print("\n=== 校验：剩余 unsure 对 ===")
    n = conn.execute("SELECT COUNT(*) FROM fact_tag_review WHERE verdict='unsure'").fetchone()[0]
    print(f"  fact_tag_review.verdict='unsure' 剩余 {n} 对（应为 0）")
    print("=== 两款 fact_tag 现状 ===")
    for aid in ('6737022506', '6802383216'):
        for r in conn.execute(
                "SELECT tag_key,value,method,need_review FROM fact_tag WHERE app_id=?", (aid,)):
            if r['tag_key'] in ('subgenre', 'play_desc'):
                print(f"  {aid} {r['tag_key']:<10} val={r['value']!r} method={r['method']} rev={r['need_review']}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
