#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 2026-11-05 那次 95 条抽检的结论落到 fact_tag 上。

为什么需要单独一个脚本，而不是让 import-review 顺手改
------------------------------------------------------
`import-review` 只写 `fact_tag_review`（抽检记录），**不动 fact_tag**——
这是对的：抽检是证据，改标签是决策，两件事分开才能追责。
但抽检之后必须有人把决策执行掉，否则库里还是机器的旧值，报告会继续用错标签。
这个脚本就是那一步，并且把每一步都打印出来，可重跑（幂等）。

它做三件事
----------
1. 4 条判「错」的：把 fact_tag 改成人工值，method='human'，need_review=0。
2. 2 条判「存疑」的：**不改值**，只把 need_review 置 1——存疑不是「知道正确答案」，
   不能编一个值上去。
3. 一条规则修正：螺丝族里明示「入盒」的 2 个产品，按新判据从 brain 改 sort。
   （这两条同时也是上面第 1 条的对象，这里显式写出规则依据，方便以后复查。）

不做的事
--------
- 不动 `fact_tag_review`（那是抽检记录，已由 import-review 写入）。
- 不重打整批标签。规则变了要重打时会走 `seed_tag_def.py` + 重新 export/validate/import。
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "radar.db"
CSV = ROOT / "tag_work" / "review_filled_20261105.csv"

# 规则修正：螺丝族里「明示入盒」的两个产品（判据见 dim_tag_def.subgenre v3）
RULE_FIXES = {
    "6740043080": ("puzzle.sort",
                   "规则修正：描述明示「같은 색의 나사를 찾아 올바른 상자로 이동」（同色螺丝放入对应盒子）"
                   "——容器即答案，按 v3 判据记 sort；顺序规划是次级约束"),
    "6749300334": ("puzzle.sort",
                   "规则修正：描述写「분류하고」（分类）+ play_desc「放进对应盒子」——同色入盒是主循环，"
                   "按 v3 判据记 sort"),
}
# 判「存疑」但不改值，只提复核优先级
KEEP_UNCERTAIN = {"6737022506"}   # 나사 풀기 대작전：只写「按颜色点击移除、清空木板」，没有容器归位


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    now = datetime.now().isoformat(timespec="seconds")

    if not CSV.exists():
        print(f"找不到抽检结果：{CSV}")
        return 1
    with CSV.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    changes = []

    # 1. 判错的 → 采纳人工值
    for r in rows:
        v = (r.get("verdict") or "").strip().lower()
        if v != "wrong":
            continue
        aid, key, human = r["app_id"], r["tag_key"], (r.get("human_value") or "").strip()
        cur = conn.execute("SELECT value FROM fact_tag WHERE app_id=? AND tag_key=?",
                           (aid, key)).fetchone()
        if cur and cur[0] == human:
            continue
        note = (r.get("note") or "").strip()
        conn.execute("""UPDATE fact_tag
                           SET value=?, method='human', need_review=0,
                               evidence=?, tagged_at=?
                         WHERE app_id=? AND tag_key=?""",
                     (human, f"人工抽检修正（原值 {cur[0] if cur else '?'}）：{note}", now, aid, key))
        changes.append(("人工修正", r["name"], key, cur[0] if cur else "?", human))

    # 2. 判存疑的 → 只提优先级，不改值
    for r in rows:
        v = (r.get("verdict") or "").strip().lower()
        if v != "unsure":
            continue
        aid, key = r["app_id"], r["tag_key"]
        conn.execute("UPDATE fact_tag SET need_review=1 WHERE app_id=? AND tag_key=?", (aid, key))
        cur = conn.execute("SELECT value FROM fact_tag WHERE app_id=? AND tag_key=?",
                           (aid, key)).fetchone()
        changes.append(("标记存疑", r["name"], key, cur[0] if cur else "?", "（值不变）"))

    # 3. 规则修正：螺丝族入盒 → sort
    for aid, (val, ev) in RULE_FIXES.items():
        cur = conn.execute("SELECT value FROM fact_tag WHERE app_id=? AND tag_key='subgenre'",
                           (aid,)).fetchone()
        if not cur or cur[0] == val:
            continue
        conn.execute("""UPDATE fact_tag
                           SET value=?, method='rule', need_review=0, evidence=?, tagged_at=?
                         WHERE app_id=? AND tag_key='subgenre'""", (val, ev, now, aid))
        changes.append(("规则修正", aid, "subgenre", cur[0], val))

    for aid in KEEP_UNCERTAIN:
        conn.execute("UPDATE fact_tag SET need_review=1 WHERE app_id=? AND tag_key='subgenre'",
                     (aid,))

    conn.commit()

    print(f"{'类型':<10}{'产品':<30}{'标签':<10}{'原值':<18}新值")
    print("-" * 96)
    for t, name, key, old, new in changes:
        print(f"{t:<10}{name[:28]:<30}{key:<10}{old:<18}{new}")
    print(f"\n共 {len(changes)} 处改动")

    print("\n=== 改后的螺丝族 ===")
    for r in conn.execute("""SELECT a.name, f.value, f.method, f.need_review
                               FROM fact_tag f JOIN dim_app a USING(app_id)
                              WHERE f.tag_key='subgenre' AND f.app_id IN (?,?,?)
                              ORDER BY f.value, a.name""",
                          ("6740043080", "6749300334", "6737022506")):
        print(f"  {r['name'][:30]:<32}{r['value']:<16}{r['method']:<8}rev={r['need_review']}")

    print("\n=== 改后的 fact_tag 总览 ===")
    for r in conn.execute("""SELECT method, COUNT(*) n FROM fact_tag GROUP BY 1 ORDER BY 2 DESC"""):
        print(f"  {r['method'] or '?':<10}{r['n']}")
    print("  need_review=1:", conn.execute(
        "SELECT COUNT(*) FROM fact_tag WHERE need_review=1").fetchone()[0])
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
