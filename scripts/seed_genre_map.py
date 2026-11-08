#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 品类字典种子
-------------------------
把四国本地化的品类名映射到统一英文规范名。

为什么必须做：`iTunes lookup?country=cc` 返回的 `genres` 是**该店面语言**——
同一个 Puzzle，在 us 是 "Puzzle"、cn 是 "益智解谜"、jp 是 "パズル"、kr 是 "퍼즐"。
不归一会让「品类结构」分析直接失效（首轮实测出现 73 个互不相同的字符串）。

注意：`Casual` / `Entertainment` 这类不是真正的子品类，标 is_top=1，
在 v_rank_sub 里不会被选作 sub_genre。

用法：
    python scripts/seed_genre_map.py            # 写入字典并报告覆盖率
    python scripts/seed_genre_map.py --dry-run  # 只看未收录字符串
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "radar.db"
SCHEMA_PATH = ROOT / "schema.sql"

# (规范名, 是否顶级品类, [各语言原文...])  —— Apple App Store 游戏子品类全集
GENRES: list[tuple[str, int, list[str]]] = [
    ("Games",        1, ["Games", "游戏", "ゲーム", "게임"]),
    ("Entertainment",1, ["Entertainment", "娱乐", "エンターテインメント", "엔터테인먼트"]),
    ("Casual",       1, ["Casual", "休闲", "カジュアル", "캐주얼"]),
    ("Action",       0, ["Action", "动作", "アクション", "액션"]),
    ("Adventure",    0, ["Adventure", "冒险", "アドベンチャー", "어드벤처"]),
    ("Arcade",       0, ["Arcade", "街机", "アーケード", "아케이드"]),
    ("Board",        0, ["Board", "桌面", "ボード", "보드"]),
    ("Card",         0, ["Card", "卡牌", "カード", "카드"]),
    ("Casino",       0, ["Casino", "娱乐场", "カジノ", "카지노"]),
    ("Dice",         0, ["Dice", "骰子", "ダイス", "다이스"]),
    ("Educational",  0, ["Educational", "教育", "教育", "교육"]),
    ("Family",       0, ["Family", "家庭聚会", "ファミリー", "가족"]),
    ("Music",        0, ["Music", "音乐", "ミュージック", "음악"]),
    ("Puzzle",       0, ["Puzzle", "益智解谜", "パズル", "퍼즐"]),
    ("Racing",       0, ["Racing", "竞速", "レーシング", "레이싱"]),
    ("Role Playing", 0, ["Role Playing", "角色扮演", "ロールプレイング", "롤플레잉"]),
    ("Simulation",   0, ["Simulation", "模拟", "シミュレーション", "시뮬레이션"]),
    ("Sports",       0, ["Sports", "体育", "スポーツ", "스포츠"]),
    ("Strategy",     0, ["Strategy", "策略", "ストラテジー", "전략"]),
    ("Trivia",       0, ["Trivia", "问答", "トリビア", "퀴즈"]),
    ("Word",         0, ["Word", "文字", "ワード", "単語", "워드"]),
    # 非游戏品类：Apple 允许一个 app 跨品类挂多个类目，游戏也会带上这些，
    # 一律标 is_top=1，避免被 v_rank_sub 选成 sub_genre。
    ("Social Networking", 1, ["Social Networking", "社交", "ソーシャルネットワーキング", "소셜 네트워킹"]),
    ("Shopping",          1, ["Shopping", "购物", "ショッピング", "쇼핑"]),
    ("Education",         1, ["Education", "教育", "教育", "교육"]),
    ("Utilities",         1, ["Utilities", "工具", "ユーティリティ", "유틸리티"]),
    ("Food & Drink",      1, ["Food & Drink", "美食佳饮", "フード／ドリンク", "음식 및 음료"]),
    ("Lifestyle",         1, ["Lifestyle", "生活", "ライフスタイル", "라이프스타일"]),
    ("Health & Fitness",  1, ["Health & Fitness", "健康健美", "ヘルスケア／フィットネス", "건강 및 피트니스"]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"库不存在：{DB_PATH}（先跑 fetch_daily.py）")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    rows = [(raw, canon, top) for canon, top, raws in GENRES for raw in raws]
    if not args.dry_run:
        conn.executemany(
            "INSERT INTO dim_genre_map(raw, canonical, is_top) VALUES (?,?,?) "
            "ON CONFLICT(raw) DO UPDATE SET canonical=excluded.canonical, is_top=excluded.is_top",
            rows,
        )
        conn.commit()
    print(f"字典种子 {len(rows)} 条（{len(GENRES)} 个规范名 × 多语言）")

    # 覆盖率检查：库里出现过的原始字符串有多少能被映射
    seen: dict[str, int] = {}
    for (gj,) in conn.execute("SELECT genres_all FROM dim_app"):
        for g in json.loads(gj or "[]"):
            seen[g] = seen.get(g, 0) + 1

    mapped = {r[0] for r in rows}
    unmapped = {k: v for k, v in seen.items() if k not in mapped}
    total = sum(seen.values())
    hit = total - sum(unmapped.values())
    print(f"覆盖：{hit}/{total} 次出现（{hit / total * 100:.1f}%），"
          f"未收录字符串 {len(unmapped)} 个")

    if unmapped:
        print("未收录（按出现次数降序，需补进 GENRES）：")
        for k, v in sorted(unmapped.items(), key=lambda kv: -kv[1]):
            print(f"  {v:>4}  {k}")

    if not args.dry_run:
        n = conn.execute("SELECT COUNT(*) FROM dim_genre_map").fetchone()[0]
        sub = conn.execute("SELECT COUNT(*) FROM v_rank_sub WHERE sub_genre IS NOT NULL").fetchone()[0]
        allrows = conn.execute("SELECT COUNT(*) FROM v_rank_sub").fetchone()[0]
        print(f"库内字典 {n} 条；v_rank_sub 中已识别子品类 {sub}/{allrows}")
    conn.close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
