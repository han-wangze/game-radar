#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 用模型读商店介绍，生成中文「玩法一句话」+「玩法类型」+「中文译名」
------------------------------------------------------
为什么需要它
  页面卡片上的游戏介绍大多直接取自商店原文：实测当日 8 条里中文 1、英文 2、
  日文 1、韩文 4。规则只能抽句、不能翻译，所以读者看不懂。
  展开区仍保留商店原文（原始语言），中文只用于卡片常显的那一句。

产出（每个产品一次调用，三个字段都落 fact_tag）
  · play_desc  一句话中文玩法（≤40 字）—— 页面常显。
  · like_of    玩法类型短语（≤12 字，如「三消+家装」「水排序」）—— 页面常显。
  · name_cn    产品名的中文译名（≤60 字）—— 周报与日报在原名后括号显示，
               让不读韩文/日文/英文的读者也能认出是哪款。原名已是中文时留空。

落在哪里
  `fact_tag(app_id, 'play_desc' / 'like_of')` —— 和人工打标走同一张表，标签体系
  因此有了实际出口。用 `descr_country` / `descr_sha1` 记生成时依据的是哪国、哪份
  描述（v5 的证据机制）。`method='llm'`、`need_review=1`，与人工标签区分。

密钥
  只从项目根 `.env` 读取，不进命令行、不进对话、不进仓库。
  `.env` 已在 .gitignore 中。格式：
      DEEPSEEK_API_KEY=...
      DEEPSEEK_BASE_URL=https://api.deepseek.com

幂等
  · 人工标签（method='human'）一律跳过，不覆盖
  · 已有 llm 标签且 descr_sha1 未变 → 该字段跳过（另一个字段若仍缺则只补那个）
  · 描述变了 → 重新生成
  · --force 强制重生成（仍不覆盖人工标签）

用法
  python scripts/gen_cn_blurb.py --dry-run          # 只打印将要处理的条目与 prompt 规模
  python scripts/gen_cn_blurb.py                   # 生成最新榜日当日进榜产品
  python scripts/gen_cn_blurb.py --date 2026-11-05 --limit 2
  python scripts/gen_cn_blurb.py --all             # 回填所有历史进榜产品（缺什么补什么）
  python scripts/gen_cn_blurb.py --all --regen-like # 只重生成玩法类型（字段口径变更时用）
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
DB_PATH = ROOT / "data" / "radar.db"
ENV_PATH = ROOT / ".env"

DESCR_PREF = ("us", "cn", "jp", "kr")
MAX_LEN = 40          # 与 dim_tag_def.play_desc.max_len 一致
LIKE_MAX = 12         # 玩法类型短语上限（如「三消+家装」「水排序」）
NAME_MAX = 60         # 与 dim_tag_def.name_cn.max_len 一致
DESCR_CHARS = 2200    # 送给模型的前 N 字符，足够覆盖多数描述的玩法段

SYSTEM_PROMPT = """你是游戏行业分析师，读 App Store 商店介绍，给出一句中文玩法说明、一个玩法类型短语和中文译名。

规则：
1. play_desc：只说这款游戏「怎么玩」——核心循环、玩家做什么、目标是什么。一句话，不超过 40 个汉字；不加引号、不加书名号、不写「这是一款」。描述确实没有玩法信息时，play_desc 输出空字符串。
2. like_of：这款游戏的**玩法类型**，用两三个中文词概括，例如「三消+家装」「水排序」「放置卡牌」「合成经营」「塔防」「开放世界 RPG」「模拟经营」。只写一个短语，不超过 12 字；不要写具体游戏名、不要写厂商名、不要写英文品类词。描述确实看不出玩法时，like_of 输出空字符串。
3. name_cn：产品名的中文译名，让中文读者能认出是哪款。原名已经是中文（不含日文假名、韩文谚文）时输出空字符串；否则给出简洁通用的译名，可省略副标题与注册商标符号，不超过 60 字。不要加引号、书名号或括号。
4. 不要复述营销口号、奖项、版本活动、IP 联动、下载号召、公司信息。
5. 只输出 JSON，不要任何解释、不要 Markdown 代码块包裹，格式严格如下：
{"play_desc": "...", "like_of": "...", "name_cn": "..."}
"""


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        raise RuntimeError(f"缺少 {ENV_PATH}；请把密钥放本地 .env，不要写进命令行")
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def call_deepseek(descr: str, name: str, cc: str, cfg: dict[str, str],
                  retries: int = 3) -> str:
    url = cfg.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    url += "/chat/completions"
    key = cfg.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError(".env 里没有 DEEPSEEK_API_KEY")

    user = (f"产品名：{name}\n商店描述（{cc} 店面，原文，可能被截断）：\n"
            f"{descr[:DESCR_CHARS]}")
    body = json.dumps({
        "model": cfg.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 320,
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
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            text = payload["choices"][0]["message"]["content"].strip()
            text = re.sub(r"\s+", " ", text)
            return text
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
                json.JSONDecodeError, TimeoutError) as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError):
                # 不打印响应体，避免任何上游回显的敏感串进入日志
                last = RuntimeError(f"HTTP {exc.code}")
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"DeepSeek 调用失败：{last}")


def parse_tags(text: str) -> tuple[dict, str | None]:
    """把模型返回的 JSON 解析成 {play_desc, like_of}。

    返回 (obj, err)：err 为 None 表示解析成功；否则 err 是模型对 play_desc 的
    兜底纯文本（兼容旧版模型不输出 JSON 的情况）。
    """
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    try:
        return json.loads(t), None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0)), None
            except json.JSONDecodeError:
                pass
        # 整段无法解析：当作纯 play_desc 兜底（旧行为），like_of 留空
        return {}, t


def ver_of(conn, key: str) -> int:
    r = conn.execute("SELECT ver FROM dim_tag_def WHERE tag_key=?", (key,)).fetchone()
    return r[0] if r else 1


def upsert_tag(conn, app_id: str, key: str, value: str, ver: int,
               cc: str, sha, now: str) -> None:
    conn.execute("""
        INSERT INTO fact_tag(app_id, tag_key, value, confidence,
            evidence_strength, method, model, tag_ver, evidence,
            descr_country, descr_sha1, need_review, tagged_at)
        VALUES (?, ?, ?, NULL, 'inferred', 'llm', 'deepseek-chat',
                ?, ?, ?, ?, 1, ?)
        ON CONFLICT(app_id, tag_key) DO UPDATE SET
            value=excluded.value, method=excluded.method,
            model=excluded.model, tag_ver=excluded.tag_ver,
            evidence=excluded.evidence, descr_country=excluded.descr_country,
            descr_sha1=excluded.descr_sha1, need_review=excluded.need_review,
            tagged_at=excluded.tagged_at
    """, (app_id, key, value, ver,
          f"DeepSeek 依据 {cc} 店面商店描述生成；descr_sha1={sha}",
          cc, sha, now))


def pick_descr(conn, app_id: str, country: str):
    """优先用该产品进榜所在店面的描述；拿不到按 us→cn→jp→kr 兜底。"""
    rows = conn.execute(
        "SELECT country, descr, descr_sha1 FROM dim_app_country "
        "WHERE app_id=? AND descr IS NOT NULL AND TRIM(descr)<>''", (app_id,)).fetchall()
    by = {r[0]: (r[1], r[2]) for r in rows}
    if country in by:
        return country, by[country][0], by[country][1]
    for cc in DESCR_PREF:
        if cc in by:
            return cc, by[cc][0], by[cc][1]
    return None, "", None


def is_cn_name(name: str) -> bool:
    """原名是否已经是中文（含汉字、且不含日文假名/韩文谚文）。

    只含汉字的日文名（如「戦国」）会被当成中文而跳过译名——这是有意的取舍：
    误译一个汉字名比漏译更糟，读者至少能认出其中汉字。
    """
    s = name or ""
    return (bool(re.search(r"[\u4e00-\u9fff]", s))
            and not re.search(r"[\u3040-\u30ff\uac00-\ud7af]", s))


# ── 取目标 ───────────────────────────────────────────────────
def targets(conn, date: str, limit: int | None, all_mode: bool = False):
    if all_mode:
        # 回填：每个产品取它最早一次进榜作代表（译名与玩法与榜日无关）
        sql = """
            WITH e AS (
                SELECT n.*, ROW_NUMBER() OVER (
                         PARTITION BY n.app_id
                         ORDER BY n.entry_date, n.entry_rank) AS rn
                  FROM v_new_entrant n
            )
            SELECT app_id, name, country, entry_date FROM e WHERE rn = 1
             ORDER BY entry_date, country
        """
        rows = conn.execute(sql).fetchall()
    else:
        sql = """
            SELECT n.app_id, n.name, n.country, n.entry_date
              FROM v_new_entrant n
             WHERE n.entry_date = ?
             ORDER BY n.country, n.entry_rank
        """
        rows = conn.execute(sql, (date,)).fetchall()
    return rows[:limit] if limit else rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--date", default=None, help="榜日，默认最新")
    ap.add_argument("--all", action="store_true",
                    help="回填所有历史进榜产品（每产品取最早一次进榜）")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--regen-like", action="store_true",
                    help="只重生成 like_of（玩法类型口径变更时用，其余字段仍按 sha 幂等）")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    if args.regen_like:
        # 字段口径从「标杆产品 like」改成「玩法类型短语」时，清掉全库仍带旧后缀的
        # 残留值。其中一部分是不在 v_new_entrant 里的老产品标签（页面不显示），
        # 光靠下面的 targets 遍历覆盖不到。留空而不是删行，以后这些产品进榜会重生成。
        # 只匹配「空格 + like」结尾，不会误伤 Roguelike 这类合法玩法词。
        cur = conn.execute(
            "UPDATE fact_tag SET value='' WHERE tag_key='like_of' "
            "AND trim(value) LIKE '% like'")
        conn.commit()
        if cur.rowcount:
            print(f"清理旧口径残留 {cur.rowcount} 条")
    date = args.date or conn.execute(
        "SELECT MAX(snap_date) FROM fact_rank").fetchone()[0]
    rows = targets(conn, date, args.limit, args.all)
    ver_play = ver_of(conn, "play_desc")
    ver_like = ver_of(conn, "like_of")
    ver_name = ver_of(conn, "name_cn")
    scope = "全部历史进榜产品" if args.all else f"榜日 {date} 当日进榜"
    print(f"{scope}　{len(rows)} 条")

    cfg = None if args.dry_run else load_env()
    todo = []
    for app_id, name, cc, entry in rows:
        cc_used, descr, sha = pick_descr(conn, app_id, cc)
        if not descr:
            print(f"  跳过（无描述）  {name[:24]}")
            continue
        old_play = conn.execute(
            "SELECT value, method, descr_sha1 FROM fact_tag "
            "WHERE app_id=? AND tag_key='play_desc'", (app_id,)).fetchone()
        old_like = conn.execute(
            "SELECT value, method, descr_sha1 FROM fact_tag "
            "WHERE app_id=? AND tag_key='like_of'", (app_id,)).fetchone()
        old_name = conn.execute(
            "SELECT value, method, descr_sha1 FROM fact_tag "
            "WHERE app_id=? AND tag_key='name_cn'", (app_id,)).fetchone()
        skip_play = bool(old_play and (old_play[1] == "human" or
                                      (not args.force and old_play[2] == sha)))
        skip_like = bool(old_like and old_like[1] == "human") or bool(
            old_like and not args.force and not args.regen_like
            and old_like[2] == sha)
        # 原名已是中文就不需要译名；否则按 sha 判幂等
        skip_name = is_cn_name(name) or bool(
            old_name and (old_name[1] == "human" or
                          (not args.force and old_name[2] == sha)))
        if skip_play and skip_like and skip_name:
            continue  # 三个字段都齐且未变，不调 API
        todo.append((app_id, name, cc_used, descr, sha,
                     skip_play, skip_like, skip_name, entry))

    print(f"待生成 {len(todo)} 条")
    if args.dry_run:
        for app_id, name, cc, descr, sha, sp, sl, sn, entry in todo:
            print(f"  {name[:26]:<28} 店面={cc} 描述长度={len(descr)} "
                  f"需play={not sp} 需like={not sl} 需译名={not sn}")
        conn.close()
        return 0

    # 戳成被处理的榜日（entry_date），不是脚本运行时刻。
    # 这样标签天然属于它该出现的那天，build_site 的「历史页 tagged_at<=cut」规则
    # 才能正确放行——否则标签会被戳成运行日，历史页永远看不到刚生成的标签。
    ok = fail = 0
    streak_fail = 0
    for app_id, name, cc, descr, sha, skip_play, skip_like, skip_name, entry in todo:
        now = entry
        try:
            text = call_deepseek(descr, name, cc, cfg)
            obj, err = parse_tags(text)
            if err is not None:
                # 旧版模型未输出 JSON：整段兜底为 play_desc，其余留空
                pd, lk, nc = err.strip(), "", ""
            else:
                pd = (obj.get("play_desc") or "").strip()
                lk = (obj.get("like_of") or "").strip()
                nc = (obj.get("name_cn") or "").strip()
            # 只做长度约束，不做词表/自 IP 判定：玩法类型短语不涉及具体产品名。
            if len(pd) > MAX_LEN:
                pd = pd[:MAX_LEN].rstrip("，,、 ")
            if len(lk) > LIKE_MAX:
                lk = lk[:LIKE_MAX].rstrip("，,、 ")
            if len(nc) > NAME_MAX:
                nc = nc[:NAME_MAX].rstrip("，,、 ")
            wrote = []
            if not skip_play and pd:
                upsert_tag(conn, app_id, "play_desc", pd, ver_play, cc, sha, now)
                wrote.append("play_desc")
            if not skip_like and lk:
                upsert_tag(conn, app_id, "like_of", lk, ver_like, cc, sha, now)
                wrote.append("like_of")
            if not skip_name and nc:
                upsert_tag(conn, app_id, "name_cn", nc, ver_name, cc, sha, now)
                wrote.append("name_cn")
            conn.commit()
            ok += 1
            streak_fail = 0
            print(f"  ✓ {name[:24]:<26} {wrote} | "
                  f"play={pd[:16]!r} like={lk[:14]!r} cn={nc[:16]!r}")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            streak_fail += 1
            print(f"  ✗ {name[:26]:<28} {exc}")
            # API 整体不可用时逐条重试会拖垮整轮自动化；连续 3 条失败就停，
            # 剩下的留到下次运行（幂等，descr_sha1 未变的下次会被跳过）
            if streak_fail >= 3:
                print("  连续 3 条失败，提前中止；其余留待下次运行")
                break
        time.sleep(0.4)
    conn.close()
    print(f"完成：成功 {ok} / 失败 {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
