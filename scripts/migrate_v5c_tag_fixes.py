#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / v5c：修 6 条校验不过的标签
--------------------------------------
来源：v5 把 descr 下移后重跑 validate，库内 330 条只剩 6 条不过。

A. Block Blast 的 2 条 direct 是**改写**，不是原文子串
   原文 kr：「8×8 보드에 색색의 블록을 맞추고」「가로줄이나 세로줄을 완성하고」
   原 evidence 把两句拼成「8x8 그리드에 블록을 올리고 가로줄이나 세」，标了 direct。
   → 保留判断，把 evidence 换成两条真实子串。

B. 4 条 art 用了「依系列推断」，违反 v3 第⑩条
   补了 screenshots 之后逐张看过：
     · 金铲铲之战：第 2 张中央棋盘为实机，Q 版大头 + 3D 材质光影 → 3d_stylized（但图含宣传元素，标 need_review）
     · ツムツム：第 2 张为纯实机，玩偶有体积与高光、无描边 → 3d_stylized（可确认）
     · 王者荣耀：第 2 张为宣传图 + 小窗实机，角色七头身、材质风格化 → 3d_stylized（图不纯，标 need_review）
     · 天天爱消除：4 张全是运营活动界面（市集/免单/转盘），看不到核心棋盘 → 按规则必须 unclear + none + need_review
   evidence 一律写「第几张、看到什么」，不用「依系列」。

幂等：重复跑结果相同。
用法：python scripts/migrate_v5c_tag_fixes.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "radar.db"

# (app_id, tag_key, value, strength, evidence, need_review, 来源店面, note)
# 来源店面非空时，脚本会去 dim_app_country 取当前 sha1 一并写入（不硬编码指纹）。
FIXES = [
    ("1617391485", "play_desc", "把三块一组的方块摆进 8x8 格子，凑满整行整列就炸掉",
     "direct", "8×8 보드에 색색의 블록을 맞추고 | 가로줄이나 세로줄을 완성하고", 0, "kr",
     "原 evidence 是两句拼接改写，改为两条真实子串"),
    ("1617391485", "subgenre", "puzzle.block",
     "direct", "8×8 보드에 색색의 블록을 맞추고 | 가로줄이나 세로줄을 완성하고", 0, "kr",
     "同上"),
    ("1478101301", "art", "3d_stylized",
     "inferred",
     "截图（cn 第2张，中央为实机棋盘）：棋子为 Q 版大头比例、有立体材质与投影，"
     "非手绘描边 → 3D 卡通渲染", 1, None,
     "原为「依系列推断」，改为截图依据；图含宣传元素，留待复核"),
    ("724594093", "art", "3d_stylized",
     "inferred",
     "截图（jp 第2张，纯实机）：玩偶有体积、高光与投影，无描边 → 3D 卡通渲染", 0, None,
     "原为「系列既有形象」，改为截图依据"),
    ("989673964", "art", "3d_stylized",
     "inferred",
     "截图（cn 第2张，宣传图+小窗实机）：角色七头身、材质风格化非 PBR → 3D 风格化", 1, None,
     "原为「依 IP 既有形象」，改为截图依据；小窗过小，留待复核"),
    ("654897098", "art", "unclear",
     "none",
     "4 张商店截图均为运营活动界面（萌趣市集 / 闯关免单 / 星光大转盘），"
     "未出现核心三消棋盘，无法判断主视觉渲染形态", 1, None,
     "原为「依萌系 IP 与系列既有形象」判 2D 卡通，违反第⑩条；截图不足，改 unclear"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    n = 0
    for aid, k, v, st, ev, nr, cc, note in FIXES:
        old = conn.execute(
            "SELECT value, evidence_strength, substr(evidence,1,40) FROM fact_tag "
            "WHERE app_id=? AND tag_key=?", (aid, k)).fetchone()
        if not old:
            print(f"  跳过（无此标签）{aid} {k}")
            continue
        sha = None
        if cc:
            row = conn.execute(
                "SELECT descr_sha1 FROM dim_app_country WHERE app_id=? AND country=?",
                (aid, cc)).fetchone()
            sha = row[0] if row else None
        conn.execute("""
            UPDATE fact_tag
               SET value=?, evidence_strength=?, evidence=?, need_review=?,
                   method='human', descr_country=?, descr_sha1=?,
                   tagged_at=datetime('now','localtime')
             WHERE app_id=? AND tag_key=?""",
                     (v, st, ev, nr, cc, sha, aid, k))
        n += 1
        print(f"  {aid} {k:<9} {old[0]} → {v}  [{old[1]}→{st}]  {note}")
    conn.commit()
    print(f"已修 {n} 条")

    # 修完立刻自检：这几条必须能通过校验（用与 validate 相同的口径）
    sys.path.insert(0, str(ROOT / "scripts"))
    from tag_apps import descr_index, tag_source_index, validate, load_defs, norm  # noqa: E402
    defs = load_defs(conn)
    rows = conn.execute("""
        SELECT app_id, tag_key, value, evidence_strength, evidence, need_review,
               descr_country, descr_sha1, method FROM fact_tag""").fetchall()
    items: dict[str, dict] = {}
    for a, kk, vv, ss, ee, nn, cc, sh, mm in rows:
        items.setdefault(a, {"app_id": a, "tags": []})["tags"].append(
            {"tag_key": kk, "value": vv, "evidence_strength": ss, "evidence": ee,
             "need_review": nn, "descr_country": cc, "descr_sha1": sh, "method": mm})
    errs = validate({"items": list(items.values())}, defs,
                    descr_index(args.db), tag_source_index(args.db))
    print(f"\n自检：{len(rows)} 条标签 → 错误 {len(errs)}")
    for e in errs:
        print("  -", e)
    conn.close()
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
