#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 生成日韩商店描述的中文译文（本地模型，DeepSeek）
------------------------------------------------------------
为什么需要它
  卡片点开后会展开商店原文，而日韩产品的原文是日文/韩文，读者读不懂；
  英美产品的原文是英文，读者读得懂。所以只翻日韩。

判定语言按**描述正文**，不按店面
  实测存在「美国店面返回日文名 + 日文描述」的情况（Apple 的元数据语言与店面
  不一致）。按 country 判定会漏翻，也可能把英文描述送去翻一遍。
  因此这里检测正文：含假名 → 日文，含谚文 → 韩文，其余（含纯汉字中文、
  纯英文）一律跳过。

产出
  fact_tag.descr_cn  商店描述前 600 字的中文译文（≤600 字）
  它是译文而不是摘要：保留原文语气与顺序，不替厂商改写成中性描述，
  这样读者才能拿 play_desc 与原文核对。

幂等
  · 人工标签（method='human'）跳过
  · descr_sha1 未变 → 跳过（--force 可强制重生成）
  · 描述变了 → 重生成

用法
  python scripts/gen_descr_cn.py --dry-run        # 只统计待翻条数与语种
  python scripts/gen_descr_cn.py                  # 翻所有历史进榜产品里的日韩产品
  python scripts/gen_descr_cn.py --limit 5
  python scripts/gen_descr_cn.py --only-missing   # 只补库里确实没有译文的条目（不重翻存量）
  python scripts/gen_descr_cn.py --force          # 忽略 descr_sha1 全量重排
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_cn_blurb as G  # noqa: E402 —— 复用 .env 读取 / upsert / 取描述 / 取版本

DB_PATH = ROOT / "data" / "radar.db"
DESCR_CN_MAX = 600      # 与 dim_tag_def.descr_cn.max_len 一致
SRC_CHARS = 700         # 送给模型的原文字符数（译文上限 600 字，原文留些余量）

SYSTEM_PROMPT = """你是翻译，把 App Store 商店描述译成中文。

规则：
1. 逐段翻译，保留原文的段落顺序、语气与卖点强度。原文是宣传口吻就译成宣传口吻，不要改成中立的客观描述，也不要删掉感叹号与号召句。
2. 不增不减：不补充原文没有的信息，不添加译者注、不加小标题、不总结。
3. 游戏术语用中文玩家熟悉的说法（如「ガチャ」→「抽卡」、「뽑기」→「抽卡」、「放置」保留）。
4. 原文里的专有名词（游戏名、角色名、公司名）保留原文，可在首次出现时用括号附中文。
5. 用户消息就是要翻译的原文本身，直接从第一句译起。不要输出「产品名：」「原文语种：」「商店描述原文：」这类标签或前言，也不要任何说明。
6. 只输出译文正文，不要 Markdown 代码块、不要「译文：」这类前缀。
"""

# 模型偶尔会把 prompt 里的元信息标签原样抄进译文（首轮 110 条里 21 条中招，
# 都是开头多一行「产品名：XXX」）。两道防线：user 消息只放原文 + 落库前清洗。
_META_LINE = re.compile(r"^\s*(产品名|原文语种|商店描述原文|原文|产品)\s*[:：]")


def clean_translation(text: str) -> str:
    """删掉译文里被模型抄进来的元信息行与代码块围栏。"""
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text or "").strip()
    t = re.sub(r"^(译文|中文翻译|翻译)\s*[:：]\s*", "", t).strip()
    lines = [ln for ln in t.split("\n") if not _META_LINE.match(ln)]
    return "\n".join(lines).strip()


def truncate_cn(text: str, max_len: int = DESCR_CN_MAX) -> str:
    """把译文截到不超过 max_len 字，且尽量落在句末标点，避免停在半句。

    优先级：最后一个落在上限内的「句末标点（。！？.!?）」→ 否则最后一个
    「逗号/顿号（，、,;；）」→ 否则硬切。无论哪种，截断都补省略号，提示读者
    下面还有原文可展开。上限内的完整译文（含本身就在句末结束的）原样返回。
    """
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    cut = 0
    for i, ch in enumerate(t):
        if i >= max_len:
            break
        if ch in "。！？.!?":
            cut = i + 1
    if cut == 0:                       # 整段无句末标点（宣传长句），退而求其次切在 clauses
        for i, ch in enumerate(t):
            if i >= max_len:
                break
            if ch in "，、,;；":
                cut = i + 1
    if cut == 0:                       # 整段无标点，硬切
        cut = max_len
    return t[:cut].rstrip("，,、。 ") + "…"


def script_of(text: str) -> str:
    """按正文判定语种：ja / ko / zh / other。

    只认假名与谚文——纯汉字按中文处理（日文汉字名与中文无法可靠区分，
    误判成日文会平白多翻一遍，而中日汉字互读性尚可）。
    """
    s = text or ""
    if re.search(r"[\u3040-\u30ff]", s):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", s):
        return "ko"
    if re.search(r"[\u4e00-\u9fff]", s):
        return "zh"
    return "other"


def call_translate(descr: str, name: str, lang: str,
                   cfg: dict[str, str], retries: int = 3) -> str:
    url = cfg.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    url += "/chat/completions"
    key = cfg.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError(".env 里没有 DEEPSEEK_API_KEY")

    user = descr[:SRC_CHARS]
    body = json.dumps({
        "model": cfg.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": 1200,
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")

    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json",
                         "User-Agent": "game-radar/0.1"},
                method="POST")
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            text = payload["choices"][0]["message"]["content"].strip()
            return clean_translation(text)
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
                json.JSONDecodeError, TimeoutError) as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError):
                last = RuntimeError(f"HTTP {exc.code}")
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"DeepSeek 调用失败：{last}")


def targets(conn, limit: int | None, *, only_missing: bool = False,
            force: bool = False):
    """所有进榜产品（每产品取最早一次进榜作代表），返回需要翻的日韩条目。

    only_missing：只翻「库里没有非空 descr_cn」的条目，有译文就跳过、不看 sha。
      存量 110 条译文是 tagged_at 机制之前写入的，descr_sha1 全为 NULL；
      按 sha 判幂等时 `NULL == sha` 恒假，会把它们全部判成「描述变了」而重翻。
      这个开关用来只补真正缺译文的少数条目，保住已清洗过的内容。默认关闭，
      日常自动化按 descr_sha1 幂等的行为完全不变。
    force：忽略 descr_sha1 全量重排（人工标签仍跳过）。
    """
    rows = conn.execute("""
        WITH e AS (
            SELECT n.*, ROW_NUMBER() OVER (
                     PARTITION BY n.app_id
                     ORDER BY n.entry_date, n.entry_rank) AS rn
              FROM v_new_entrant n
        )
        SELECT app_id, name, country, entry_date FROM e WHERE rn = 1
         ORDER BY entry_date, country
    """).fetchall()
    out = []
    stats = {"ja": 0, "ko": 0, "zh": 0, "other": 0, "nodesc": 0, "skip": 0}
    for app_id, name, cc, entry in rows:
        cc_used, descr, sha = G.pick_descr(conn, app_id, cc)
        if not descr:
            stats["nodesc"] += 1
            continue
        lang = script_of(descr)
        stats[lang] += 1
        if lang not in ("ja", "ko"):
            continue
        old = conn.execute(
            "SELECT method, descr_sha1, value FROM fact_tag "
            "WHERE app_id=? AND tag_key='descr_cn'", (app_id,)).fetchone()
        # 人工标签一律不覆盖，三种模式都遵守
        if old and old[0] == "human":
            stats["skip"] += 1
            continue
        if only_missing:
            # 有非空译文就跳过，不看 sha、不重翻
            if old and (old[2] or "").strip():
                stats["skip"] += 1
                continue
        elif old and not force and old[1] == sha:
            # 默认口径：描述没变就跳过；--force 忽略 sha 全量重排
            stats["skip"] += 1
            continue
        out.append((app_id, name, cc_used, descr, sha, lang, entry))
    return out, stats, len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="忽略 descr_sha1 全量重排（人工标签仍跳过）")
    ap.add_argument("--only-missing", action="store_true",
                    help="只翻库里没有非空 descr_cn 的条目，不重翻存量译文")
    ap.add_argument("--clean", action="store_true",
                    help="只清洗已落库译文里被模型抄进来的元信息行，不调 API")
    args = ap.parse_args()
    if args.force and args.only_missing:
        ap.error("--force 与 --only-missing 互斥：前者全量重排，后者只补缺失")

    conn = sqlite3.connect(args.db)
    if args.clean:
        rows = conn.execute(
            "SELECT app_id, value FROM fact_tag WHERE tag_key='descr_cn'").fetchall()
        fixed = 0
        for app_id, v in rows:
            nv = clean_translation(v)
            if nv and nv != v:
                conn.execute("UPDATE fact_tag SET value=? "
                             "WHERE app_id=? AND tag_key='descr_cn'", (nv, app_id))
                fixed += 1
        conn.commit()
        conn.close()
        print(f"清洗完成：{len(rows)} 条译文里 {fixed} 条有改动")
        return 0
    todo, stats, total = targets(conn, args.limit,
                                 only_missing=args.only_missing,
                                 force=args.force)
    mode = "（只补缺失）" if args.only_missing else ("（强制全量）" if args.force else "")
    print(f"进榜产品 {total} 个{mode}："
          f"日文 {stats['ja']} / 韩文 {stats['ko']} / 中文 {stats['zh']} / "
          f"其他语种 {stats['other']} / 无描述 {stats['nodesc']}")
    print(f"待翻 {len(todo)} 条（跳过 {stats['skip']} 条）")
    if args.dry_run:
        for app_id, name, cc, descr, sha, lang, entry in todo[:20]:
            print(f"  [{lang}] {name[:30]:<32} 店面={cc} 原文 {len(descr)} 字")
        conn.close()
        return 0
    if not todo:
        conn.close()
        print("没有需要翻译的条目。")
        return 0

    cfg = G.load_env()
    ver = G.ver_of(conn, "descr_cn")
    ok = fail = 0
    streak = 0
    for app_id, name, cc, descr, sha, lang, entry in todo[:args.limit] if args.limit else todo:
        try:
            zh = call_translate(descr, name, lang, cfg)
            if not zh:
                raise RuntimeError("返回空译文")
            zh = truncate_cn(zh)        # 句末边界截断，避免停在半句
            G.upsert_tag(conn, app_id, "descr_cn", zh, ver, cc, sha, entry)
            conn.commit()
            ok += 1
            streak = 0
            print(f"  ✓ [{lang}] {name[:26]:<28} {len(zh)} 字 | {zh[:24]}…")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            streak += 1
            print(f"  ✗ [{lang}] {name[:26]:<28} {exc}")
            if streak >= 3:
                print("  连续 3 条失败，提前中止；其余留待下次运行")
                break
        time.sleep(0.4)
    conn.close()
    print(f"完成：成功 {ok} / 失败 {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
