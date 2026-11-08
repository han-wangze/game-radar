#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 自然周独立静态页周报
------------------------------------
定位：台账按周的回看索引，不是分析报告。

与日报的边界
  日报：当日切片、逐条、及时，回答「昨天谁进来了、它是什么」
  周报：本周聚合、**按产品去重**、可回看，回答「这周一共进了哪些、各自什么成色」
  周报不重复日报 02–05 分析板块，不写「值得跟 / 趋势 / 推荐」。

自然周怎么定
  周一 00:00 ~ 周日 23:59，按库内**最新榜日**回退到上一个已结束且
  7 个榜日齐全的自然周；不足 7 天就继续往前找（最多回退 4 周）。
  用库内日期而不是系统日期，避免自动化在没采到数据的那天选错周。

  ⚠️ SQLite 的 date(d,'weekday 1','-7 days') 对周一当天会退回上周一，
  这里用 ((strftime('%w',d)+6)%7) 算「距本周一几天」，不受这个坑影响。

残周
  面板从 2026-10-23（周五）起，10-19~10-25 那一周只有 3 个榜日。
  周报在「与上周对照」里如实标出榜日数，不把 3 天的数当 7 天比。

用法
  python scripts/build_weekly.py                      # 生成 site/weekly.html
  python scripts/build_weekly.py --week-start 2026-10-26   # 指定周（仍会校验榜日数）
  python scripts/build_weekly.py --out site/weekly_1026.html
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_site as S  # noqa: E402 —— 复用 CSS / CN / esc / n，避免第二套样式

DB_PATH = ROOT / "data" / "radar.db"
OUT_PATH = ROOT / "site" / "weekly.html"

GROUPS = (("new", "新上架（≤30 天）"),
          ("mid", "上架 31–90 天"),
          ("return", "上架 >90 天 · 回榜"),
          ("unknown", "上架天数未知 / 预注册"))

WEEK_CSS = """
/* 周报专用：台账行（每行带玩法 / 译名 / 在榜 N-M 天） + 简单表格 */
.wk{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
box-shadow:var(--sh);overflow:hidden;margin-bottom:10px}
.wk-head,.wk-row{display:grid;grid-template-columns:74px minmax(0,1fr) 66px;
gap:12px;align-items:center;padding:9px 15px}
.wk-head{background:var(--soft);border-bottom:1px solid var(--line);font-size:10.5px;
color:var(--faint);font-weight:600;letter-spacing:.04em}
.wk-head span:last-child{text-align:right}
.wk-row{border-bottom:1px solid var(--line)}
.wk-row:last-child{border-bottom:0}
.wk-row:hover{background:#fbfcfe}
.wk-side{display:flex;flex-direction:column;gap:1px;line-height:1.35;min-width:0}
.wk-date{font-family:var(--mono);font-size:10.5px;color:var(--faint)}
.wk-mkt{font-size:11px;color:var(--muted)}
.wk-rk{font-family:var(--mono);font-size:11px;font-weight:650;color:var(--ink-2)}
.wk-nm{font-size:14px;font-weight:650;line-height:1.4;word-break:break-word}
.wk-nm .cn{font-size:11.5px;font-weight:400;color:var(--muted)}
.wk-play{font-size:12.5px;color:var(--ink-2);margin-top:3px;line-height:1.55}
.wk-facts{font-size:11px;color:var(--muted);margin-top:4px;line-height:1.5}
.wk-st{text-align:right}
.st-on{font-size:10px;font-weight:650;color:var(--new-ink);background:var(--new-bg);
border:1px solid var(--new-line);border-radius:999px;padding:1px 7px;white-space:nowrap}
.st-off{font-size:10px;color:var(--faint);background:var(--soft);border:1px solid var(--line);
border-radius:999px;padding:1px 7px;white-space:nowrap}
@media(max-width:720px){
 .wk-head{display:none}
 .wk-row{grid-template-columns:1fr auto;gap:4px 10px;padding:11px 14px}
 .wk-side{grid-column:1;flex-direction:row;gap:8px;align-items:baseline}
 .wk-main{grid-column:1 / -1}
 .wk-st{grid-column:2;grid-row:1}
}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
th{font-size:10.5px;color:var(--faint);font-weight:600;background:var(--soft);
letter-spacing:.03em}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.tbl-wrap{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
box-shadow:var(--sh);overflow:hidden;margin-bottom:10px}
.tbl-wrap th{border-bottom:1px solid var(--line)}
.tbl-wrap tr:last-child td{border-bottom:0}
.cav{font-size:12.5px;color:var(--ink-2);line-height:1.8;margin:0 0 10px}
.cav:last-child{margin-bottom:0}
"""

# 周报「口径」板块文案（单一来源，render 04 段只引用这里，杜绝与日报漂移）。
# 三段连贯短文，不用 bullet、不写防御性小节标题。prev_note 仅在上一周榜日不足时渲染。
WEEKLY_CAVEAT = [
    "周期为自然周，取库内最近一个榜日齐全的已结束周。同一产品在多个市场进榜时按 "
    "app_id 去重，取最早一次；位次分布与上周对照同此口径。",
    "「在榜 N/M 天」是榜位状态而非留存：M 是进榜后到周末的可观察榜日数，N 是其中"
    "连续在榜的天数，中途掉出再回来则重新计算。本周 {n_app} 款中仅 {full} 款有完整 "
    "{days} 天窗，其余只宜逐条参考。{prev_note}",
    "上架天数、厂商、品类取该产品所在店面的值；玩法、玩法类型与译名由模型基于商店介绍生成。",
]


# ── 周期选择 ──────────────────────────────────────────────────────────

def monday(d: str) -> date:
    x = date.fromisoformat(d)
    return x - timedelta(days=(x.weekday()))


def week_days(conn, start: date, end: date) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT snap_date) FROM fact_rank "
        "WHERE snap_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat())).fetchone()[0]


def choose_week(conn, forced: str | None):
    """返回 (start, end, 榜日数, 是否完整)。默认找最近一个 7 天齐全的已结束周。"""
    latest = conn.execute("SELECT MAX(snap_date) FROM fact_rank").fetchone()[0]
    if not latest:
        raise RuntimeError("库里还没有榜单数据")
    if forced:
        st = date.fromisoformat(forced)
        if st.weekday() != 0:
            raise ValueError("周起始日必须是周一")
        # 显式指定就是「要看这一周」，不自动回退到别的周；榜日不足由页面标注
        en = st + timedelta(days=6)
        days = week_days(conn, st, en)
        return st, en, days, days == 7
    else:
        st = monday(latest) - timedelta(days=7)   # 上一个已结束的自然周
    for _ in range(4):
        en = st + timedelta(days=6)
        days = week_days(conn, st, en)
        if days == 7:
            return st, en, days, True
        st -= timedelta(days=7)
    # 都不完整：返回最近的那一周，由页面标注「榜日不足」
    st = (date.fromisoformat(forced) if forced
          else monday(latest) - timedelta(days=7))
    return st, st + timedelta(days=6), week_days(conn, st, st + timedelta(days=6)), False


# ── 取数 ──────────────────────────────────────────────────────────────

def load(conn, st: date, en: date):
    a, b = st.isoformat(), en.isoformat()
    d: dict = {"start": a, "end": b}
    d["stamp"] = conn.execute("SELECT MAX(run_at) FROM job_log").fetchone()[0] or "—"

    # 本周首次进榜事件 → 按产品去重（取最早进榜那条作代表）
    d["events"] = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT app_id) FROM v_new_entrant "
        "WHERE entry_date BETWEEN ? AND ?", (a, b)).fetchone()
    # 周报是「站在当前回看上一完整周」的视角，标签子查询不带日报的 /*TAGCUT*/：
    # 读取的是库里当前值，包括 tagged_at 为 NULL 的历史标签与本周之后才生成的标签。
    # 这与日报历史页（按 tagged_at <= cut 还原当日快照）是**有意的不对称**，
    # 不要在对齐改动里顺手给它加时间过滤；若日后要严格 as-of，需单独立项。
    d["apps"] = conn.execute("""
        WITH e AS (
            -- 第三级 country 是确定性兜底：同日同位次的跨市场事件（如 6444520814）
            -- 否则由 SQLite 物理顺序决定代表国，按市场分组时会两次重建落到不同组。
            SELECT n.*, ROW_NUMBER() OVER (
                     PARTITION BY n.app_id
                     ORDER BY n.entry_date, n.entry_rank, n.country) AS rn
              FROM v_new_entrant n
             WHERE n.entry_date BETWEEN ? AND ?
        )
        SELECT e.country, e.name, e.entry_rank, e.entry_date, e.age_days,
               COALESCE(NULLIF(TRIM(e.seller), ''), '') AS seller,
               e.app_id, v.sub_genre,
               (SELECT value FROM fact_tag t
                 WHERE t.app_id=e.app_id AND t.tag_key='play_desc') AS blurb,
               (SELECT value FROM fact_tag t
                 WHERE t.app_id=e.app_id AND t.tag_key='like_of') AS like_of,
               (SELECT value FROM fact_tag t
                 WHERE t.app_id=e.app_id AND t.tag_key='name_cn') AS name_cn
          FROM e
          LEFT JOIN v_rank_sub v
                 ON v.snap_date=e.entry_date AND v.country=e.country
                AND v.app_id=e.app_id
         WHERE e.rn=1
         ORDER BY e.entry_date, e.country, e.entry_rank
    """, (a, b)).fetchall()

    # 逐条「在榜 N/M 天」：从进榜日起连续在榜天数 / 该日到周末的可观察天数。
    # 只做逐条事实，不汇总成率——98 款的观察窗 1–7 天不等，
    # 任何单一比率都会被「周末才进榜、只观察到 1 天」的产品抬高。
    on: dict[str, set] = {}
    for aid, sd in conn.execute(
            "SELECT DISTINCT app_id, snap_date FROM fact_rank "
            "WHERE snap_date BETWEEN ? AND ?", (a, b)):
        on.setdefault(aid, set()).add(sd)
    end_d = date.fromisoformat(b)
    d["stay"] = {}
    for aid, ed in conn.execute(
            "SELECT app_id, MIN(entry_date) FROM v_new_entrant "
            "WHERE entry_date BETWEEN ? AND ? GROUP BY app_id", (a, b)):
        # 必须 GROUP BY 取 MIN：同一款可能在多个市场分别进榜、进榜日不同，
        # 逐条 (app_id, entry_date) 迭代会让后一个日期覆盖前一个，
        # M（可观察天数）就跟行上显示的日期对不上了。
        have = on.get(aid, set())
        d0 = date.fromisoformat(ed)
        n_on = 0
        while (d0 + timedelta(days=n_on)).isoformat() in have:
            n_on += 1
        d["stay"][aid] = (n_on, (end_d - d0).days + 1)

    # 这里原有「市场 × 品类」分布（98 款分 4 国再分 16 个品类，每格 1–5 个）。
    # 已删除：样本撑不起交叉表，读者也读不出结论；品类结构看日报的榜位份额更准。
    # d["genres"] / d["countries"] 一并去掉，避免留着没人用的查询。

    # 固定暴露期 cohort 查询已随 02 段（留榜率）一并删除：
    # 汇总率被逐条的「在榜 N/M 天」取代，这里不再需要。
    # 「在榜 N/M 天」只用本周 7 个榜日，不跨窗口，所以也不需要 panel_end。

    # 上一自然周（可能残周，只报事实不做趋势）
    ps, pe = st - timedelta(days=7), st - timedelta(days=1)
    d["prev"] = {
        "start": ps.isoformat(), "end": pe.isoformat(),
        "days": week_days(conn, ps, pe),
        "events": conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT app_id) FROM v_new_entrant "
            "WHERE entry_date BETWEEN ? AND ?",
            (ps.isoformat(), pe.isoformat())).fetchone(),
    }
    return d


# ── 渲染 ──────────────────────────────────────────────────────────────

def group_key(age):
    if age is None or age < 0:
        return "unknown"
    if age <= 30:
        return "new"
    if age <= 90:
        return "mid"
    return "return"


def rows_html(rows, stay):
    """台账行：日期·市场·名次 ／ 名称（含译名）· 玩法 · 玩法类型+品类+上架+厂商 ／ 在榜 N/M 天。

    用行卡片而不是表格：每款都要带一句玩法，表格的列宽会把中文挤成两三个字一行。
    每条只占三行（名称 / 玩法 / 事实），一屏能扫过 8–10 款——上周还是四行、
    98 条平铺下来读者根本扫不完。
    """
    out = ['<div class="wk">',
           '<div class="wk-head"><span>日期 · 市场</span><span>产品 · 玩法</span>'
           '<span style="text-align:right">在榜</span></div>']
    for cc, name, rank, ed, age, seller, aid, genre, blurb, like_of, name_cn in rows:
        age_s = S.age_text(age)
        g = S.genre_cn(genre)
        cn = f'<span class="cn">（{S.esc(name_cn)}）</span>' if name_cn else ""
        play = f'<div class="wk-play">{S.esc(blurb)}</div>' if blurb else ""
        lk = S.like_text(like_of)
        # 玩法类型与品类/上架/厂商压成同一行，省掉一整行高度；
        # 显式写「品类」标签，避免「玩法类型 X · Casual」被读成「同类 Casual」
        facts = " · ".join(x for x in (
            f'{S.LIKE_LABEL} {lk}' if lk else "",
            f'品类 {g}' if g else "", age_s, seller) if x)
        # 「在榜 N/M 天」：N 连续在榜天数，M 从进榜日到周末的可观察天数。
        # 只写「仍在榜」会让周日才进榜的产品（M=1）变成同义反复，所以把分母写出来。
        n_on, m_obs = stay.get(aid, (0, 1))
        cls = "st-on" if n_on >= m_obs else "st-off"
        st = f'<span class="{cls}">{n_on}/{m_obs} 天</span>'
        out.append(
            f'<div class="wk-row">'
            f'<div class="wk-side"><span class="wk-date">{S.esc(ed[5:])}</span>'
            f'<span class="wk-mkt">{S.esc(S.CN.get(cc, cc))}</span>'
            f'<span class="wk-rk">#{S.n(rank)}</span></div>'
            f'<div class="wk-main"><div class="wk-nm">{S.esc(name)}{cn}</div>'
            f'{play}<div class="wk-facts">{S.esc(facts)}</div></div>'
            f'<div class="wk-st">{st}</div></div>')
    out.append('</div>')
    return "\n".join(out)


def render(d, days: int, complete: bool, nav: str) -> str:
    A: list[str] = []
    add = A.append
    add('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">')
    add('<meta name="viewport" content="width=device-width,initial-scale=1">')
    add(f'<title>四国新游周报 · {d["start"]} ~ {d["end"]}</title>')
    add(f'<style>{S.CSS}{WEEK_CSS}</style></head><body><div class="wrap">')

    n_ev, n_app = d["events"]

    # 先分组：指标卡要用新上架/回榜的拆分数，不能等到 01 段才算
    by: dict[str, list] = {}
    for r in d["apps"]:
        by.setdefault(group_key(r[4]), []).append(r)
    n_new = len(by.get("new", []))
    n_ret = len(by.get("return", []))
    n_mid = len(by.get("mid", []))
    n_unk = len(by.get("unknown", []))
    # 进榜位次：这一批是「打进可见位置」还是「榜尾擦边」。
    # 用它替代原来的留榜率——位次是本周内的事实，不受观察窗长短影响。
    n_top10 = sum(1 for r in d["apps"] if r[2] <= 10)
    n_top30 = sum(1 for r in d["apps"] if r[2] <= 30)

    nav_html = f'<div class="nav">{nav}</div>' if nav else ""
    add(f'<div class="top"><div class="brand"><i></i>game-radar <em>新游台账</em></div>'
        f'{nav_html}</div>')
    add(f'<div class="hero"><div class="eyebrow">App Store · 周报</div>'
        f'<h1>本周新游</h1>'
        f'<p class="deck">美国 / 中国 / 日本 / 韩国 <b>Top100</b>，本周<b>第一次进榜</b>的产品：'
        f'玩法、玩法类型与进榜名次。</p>'
        f'<div class="chips">'
        f'<span class="chip">统计周期 <b>{d["start"]} → {d["end"]}</b></span>'
        f'<span class="chip">榜日 <b class="{"" if complete else "bad"}">{days} / 7</b></span>'
        f'<span class="chip">快照 <code>{S.esc(S.stamp_text(d["stamp"]))}</code></span>'
        f'</div></div>')

    add(f'<div class="stats">'
        f'<div class="stat"><div class="k">本周首次进榜</div>'
        f'<div class="v">{S.n(n_app)} 款</div>'
        f'<div class="h">进榜事件 {S.n(n_ev)} 次（可在多市场分别进榜）</div></div>'
        f'<div class="stat"><div class="k">其中新上架</div>'
        f'<div class="v">{S.n(n_new)} 款</div>'
        f'<div class="h">回榜 {S.n(n_ret)} 款 · '
        f'上架 {S.NEW_AGE + 1}–{S.RETURN_AGE} 天 {S.n(n_mid)} 款</div></div>'
        f'<div class="stat"><div class="k">进榜前 30 位</div>'
        f'<div class="v">{S.n(n_top30)} 款</div>'
        f'<div class="h">前 10 位 {S.n(n_top10)} 款 · '
        f'其余 {S.n(n_app - n_top30)} 款从 31–100 位进来</div></div>'
        f'</div>')

    # 01 清单
    add('<section class="sec"><div class="sec-h"><span class="idx">01</span>'
        '<h2>本周首次进榜</h2></div>')
    from collections import Counter
    mk = Counter(r[0] for r in d["apps"])
    mk_txt = "　·　".join(f"{S.CN.get(cc, cc)} {S.n(mk[cc])}" for cc in S.ORDER if mk.get(cc))
    add(f'<p class="sec-d">按产品去重后 {S.n(n_app)} 款（同一款在多个市场分别进榜时只列一次，'
        f'取最早那条）。{mk_txt}。</p>')
    for key, label in GROUPS:
        rows = by.get(key, [])
        if not rows:
            continue
        # 只默认展开「新上架」：98 款里 59 款是上架超 90 天的回榜，
        # 全展开会让读者扫不到真正的新产品。组标题可点击展开，数量写在标题上。
        if key == "new":
            add(f'<div class="grp">{label}<span class="cnt">{len(rows)} 款</span></div>')
            # 二级分组：只在「新上架」内部按「首次进榜所在市场」切分（r[0]=country）。
            # 稳定分区、不排序：rows 已按 entry_date, country, entry_rank 升序，
            # 切分后每个子组内仍是日期升序。country 是代表条目的进榜市场，
            # 不是产品产地或它的全部市场，故标题写「市场」而非「游戏」。
            sub: dict[str, list] = {}
            for r in rows:
                sub.setdefault(r[0], []).append(r)
            # 顺序沿用 S.ORDER；未知国家码兜底追加，保证任何情况下都不丢行。
            mkt_order = ([cc for cc in S.ORDER if cc in sub]
                         + [cc for cc in sub if cc not in S.ORDER])
            for cc in mkt_order:
                srows = sub[cc]
                add(f'<div class="grp">首次进榜市场 · {S.CN.get(cc, cc)}'
                    f'<span class="cnt">{len(srows)} 款</span></div>')
                add(rows_html(srows, d["stay"]))
        else:
            add(f'<details class="grp-fold"><summary class="grp">{label}'
                f'<span class="cnt">{len(rows)} 款</span></summary>'
                f'{rows_html(rows, d["stay"])}</details>')
    add('</section>')

    # 02 进榜位次
    # 这里原来放的是留榜率（先 58/98=59%，后改成固定窗 31/74=42%），已删除。
    # 删的理由不是分母没算对，而是这个数没有对照物：一周的 cohort、
    # 74 个样本、面板才 15 个榜日，42% 既不构成趋势也没有基准，
    # 读者看完不知道该拿它跟什么比。逐条的在榜天数保留（见清单右列），
    # 那是每款产品自己的事实，不是汇总出来的结论。
    add('<section class="sec"><div class="sec-h"><span class="idx">02</span>'
        '<h2>进榜名次分布</h2></div>')
    BUCKETS = [("1–10 位", 1, 10), ("11–30 位", 11, 30),
               ("31–50 位", 31, 50), ("51–100 位", 51, 100)]
    bk = [(lab, sum(1 for r in d["apps"] if lo <= r[2] <= hi))
          for lab, lo, hi in BUCKETS]
    # 条形长度与同行打印的百分比用同一个分母（本周 N 款）。
    # 原来用 mx=max(各档) 作条形分母、却用 n_app 算百分比，两者自相矛盾，
    # 且 51–100 位常是最大档 → 恒 100% 宽，把其他三档压扁。
    add(f'<div class="mkt"><div class="name">进榜位次'
        f'<span>{S.n(n_app)} 款 · 按产品去重</span></div>')
    for lab, cnt in bk:
        add(f'<div class="row"><div class="lab">{lab}</div>'
            f'<div class="track"><i style="width:{max(cnt * 100.0 / (n_app or 1), 1):.1f}%"></i></div>'
            f'<div class="val">{S.n(cnt)} 款 · {S.pct(cnt / n_app if n_app else 0, 0)}</div></div>')
    add('</div>')
    add(f'<p class="sec-d">条形长度 = 该档占本周 {S.n(n_app)} 款的比例（线性刻度，满格 = 100%）。</p>')
    add(f'<p class="sec-d">位次按产品去重后取最早那次进榜。'
        f'{S.n(n_app)} 款里 {S.n(n_top30)} 款进的是前 30 位，'
        f'{S.n(n_app - n_top30)} 款落在 31–100 位，其中 {S.n(bk[-1][1])} 款在 51–100 位。</p>')
    add('</section>')

    # 03 上周对照
    p = d["prev"]
    add('<section class="sec"><div class="sec-h"><span class="idx">03</span>'
        '<h2>与上周对照</h2></div>')
    add('<div class="tbl-wrap"><table><thead><tr><th>周期</th><th class="num">榜日</th>'
        '<th class="num">进榜次数</th><th class="num">去重产品</th></tr></thead><tbody>')
    add(f'<tr><td>{d["start"]} ~ {d["end"]}（本周）</td><td class="num">{days}</td>'
        f'<td class="num">{S.n(n_ev)}</td><td class="num">{S.n(n_app)}</td></tr>')
    add(f'<tr><td>{p["start"]} ~ {p["end"]}（上一周）</td>'
        f'<td class="num">{p["days"]}</td>'
        f'<td class="num">{S.n(p["events"][0])}</td>'
        f'<td class="num">{S.n(p["events"][1])}</td></tr>')
    add('</tbody></table></div>')
    if p["days"] < 7:
        add(f'<p class="sec-d">上一周只有 {p["days"]} 个榜日'
            f'（面板从 2026-10-23 开始），<b>条数不可直接比较</b>；'
            f'这里保留原始数字供回看，不据此算增减或趋势。</p>')
    add('</section>')

    # 04 口径：保留一个短板块，但改成连贯短文（文案在 WEEKLY_CAVEAT 单一来源），
    # 不再用 lim-h 小节标题与 bullet 列表——那些是 AI 味的来源。
    # prev_note 仅在上一周榜日不足时渲染，避免把一次性数据写死。
    prev_days = d["prev"]["days"]
    prev_note = (f"上周仅 {prev_days} 个榜日，条数不与本周直接比较。"
                 if prev_days < days else "")
    full = sum(1 for _n, _m in d["stay"].values() if _m >= days)
    add('<section class="sec"><div class="sec-h"><span class="idx">04</span>'
        '<h2>口径</h2></div>')
    for para in WEEKLY_CAVEAT:
        txt = S.esc(para.format(n_app=S.n(n_app), full=S.n(full),
                                days=days, prev_note=prev_note))
        add(f'<p class="cav">{txt}</p>')
    add('</section>')

    add(f'<footer>静态页由 <code>scripts/build_weekly.py</code> 生成于 '
        f'{datetime.now().strftime("%Y-%m-%d %H:%M")}（本机时区）。'
        f'数据源 <code>data/radar.db</code>；每个数字可追到 <code>sql/</code> 或本脚本的查询。'
        f'<br><a href="index.html">← 日报（今天）</a>　'
        f'<a href="yesterday.html">日报（昨天）</a>　'
        f'<a href="{S.REPO_URL}">项目源码</a></footer>')
    add('</div></body></html>')
    return "\n".join(A)


def generate(db=DB_PATH, out=OUT_PATH, week_start=None) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        st, en, days, complete = choose_week(conn, week_start)
        d = load(conn, st, en)
        latest = conn.execute("SELECT MAX(snap_date) FROM fact_rank").fetchone()[0]
        nav = (f'<a href="index.html">← 日报（今天 {S.esc(latest)}）</a>　'
               f'<b>周报（{d["start"]} ~ {d["end"]}）</b>　'
               f'<a href="yesterday.html">日报（昨天）→</a>')
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(render(d, days, complete, nav), encoding="utf-8")
        print(f"已生成 {out}　（{d['start']} ~ {d['end']}，{days} 个榜日，"
              f"{d['events'][0]} 次进榜 / {d['events'][1]} 款）")
    finally:
        conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--week-start", default=None,
                    help="周起始日 YYYY-MM-DD，必须是周一；默认最近一个完整自然周")
    args = ap.parse_args()
    return generate(args.db, args.out, args.week_start)


if __name__ == "__main__":
    raise SystemExit(main())
