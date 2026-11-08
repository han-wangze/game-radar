#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 从《游戏类型与玩法.docx》提取结构化玩法知识
--------------------------------------------------------
源文件是一张 7 列表格（游戏大类 / 核心玩法标签 / 简要介绍 / 经典代表 /
核心特征 / 玩法识别关键点 / 核心商业化模式）。按 w:tbl → w:tr → w:tc 还原成
Markdown，供 scripts/build_tag_manual.py 编进「材料层」。

只保留与识别有关的 4 列（大类 / 玩法标签 / 简要介绍 / 玩法识别关键点），
代表产品与商业化模式对判别闭集没有帮助，且会占 prompt 预算。

用法：python scripts/extract_docx.py
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "refs" / "游戏类型与玩法.docx"
OUT = ROOT / "refs" / "gameplay_extracted.md"
NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

KEEP = [0, 1, 2, 5]  # 大类 / 玩法标签 / 简要介绍 / 玩法识别关键点
HEADERS = ["游戏大类", "核心玩法标签", "简要介绍", "玩法识别关键点"]


def cell_text(tc) -> str:
    parts = []
    for p in tc.iter(NS + "p"):
        t = "".join(n.text or "" for n in p.iter(NS + "t")).strip()
        if t:
            parts.append(t)
    return " ".join(parts)


def main() -> int:
    if not SRC.exists():
        print(f"缺少 {SRC}")
        return 1
    with zipfile.ZipFile(SRC) as z:
        root = ET.fromstring(z.read("word/document.xml"))

    rows: list[list[str]] = []
    for tbl in root.iter(NS + "tbl"):
        for tr in tbl.iter(NS + "tr"):
            cells = [cell_text(tc) for tc in tr.iter(NS + "tc")]
            if cells:
                rows.append(cells)

    by_cat: dict[str, list[list[str]]] = {}
    last_cat = ""
    for r in rows:
        padded = (r + [""] * 7)[:7]
        cat_cell, label = padded[0].strip(), padded[1].strip()
        if cat_cell == "游戏大类" or label == "核心玩法标签":
            continue
        # 合并单元格只在分类首行写大类；只有大类没有玩法标签的行是分类标题行
        if cat_cell and not label:
            last_cat = cat_cell
            continue
        if cat_cell:
            last_cat = cat_cell
        if not label:
            continue
        vals = [padded[i].strip() for i in KEEP]
        vals[0] = last_cat or "（未标大类）"
        cat = vals[0]
        by_cat.setdefault(cat, []).append(vals)

    lines = [
        "# 《游戏类型与玩法》结构化摘录（自动生成，勿手改）",
        "",
        f"源文件：`refs/游戏类型与玩法.docx`　|　{len(rows)} 行表格　|　"
        f"保留 {sum(len(v) for v in by_cat.values())} 条玩法、{len(by_cat)} 个大类",
        "",
        "只保留与玩法识别有关的四列；「经典代表」「核心特征」「商业化模式」未收录。",
        "本文件是**材料层**素材，不是值域依据；值域与判定纪律一律以 `dim_tag_def` 为准。",
        "",
    ]
    for cat, items in by_cat.items():
        lines += [f"## {cat}", ""]
        for label, intro, key in [(v[1], v[2], v[3]) for v in items]:
            lines.append(f"### {label}")
            if intro:
                lines.append(f"- 玩法：{intro}")
            if key:
                lines.append(f"- 识别关键点：{key}")
            lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已提取 {len(rows)} 行 → {len(by_cat)} 个大类 / "
          f"{sum(len(v) for v in by_cat.values())} 条玩法 → {OUT}")
    for cat, items in by_cat.items():
        print(f"  {cat:<12} {len(items)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
