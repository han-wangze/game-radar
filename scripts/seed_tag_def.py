#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 标签字典种子
-------------------------
四个标签键（2026-11-06 起，方案甲收口 + 译名 + 商店介绍译文）：

  play_desc   玩法一句话   开集，必填，≤40 字   ← 给人读、可核对、直接上页面
  like_of     玩法类型     开集，可选，≤12 字   ← 两三个中文词概括玩法（三消+家装 / 水排序 / 放置卡牌）
  name_cn     中文译名     开集，可选，≤60 字   ← 原名是韩/日/英文时，括号里给中文读者一个抓手
  descr_cn    介绍译文     开集，可选，≤600 字  ← 只对日韩产品生成，展开层里替代看不懂的原文

设计顺序：先写人话（play_desc），再给玩法类型（like_of），译名只为「认得出」，
译文只为「点开后读得懂」。
name_cn / descr_cn 之所以进标签表而不是另建表：它们与 play_desc 由同类模型调用产出、
同一个 (app_id, tag_key) 幂等机制管理，单独建表只会多一份同步负担。

descr_cn 的边界（重要）：它是**译文**，不是第二份玩法摘要。原文是营销文案，
译文也照营销文案译，不替厂商改写成中性描述——否则读者没法核对 play_desc
与原文是否对得上。英语与中文原文不生成（读者读得懂 / 本来就是中文）。

历史上曾定义 subgenre / theme / art 三个闭集字段（赛道层 / 题材 / 美术风格），
由 gen_tags 盲测体系驱动。经验证：这三个字段在页面与周报里均未被读取（品类展示
统一用 Apple 的 sub_genre），且缺乏真实消费者，故 2026-11-06 起停用、不再自动生成，
相关盲测脚本（gen_tags.py）与手册生成器（build_tag_manual.py）一并移除。

用法：python scripts/seed_tag_def.py [--db path]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "radar.db"
SCHEMA_PATH = ROOT / "schema.sql"

PLAY_DESC_MAX = 40
LIKE_OF_MAX = 12
NAME_CN_MAX = 60
DESCR_CN_MAX = 600

DEFS = [
    {
        "tag_key": "play_desc",
        "name_cn": "玩法一句话",
        "definition": (
            "用一句人话说清「玩家在做什么、为什么继续做」，≤40 字。必填。"
            "要求：写动作与循环，不写营销词（「史诗」「沉浸」不算）；"
            "复合玩法就写进这一句（如「合并同名物品，同时按颜色分拣入库」），不要拆成两行。"
            "这条不参与枚举校验，它本身就是给人核对与展示用的。"
        ),
        "value_domain": None,
        "max_len": PLAY_DESC_MAX,
        "source": "—",
    },
    {
        "tag_key": "like_of",
        "name_cn": "玩法类型",
        "definition": (
            "两三个中文词概括这款游戏属于哪类玩法，≤12 字，可空。"
            "例如「三消+家装」「水排序」「放置卡牌」「合成经营」「塔防」「模拟经营」。"
            "用途：让读者一眼知道玩法大类，不必先认得某个标杆产品。"
            "边界：写玩法类型，不写具体游戏名、厂商名或英文品类词；"
            "描述里看不出玩法时留空，不硬凑。"
        ),
        "value_domain": None,
        "max_len": LIKE_OF_MAX,
        "source": "—",
    },
    {
        "tag_key": "name_cn",
        "name_cn": "中文译名",
        "definition": (
            "产品名的中文译名，≤60 字，可空。只解决「中文读者能不能认出这是哪款」，"
            "不是官方译名的考据：有通行译名用通行译名，没有就直译核心词，可省略副标题与商标符号。"
            "原名已是中文（含汉字且不含日文假名、韩文谚文）时留空，不生成。"
            "边界：不做意译营销化（不把「Bloons」译成「气球塔防大师」这类加戏写法）。"
        ),
        "value_domain": None,
        "max_len": NAME_CN_MAX,
        "source": "—",
    },
    {
        "tag_key": "descr_cn",
        "name_cn": "商店介绍译文",
        "definition": (
            "把日文/韩文商店描述的前一段译成中文，≤600 字，可空。"
            "只解决「点开以后读得懂」，不给原文加戏：保留原文的语气与顺序，"
            "不把营销词改写成中性描述，也不额外补充原文没有的玩法推断。"
            "原文是英语或中文时留空，不生成（读者读得懂 / 本来就是中文）。"
            "边界：只译前 600 字（多数描述的玩法段在这之内），"
            "末尾的更新日志、版权声明、致谢名单不译。"
        ),
        "value_domain": None,
        "max_len": DESCR_CN_MAX,
        "source": "—",
    },
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()
    db = Path(args.db)
    if not db.exists():
        print(f"库不存在：{db}")
        return 1
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))   # 确保表存在

    now = datetime.now().isoformat(timespec="seconds")
    for d in DEFS:
        conn.execute("""
            INSERT INTO dim_tag_def(tag_key, name_cn, definition, value_domain,
                                    max_len, source, multi_value, ver, changed_at, note)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(tag_key) DO UPDATE SET
                name_cn      = excluded.name_cn,
                definition   = excluded.definition,
                value_domain = excluded.value_domain,
                max_len      = excluded.max_len,
                source       = excluded.source,
                multi_value  = excluded.multi_value,
                changed_at   = excluded.changed_at,
                note         = excluded.note,
                -- 定义或值域变了就自动升版本，避免旧标签被静默沿用
                ver = CASE WHEN dim_tag_def.definition   IS NOT excluded.definition
                             OR dim_tag_def.value_domain IS NOT excluded.value_domain
                           THEN dim_tag_def.ver + 1 ELSE dim_tag_def.ver END
        """, (d["tag_key"], d["name_cn"], d["definition"],
              json.dumps(d["value_domain"], ensure_ascii=False) if d["value_domain"] else None,
              d["max_len"], d["source"], 0, 1, now, None))

    # 清理已停用的僵尸键（如历史上曾写入的 subgenre / theme / art）
    live = [d["tag_key"] for d in DEFS]
    conn.execute(
        f"DELETE FROM dim_tag_def WHERE tag_key NOT IN ({','.join('?' * len(live))})",
        live)

    conn.commit()
    for k, name, ver, n, ml, src in conn.execute(
            "SELECT tag_key, name_cn, ver, (SELECT COUNT(*) FROM fact_tag f "
            "WHERE f.tag_key=dim_tag_def.tag_key), max_len, source FROM dim_tag_def "
            "ORDER BY tag_key"):
        print(f"  {k:<12} v{ver}  {name}  标签{n}条  max_len={ml}  source={src}")
    print("dim_tag_def 已重建（已清理停用键）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
