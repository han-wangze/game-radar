#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 静态页生成
------------------------
把库里的事实渲染成一个**自包含**的 HTML（内联 CSS、无外部 JS/CDN），
双击就能看，也能直接放到 GitHub Pages。

设计约束（来自项目定位，不是审美偏好）：
  1. 只展示事实与基线，不下"值得跟"这类判断；
  2. 每个数字都能追到 sql/ 里的查询或视图；
  3. 首屏写清数据窗口天数；不足 14 天说不能谈存活期，满 14 天说仍不能算平均存活时长；
  4. 没有内容时如实说"今日无新进榜"，不为了填满版面造结论；
  5. 卡片给中文一句玩法（本地模型生成、标注未核验），展开区给商店介绍——
     中文产品本来就是中文、英美产品是英文（读者读得懂）、日韩产品给中文译文
     （fact_tag.descr_cn），三者都读得懂，读者才能拿它核对 play_desc。

口径只写一处：正文里不解释口径，全部集中在末段「数据来源与口径」。
理由是它曾经散在页首折叠、01 段折叠、06 段正文三处，三份说法还会互相漂移。

用法：
    python scripts/build_site.py                          # index + yesterday + weekly
    python scripts/build_site.py --no-yesterday           # 不生成 yesterday.html
    python scripts/build_site.py --no-weekly              # 不生成 weekly.html
    python scripts/build_site.py --date 2026-11-04 --out site/2026-11-04.html
    python scripts/build_site.py --out x.html             # 指定输出（不额外生成其他页）
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "radar.db"
OUT_PATH = ROOT / "site" / "index.html"

CN = {"us": "美国", "cn": "中国", "jp": "日本", "kr": "韩国"}
ORDER = ["us", "cn", "jp", "kr"]

# 「玩法类型」这个位置的显示词。数据里 like_of 存的是模型给出的中文玩法短语
# （如「三消+家装」「水排序」），不是具体游戏名，也不需要剥后缀。
LIKE_LABEL = "玩法类型"

# Apple 的 16 个子类原名是英文，中文页面上直接显示会让「Role Playing · 上架 4 天」
# 这种事实行中英混排。这里只做显示层翻译，库里仍存英文原名（归一规则是按英文写的）。
GENRE_CN = {
    "Action": "动作", "Adventure": "冒险", "Arcade": "街机", "Board": "棋盘",
    "Card": "卡牌", "Casino": "赌场", "Dice": "骰子", "Educational": "教育",
    "Family": "家庭", "Music": "音乐", "Puzzle": "解谜", "Racing": "竞速",
    "Role Playing": "角色扮演", "Simulation": "模拟", "Sports": "体育",
    "Strategy": "策略", "Trivia": "问答", "Word": "文字",
    "Casual": "休闲", "Unclassified": "未分类",
}


def genre_cn(g) -> str:
    if not g:
        return "未分类"
    return GENRE_CN.get(g, g)


def n(v, dash="—"):
    if v is None:
        return dash
    return f"{v:,}"


def pct(v, digits=1, dash="—"):
    if v is None:
        return dash
    return f"{v * 100:.{digits}f}%"


def esc(s):
    return html.escape(str(s if s is not None else ""))


def first_shot(raw):
    """screenshots 是 JSON 数组字符串；取第一张，取不到返回空串。"""
    try:
        arr = json.loads(raw or "[]")
        return arr[0] if isinstance(arr, list) and arr else ""
    except (ValueError, TypeError):
        return ""


# ── CARD-01：新品 / 回榜分组 ────────────────────────────────────────
# 阈值是产品约定：≤30 天算新上架，>90 天算老产品回榜，中间不标。
# 注意「新上架」≠ 项目定义的「新游（首次进榜）」，页面上必须把两者说清。
NEW_AGE, RETURN_AGE = 30, 90

# 商店原文/译文的展示上限。公开页面只放前 N 字，完整内容回链 App Store：
# 一是整段转载商店文案不合适（文案版权归各开发者与 Apple），
# 二是一屏读不完 —— 读者要的是「这游戏怎么玩」，不是全文。
PREVIEW_CHARS = 320

# 日报页脚口径（index / yesterday 共用单一来源，杜绝两页措辞漂移）。
# 生成口径：玩法/玩法类型/译名由模型基于已入库的商店介绍文本生成，不写
# 「没有逐条人工核对」这类自我拆台的话。
# RETURN_AGE 在 render 末尾的 footer 处 .format() 注入。
DAILY_CAVEAT = (
    "榜单每 12 小时抓取名次。「进榜」指第一次出现在该市场 Top100；"
    "上架超过 {RETURN_AGE} 天的老游戏重新进榜，标为回榜。"
    "玩法、玩法类型与译名由模型基于商店介绍生成。"
)

# 页脚第二行：交代第三方内容的出处与边界。页面只放商店文案的前 PREVIEW_CHARS 字，
# 完整内容与截图都回链 App Store，版权归各开发者与 Apple。
SOURCE_NOTE = ("商店介绍与截图版权归各开发者与 Apple 所有，本页仅作榜单研究的引用；"
               "介绍只截取前 {PREVIEW} 字，完整内容与截图请到 App Store 查看。")

# 页脚回链仓库：从 Pages 分享链接进来的人，不该只能靠猜找到源码。
REPO_URL = "https://github.com/han-wangze/game-radar"

GROUPS = [
    ("new", "新上架（≤30 天）"),
    ("mid", "上架 31–90 天"),
    ("return", "上架 >90 天 · 回榜"),
    ("unknown", "上架天数未知"),
]


def age_group(age):
    if age is None or age < 0:
        return "unknown"
    if age <= NEW_AGE:
        return "new"
    if age > RETURN_AGE:
        return "return"
    return "mid"


def age_text(age) -> str:
    """把上架天数写成人能一眼读懂的量级：超过一年就说年，不让人心算 365。"""
    if age is None:
        return "上架天数未知"
    if age < 0:
        return "未上线（预注册）"
    if age >= 365:
        y = age / 365
        return f"上架 {int(round(y))} 年" if abs(y - round(y)) < 0.05 else f"上架 {y:.1f} 年"
    return f"上架 {age} 天"


def stamp_text(v) -> str:
    """把 2026-11-06T17:34:13 写成「2026-11-06 17:34」，读者只要知道数据多新。"""
    s = (v or "").replace("T", " ")
    return s[:16] if len(s) >= 16 else (s or "—")


def like_text(v) -> str:
    """玩法类型短语原样显示（值已是中文短语，无 like 后缀可剥）。"""
    return (v or "").strip()


def descr_lang(text) -> str:
    """商店描述的语种标签：日文 / 韩文 / 英文，中文返回空串。

    中文产品不标——读者一眼就能看出是中文，标了反而是噪音。
    英文单独标出来是为了和「中文译文」区分：两者都显示在同一个位置，
    不标语种的话读者分不清这是厂商写的原文还是模型译的。
    """
    s = text or ""
    if re.search(r"[\u3040-\u30ff]", s):
        return "日文"
    if re.search(r"[\uac00-\ud7af]", s):
        return "韩文"
    han = len(re.findall(r"[\u4e00-\u9fff]", s))
    lat = len(re.findall(r"[A-Za-z]", s))
    if lat >= 40 and lat > han * 2:
        return "英文"
    return ""


def summarize(play_desc, limit=40):
    """卡片上的「简介」只显示中文。

    统一中文是明确要求，所以不再用原文抽句兜底——上一版的抽句返回的是
    商店原文（英/日/韩），会让这一行又变回外语。没有中文简介时宁可留空，
    读者仍可点开看商店原文。中文由 scripts/gen_cn_blurb.py 生成并落到
    fact_tag.play_desc，人工核验过的标签也走同一字段。
    """
    if not play_desc:
        return ""
    s = play_desc.strip()
    return (s[:limit] + "…") if len(s) > limit else s


def preview(text, n: int = PREVIEW_CHARS) -> str:
    """取前 n 字，超长补省略号。换行原样保留（CSS 是 pre-wrap）。"""
    s = (text or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def store_link(cc: str, app_id: str) -> str:
    """回链 App Store 商品页。既是给读者的「看全文」入口，也是转载的出处。"""
    return (f'<a class="ext" href="https://apps.apple.com/{esc(cc)}/app/id{esc(app_id)}" '
            f'target="_blank" rel="noopener noreferrer">在 App Store 查看完整介绍 ↗</a>')


def render_ent(row, group_key):
    """一张进榜卡片：名次 / 名称（含中文译名）/ 元信息 / 玩法 / 玩法类型，可展开商店介绍。"""
    (cc, name, rank, genre, age, seller, aid, descr, shots,
     play_desc, like_of, name_cn, descr_cn) = row
    age_s = age_text(age)
    genre = genre_cn(genre)

    badge = ""
    if group_key == "new":
        badge = '<span class="badge b-new">新上架</span>'
    elif group_key == "return":
        badge = '<span class="badge b-return">回榜</span>'

    nm_cn = f'<span class="nm-cn">（{esc(name_cn)}）</span>' if name_cn else ""
    meta = " · ".join(x for x in (seller, genre, CN.get(cc, cc), age_s) if x)
    blurb = summarize(play_desc)
    play_html = (f'<span class="play"><span class="lab">玩法</span>{esc(blurb)}</span>'
                 if blurb else "")
    lk = like_text(like_of)
    like_html = (f'<span class="like"><span class="lab">{LIKE_LABEL}</span>{esc(lk)}</span>'
                 if lk else "")
    body = (f'<span class="r1"><span class="rk">#{n(rank)}</span>'
            f'<span class="nm">{esc(name)}</span>{nm_cn}{badge}</span>'
            f'<span class="meta">{esc(meta)}</span>{play_html}{like_html}')

    if not descr and not descr_cn:
        return f'<div class="ent"><div class="ent-static">{body}</div></div>'

    img = first_shot(shots)
    thumb = (f'<img src="{esc(img)}" alt="{esc(name)} 商店截图" loading="lazy" '
             f'referrerpolicy="no-referrer">') if img else ""

    # 展开层：优先给中文译文；没有译文时给原文并标出语种（不写"未翻译"这种交代）。
    # 两者都只放前 PREVIEW_CHARS 字，末尾接 App Store 链接 —— 存储的是全文，
    # 展示的是预览，读者要全文就点过去。
    lang = descr_lang(descr)
    link = store_link(cc, aid)
    if descr_cn:
        main_html = (f'<span class="orig-tag">商店介绍 · 中文译文</span>'
                     f'<div class="descr-cn">{esc(preview(descr_cn))}</div>')
        if descr:
            main_html += (f'<details class="orig"><summary>查看{lang or ""}原文</summary>'
                          f'<div class="orig-body">{esc(preview(descr))}</div></details>')
    else:
        tag = f'商店介绍 · {lang}原文' if lang else "商店介绍"
        main_html = (f'<span class="orig-tag">{tag}</span>'
                     f'<div class="descr-cn">{esc(preview(descr))}</div>')
    main_html += f'<div class="ext-row">{link}</div>'

    return (f'<details class="ent"><summary>{body}</summary>'
            f'<div class="appcard">{thumb}'
            f'<div class="txt">{main_html}</div></div></details>')


# ── 取数 ────────────────────────────────────────────────────────────
_CUT = "(SELECT MAX(snap_date) FROM fact_rank)"


def load(conn, target=None):
    """按榜日取数。

    target 指定 YYYY-MM-DD 时生成那一天的日报（「昨天」页），默认最新榜日。
    所有当日切片与窗口统计都以这个截点为准，避免昨天页混入今天的数据。
    日期经过格式校验且必须存在于库内，因此用字面量拼进 SQL 是安全的。
    """
    import re
    if target:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target):
            raise ValueError("榜日格式必须是 YYYY-MM-DD")
        if not conn.execute("SELECT 1 FROM fact_rank WHERE snap_date=? LIMIT 1",
                            (target,)).fetchone():
            raise ValueError(f"库里没有榜日 {target}")
        cut = f"'{target}'"
    else:
        cut = _CUT

    # 历史页不能看到「后来才生成」的中文简介。
    # tagged_at IS NULL 只放行无时间戳的历史标签（descr_cn 110 条、like_of 239 条，
    # 都是 tagged_at 机制之前写入的封闭集合；现有写入口一律写非 NULL，不会新增）。
    # 原来的 `date(tagged_at) <= cut` 会让 SQL 的 NULL 比较为假，把这些标签整批挡掉，
    # 日韩产品在昨天页只剩原文、连「查看原文」折叠层都不生成（见 render_ent）。
    # 未来标签仍由 date(tagged_at) <= cut 分支挡住；标签子查询挂在 entry_date = cut 的
    # 进榜产品上，NULL 标签也不会串到别的榜日。
    tag_cond = (f"AND (tagged_at IS NULL OR date(tagged_at) <= {cut})"
                if target else "")

    def S(sql):
        return sql.replace(_CUT, cut).replace("/*TAGCUT*/", tag_cond)

    d = {}
    d["latest"] = target or conn.execute(
        "SELECT MAX(snap_date) FROM fact_rank").fetchone()[0]
    d["is_current"] = (target is None or target == conn.execute(
        "SELECT MAX(snap_date) FROM fact_rank").fetchone()[0])
    d["first"] = conn.execute(
        f"SELECT MIN(snap_date) FROM fact_rank WHERE snap_date <= {cut}").fetchone()[0]
    d["days"] = conn.execute(
        f"SELECT COUNT(DISTINCT snap_date) FROM fact_rank WHERE snap_date <= {cut}").fetchone()[0]
    d["rows"] = conn.execute(
        f"SELECT COUNT(*) FROM fact_rank WHERE snap_date <= {cut}").fetchone()[0]
    # 产品库是累计维度，不按榜日截；页脚已单独标注「产品库总数」
    d["apps"] = conn.execute("SELECT COUNT(*) FROM dim_app").fetchone()[0]
    d["pairs"] = conn.execute("SELECT COUNT(*) FROM dim_app_country").fetchone()[0]
    d["apps_in"] = conn.execute(
        f"SELECT COUNT(DISTINCT app_id) FROM fact_rank WHERE snap_date <= {cut}").fetchone()[0]
    d["pairs_in"] = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT DISTINCT country, app_id FROM fact_rank "
        f"WHERE snap_date <= {cut})").fetchone()[0]
    d["stamp"] = conn.execute(S(
        "SELECT MAX(run_at) FROM job_log WHERE snap_date = "
        "(SELECT MAX(snap_date) FROM fact_rank)")).fetchone()[0] or "—"
    d["unc"] = conn.execute(
        f"SELECT COUNT(*) FROM v_rank_sub WHERE sub_genre='Unclassified' "
        f"AND snap_date <= {cut}").fetchone()[0]
    d["new_total"] = conn.execute(
        f"SELECT COUNT(*) FROM v_new_entrant WHERE entry_date <= {cut}").fetchone()[0]
    d["new_apps"] = conn.execute(
        f"SELECT COUNT(DISTINCT app_id) FROM v_new_entrant "
        f"WHERE entry_date <= {cut}").fetchone()[0]
    # 默认页显示当前库全部标签；历史页只算当天之前打的标签。
    # 与上面的 tag_cond 同口径：NULL 视为无时间戳的历史标签，一并计入。
    tag_filter = (f" WHERE (tagged_at IS NULL OR date(tagged_at) <= {cut})"
                  if target else "")
    d["tag_rows"] = conn.execute(
        f"SELECT COUNT(*) FROM fact_tag{tag_filter}").fetchone()[0]
    d["cn_today"] = conn.execute(S("""
        SELECT COUNT(*) FROM fact_tag t
         WHERE t.tag_key='play_desc' /*TAGCUT*/ AND t.app_id IN (
               SELECT app_id FROM v_new_entrant
                WHERE entry_date=(SELECT MAX(snap_date) FROM fact_rank))""")).fetchone()[0]

    # 缺日（按国家×榜造日历反证）
    days = [r[0] for r in conn.execute(
        f"SELECT DISTINCT snap_date FROM fact_rank WHERE snap_date <= {cut} "
        f"ORDER BY snap_date")]
    dims = [(r[0], r[1]) for r in conn.execute(
        "SELECT DISTINCT country, chart FROM fact_rank")]
    have = {(r[0], r[1], r[2]) for r in conn.execute(
        f"SELECT snap_date, country, chart FROM fact_rank WHERE snap_date <= {cut} "
        f"GROUP BY 1,2,3")}
    from datetime import date, timedelta
    miss = 0
    gap_dates = []
    d0, d1 = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    cur = d0
    while cur <= d1:
        lacks = [1 for cc, ch in dims if (cur.isoformat(), cc, ch) not in have]
        if lacks:
            miss += len(lacks)
            gap_dates.append(cur.isoformat())
        cur += timedelta(days=1)
    d["missing"] = miss
    d["gap_dates"] = gap_dates

    # 当日首次进榜（带该店面的商店描述、首张截图与中文简介）
    d["entrants"] = conn.execute(S("""
        SELECT n.country, n.name, n.entry_rank, v.sub_genre, n.age_days,
               COALESCE(NULLIF(TRIM(n.seller), ''), '') AS seller,
               n.app_id,
               COALESCE(NULLIF(TRIM(ac.descr), ''), '') AS descr,
               ac.screenshots,
               (SELECT value FROM fact_tag
                 WHERE app_id = n.app_id AND tag_key = 'play_desc' /*TAGCUT*/) AS play_desc,
               (SELECT value FROM fact_tag
                 WHERE app_id = n.app_id AND tag_key = 'like_of' /*TAGCUT*/) AS like_of,
               (SELECT value FROM fact_tag
                 WHERE app_id = n.app_id AND tag_key = 'name_cn' /*TAGCUT*/) AS name_cn,
               (SELECT value FROM fact_tag
                 WHERE app_id = n.app_id AND tag_key = 'descr_cn' /*TAGCUT*/) AS descr_cn
          FROM v_new_entrant n
          LEFT JOIN v_rank_sub v
                 ON v.snap_date = n.entry_date
                AND v.country   = n.country
                AND v.app_id    = n.app_id
          LEFT JOIN dim_app_country ac
                 ON ac.app_id = n.app_id AND ac.country = n.country
         WHERE n.entry_date = (SELECT MAX(snap_date) FROM fact_rank)
         ORDER BY n.country, n.entry_rank""")).fetchall()

    # 相邻日榜位变动
    # 原来这里查「榜位日变动」（四国各多少款升/平/降），页面已不再展示：
    # 那是全量榜单的换手率，不是新品信息，读者看完不知道自己该做什么。
    # 02 段改成逐条跟进「昨天进的那几款今天怎么样」，不需要这个聚合。

    # 榜位份额（各市场占位前 5）。累计评价份额已从页面移除：
    # 它与上架时长共线，放在新游页里会被误读成「规模」。
    d["genres"] = conn.execute(S("""
        WITH t AS (
            SELECT country, COUNT(*) AS n_slots
              FROM v_rank_sub
             WHERE snap_date = (SELECT MAX(snap_date) FROM fact_rank)
             GROUP BY country
        ), g AS (
            SELECT v.country, v.sub_genre, COUNT(*) AS apps
              FROM v_rank_sub v
             WHERE v.snap_date = (SELECT MAX(snap_date) FROM fact_rank)
               AND v.sub_genre <> 'Unclassified'
             GROUP BY v.country, v.sub_genre
        ), r AS (
            SELECT g.*, ROW_NUMBER() OVER (PARTITION BY country ORDER BY apps DESC) AS rn
              FROM g
        )
        SELECT r.country, r.sub_genre, r.apps, 1.0 * r.apps / t.n_slots AS occ
          FROM r JOIN t ON t.country = r.country
         WHERE r.rn <= 5
         ORDER BY r.country, r.apps DESC""")).fetchall()

    # 年龄结构
    d["ages"] = conn.execute(S("""
        SELECT country,
               SUM(age_days BETWEEN 0 AND 30)  AS b1,
               SUM(age_days BETWEEN 31 AND 90) AS b2,
               SUM(age_days BETWEEN 91 AND 365) AS b3,
               SUM(age_days > 365)             AS b4,
               COUNT(*)                        AS tot
          FROM v_rank_sub
         WHERE snap_date = (SELECT MAX(snap_date) FROM fact_rank)
           AND age_days >= 0
         GROUP BY country""")).fetchall()

    # 上一个榜日进榜的产品，在今天的状态。逐条列，不汇总成率——
    # 汇总率没有对照物（一周 cohort、观察窗长短不一），读者拿它比不了任何东西。
    # 这里只回答「我昨天看到的那几款，今天怎么样了」。
    d["follow"] = conn.execute(f"""
        WITH pd AS (
            SELECT MAX(snap_date) AS d FROM fact_rank WHERE snap_date < {cut}
        )
        SELECT n.country, n.name, n.entry_rank,
               (SELECT value FROM fact_tag
                 WHERE app_id = n.app_id AND tag_key = 'name_cn') AS name_cn,
               pr.rank AS rk_prev, cr.rank AS rk_now
          FROM v_new_entrant n, pd
          LEFT JOIN v_rank_sub pr
                 ON pr.snap_date = pd.d AND pr.country = n.country
                AND pr.app_id = n.app_id
          LEFT JOIN v_rank_sub cr
                 ON cr.snap_date = {cut}
                AND cr.country = n.country AND cr.app_id = n.app_id
         WHERE n.entry_date = pd.d
         ORDER BY (cr.rank IS NULL), cr.rank, n.entry_rank
    """).fetchall()
    d["prev_day"] = conn.execute(
        f"SELECT MAX(snap_date) FROM fact_rank WHERE snap_date < {cut}").fetchone()[0]
    return d


# ── 渲染片段 ────────────────────────────────────────────────────────
CSS = """
:root{
--bg:#f4f6fa;--surface:#fff;--soft:#f8fafc;
--ink:#0f172a;--ink-2:#334155;--muted:#64748b;--faint:#94a3b8;
--line:#e6eaf1;--line-2:#d5dce7;
--accent:#2f5bea;--accent-ink:#1e40af;--accent-soft:#eef3ff;
--new-ink:#046c4e;--new-bg:#e7f7f0;--new-line:#b7e4d0;
--ret-ink:#9a5b06;--ret-bg:#fdf4e3;--ret-line:#f0d9a8;
--ok:#047857;--bad:#b42318;
--r-sm:8px;--r:12px;--r-lg:16px;--sh:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.03);
--sh-hover:0 10px 24px -12px rgba(16,24,40,.16);
--font:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB",
"Microsoft YaHei","Noto Sans CJK SC",sans-serif;
--mono:ui-monospace,SFMono-Regular,"SF Mono",Consolas,"Liberation Mono",monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14.5px/1.7 var(--font);
-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1060px;margin:0 auto;padding:0 24px 72px}
/* ── 顶栏 ───────────────────────────────────────────── */
.top{display:flex;align-items:center;justify-content:space-between;gap:16px;
padding:16px 0;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:9px;font-size:12.5px;font-weight:700;
letter-spacing:.09em;text-transform:uppercase}
.brand i{width:18px;height:18px;border-radius:50%;position:relative;flex:none;
background:conic-gradient(from 220deg,var(--accent),#8aa4ff,var(--accent));
box-shadow:inset 0 0 0 1px rgba(47,91,234,.22)}
.brand i::after{content:"";position:absolute;inset:4.5px;border-radius:50%;background:var(--bg)}
.brand em{font-style:normal;color:var(--faint);font-weight:500;letter-spacing:.02em;
text-transform:none;margin-left:-3px}
.nav{display:flex;gap:6px;flex-wrap:wrap}
.nav a,.nav b{font-size:12px;padding:6px 11px;border-radius:999px;line-height:1;
font-weight:550;white-space:nowrap}
.nav a{color:var(--ink-2);background:var(--surface);border:1px solid var(--line)}
.nav a:hover{border-color:var(--line-2);text-decoration:none}
.nav b{color:#fff;background:var(--ink);border:1px solid var(--ink);font-weight:550}
/* ── 页首 ───────────────────────────────────────────── */
.hero{padding:34px 0 0}
.eyebrow{font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--accent);
font-weight:700;margin-bottom:9px}
h1{font-size:30px;line-height:1.2;letter-spacing:-.025em;margin:0 0 9px;font-weight:750}
.deck{font-size:13.5px;color:var(--muted);max-width:800px;margin:0;line-height:1.65}
.deck b{color:var(--ink-2);font-weight:600}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:15px}
.chip{font-size:11.5px;color:var(--ink-2);background:var(--surface);border:1px solid var(--line);
border-radius:999px;padding:4px 10px;display:inline-flex;gap:6px;align-items:baseline;line-height:1.45}
.chip b{font-weight:650;font-variant-numeric:tabular-nums}
.chip code{font-family:var(--mono);font-size:10.5px;background:none;border:0;padding:0;
color:var(--muted)}
/* ── 指标卡 ─────────────────────────────────────────── */
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:22px 0 0}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
padding:15px 17px 13px;box-shadow:var(--sh)}
.stat .k{font-size:11.5px;color:var(--muted)}
.stat .v{font-size:26px;font-weight:700;letter-spacing:-.02em;line-height:1.2;
margin-top:5px;font-variant-numeric:tabular-nums}
.stat .h{font-size:10.5px;color:var(--faint);margin-top:3px;line-height:1.5}
/* ── 板块标题 ───────────────────────────────────────── */
.sec{margin-top:40px}
.sec-h{display:flex;align-items:center;gap:9px;margin:0 0 3px}
.sec-h .idx{font-family:var(--mono);font-size:10.5px;color:var(--accent);
background:var(--accent-soft);border:1px solid #dbe5ff;border-radius:6px;
padding:2px 6px;font-weight:650;line-height:1.3;flex:none}
.sec-h h2{font-size:18.5px;line-height:1.4;margin:0;letter-spacing:-.012em;font-weight:680}
.sec-d{font-size:12.5px;color:var(--muted);margin:1px 0 14px;max-width:860px;line-height:1.65}
.sec-d b{color:var(--ink-2);font-weight:600}
/* ── 进榜卡片 ───────────────────────────────────────── */
.grp{display:flex;align-items:center;gap:8px;margin:18px 0 8px;font-size:12px;
font-weight:650;color:var(--ink-2)}
.grp .cnt{font-weight:500;color:var(--faint);font-size:10.5px;background:var(--surface);
border:1px solid var(--line);border-radius:999px;padding:1px 7px}
.grp-fold{margin:18px 0 8px}
.grp-fold>summary.grp{cursor:pointer;list-style:none;margin:0}
.grp-fold>summary.grp::-webkit-details-marker{display:none}
.grp-fold>summary.grp::before{content:"▸";color:var(--faint);font-size:10px;margin-right:1px}
.grp-fold[open]>summary.grp::before{content:"▾"}
.grp-fold>summary.grp .cnt::after{content:" · 展开";color:var(--faint)}
.grp-fold[open]>summary.grp .cnt::after{content:" · 收起"}
.entrants{display:grid;gap:8px}
.ent-static{padding:13px 16px 12px}
.ent{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
box-shadow:var(--sh);transition:border-color .16s,box-shadow .16s}
.ent:hover{border-color:var(--line-2);box-shadow:var(--sh-hover)}
.ent>summary{list-style:none;cursor:pointer;padding:13px 16px 12px;display:block;position:relative}
.ent>summary::-webkit-details-marker{display:none}
.ent>summary::after{content:"＋";position:absolute;right:14px;top:11px;color:var(--faint);
font-size:12px;font-weight:400;line-height:1}
.ent[open]>summary::after{content:"－"}
.r1{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding-right:22px}
.rk{font-family:var(--mono);font-size:11px;font-weight:600;color:var(--muted);
background:var(--soft);border:1px solid var(--line);border-radius:6px;padding:2px 5px;
min-width:33px;text-align:center;flex:none;line-height:1.35}
.nm{font-size:16px;font-weight:680;line-height:1.35;letter-spacing:-.008em;word-break:break-word}
.nm-cn{font-size:12.5px;font-weight:450;color:var(--muted);letter-spacing:0}
.badge{font-size:10.5px;padding:2px 8px;border-radius:999px;font-weight:600;
white-space:nowrap;line-height:1.45}
.b-new{background:var(--new-bg);color:var(--new-ink);border:1px solid var(--new-line)}
.b-return{background:var(--ret-bg);color:var(--ret-ink);border:1px solid var(--ret-line)}
.meta{display:block;font-size:11.5px;color:var(--muted);margin-top:5px;line-height:1.55;
word-break:break-word}
.meta b{color:var(--ink-2);font-weight:550}
.play{display:block;font-size:13.5px;color:var(--ink-2);margin-top:7px;line-height:1.6}
.play .lab,.like .lab{display:inline-block;font-size:10px;font-weight:650;letter-spacing:.05em;
padding:1px 6px;border-radius:5px;margin-right:7px;vertical-align:1.5px;line-height:1.6}
.play .lab{color:var(--accent-ink);background:var(--accent-soft)}
.like{display:block;font-size:12px;color:var(--ink-2);margin-top:5px}
.like .lab{color:var(--ink-2);background:var(--soft);border:1px solid var(--line-2)}
.appcard{display:flex;flex-wrap:wrap;gap:14px;margin:0 16px 14px;padding-top:12px;
border-top:1px dashed var(--line);align-items:flex-start}
.appcard img{width:74px;height:132px;object-fit:cover;object-position:top;border-radius:8px;
border:1px solid var(--line);flex:none;background:#fff}
.appcard .txt{flex:1 1 300px;min-width:0;font-size:12.5px;line-height:1.75;color:var(--ink-2)}
.orig-tag{display:block;font-size:10px;color:var(--faint);margin:0 0 5px;
font-weight:650;letter-spacing:.05em}
.descr-cn{white-space:pre-wrap;word-break:break-word;padding-right:4px}
.ext-row{margin-top:9px}
a.ext{font-size:11.5px;color:var(--accent-ink);text-decoration:none;
border-bottom:1px solid var(--accent-soft);display:inline-block}
a.ext:hover{border-bottom-color:var(--accent-ink)}
details.orig{margin-top:8px}
details.orig>summary{cursor:pointer;list-style:none;font-size:11px;color:var(--muted);
display:inline-flex;align-items:center;gap:4px}
details.orig>summary::-webkit-details-marker{display:none}
details.orig>summary::before{content:"▸";font-size:9px;color:var(--faint)}
details.orig[open]>summary::before{content:"▾"}
.orig-body{margin-top:6px;padding:9px 11px;background:var(--soft);border:1px solid var(--line);
border-radius:var(--r-sm);max-height:260px;overflow:auto;white-space:pre-wrap;
word-break:break-word;font-size:11.5px;color:var(--muted);line-height:1.7}
/* ── 品类结构 ───────────────────────────────────────── */
.mkt{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
padding:14px 16px 10px;margin-bottom:10px;box-shadow:var(--sh)}
.mkt .name{font-size:12.5px;font-weight:650;margin-bottom:11px;display:flex;
justify-content:space-between;align-items:baseline;gap:10px}
.mkt .name span{font-weight:400;color:var(--faint);font-size:10.5px}
.row{display:grid;grid-template-columns:112px 1fr 92px;gap:10px;align-items:center;
font-size:12px;margin-bottom:7px}
.row .lab{color:var(--ink-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.track{height:7px;background:var(--soft);border-radius:4px;overflow:hidden}
.track i{display:block;height:100%;border-radius:4px;
background:linear-gradient(90deg,#6b8bff,var(--accent))}
.row .val{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums;font-size:11px}
/* ── 跟进清单（上次进榜产品的名次变化） ──────────── */
.fu{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
box-shadow:var(--sh);overflow:hidden}
.fu-row{display:grid;grid-template-columns:52px minmax(0,1fr) 112px 52px;gap:10px;
align-items:center;padding:10px 15px;border-bottom:1px solid var(--line);font-size:12.5px}
.fu-row:last-child{border-bottom:0}
.fu-mkt{font-size:11px;color:var(--muted)}
.fu-nm{font-weight:600;word-break:break-word;line-height:1.4}
.fu-nm .cn{font-size:11px;font-weight:400;color:var(--muted)}
.fu-rk{font-family:var(--mono);font-size:11px;color:var(--muted);
font-variant-numeric:tabular-nums;white-space:nowrap;text-align:right}
.fu-rk b{color:var(--ink);font-weight:700}
.fu-d{font-size:10.5px;font-weight:650;text-align:center;white-space:nowrap;
border-radius:999px;padding:2px 0}
.fu-d.up{color:#b42318;background:#fef3f2;border:1px solid #fecdca}
.fu-d.down{color:#0f7b6c;background:#ecfdf3;border:1px solid #a6f4c5}
.fu-d.flat,.fu-d.off{color:var(--faint);background:var(--soft);border:1px solid var(--line)}
/* ── 两列网格（榜单构成） ──────────────────────────── */
.env{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.env .mkt{margin-bottom:0}
@media(max-width:720px){ .env{grid-template-columns:1fr} }
@media(max-width:520px){
 .fu-row{grid-template-columns:minmax(0,1fr) 52px;gap:3px 10px;padding:10px 13px}
 .fu-mkt{grid-column:1;grid-row:1;font-size:10.5px}
 .fu-nm{grid-column:1;grid-row:2;font-size:13px}
 .fu-rk{grid-column:1;grid-row:3;text-align:left}
 .fu-d{grid-column:2;grid-row:1 / span 3;align-self:center}
}
/* ── 留存 / 年龄 ────────────────────────────────────── */
.stack{display:flex;height:9px;border-radius:5px;overflow:hidden;background:var(--soft)}
.stack i{display:block;height:100%}
.stacklab{display:flex;gap:13px;font-size:10.5px;color:var(--muted);margin-top:8px;
flex-wrap:wrap;font-variant-numeric:tabular-nums}
.stacklab span{display:inline-flex;align-items:center;gap:5px}
.stacklab i{width:7px;height:7px;border-radius:2px;display:inline-block;flex:none}
/* ── 口径 / 页脚 ────────────────────────────────────── */
code{font-family:var(--mono);font-size:11px;background:var(--soft);border:1px solid var(--line);
border-radius:4px;padding:1px 5px}
.ok{color:var(--ok)}.bad{color:var(--bad)}
.lim-h{font-size:12.5px;font-weight:680;color:var(--ink);margin:20px 0 0;
padding-left:9px;border-left:2px solid var(--accent);line-height:1.5}
footer{margin-top:44px;padding-top:15px;border-top:1px solid var(--line);
color:var(--faint);font-size:11.5px;line-height:1.75}
.foot-note{font-size:12px;color:var(--muted);line-height:1.75;margin:0 0 7px}
/* ── 响应式 ─────────────────────────────────────────── */
@media(max-width:820px){
 .wrap{padding:0 16px 56px}
 h1{font-size:24px}
 .row{grid-template-columns:90px 1fr 78px}
}
@media(max-width:720px){
 /* 顶栏必须换行：品牌 + 三个导航胶囊在 390px 会撑破页面，造成整页横向溢出 */
 .top{flex-wrap:wrap;row-gap:10px;padding:13px 0}
 .brand{flex:0 0 auto}
 .brand em{display:none}
 .nav{flex:1 1 100%;min-width:0}
 .nav a,.nav b{font-size:11.5px;padding:5px 9px}
}
@media(max-width:620px){
}
@media(max-width:520px){
 .stats{grid-template-columns:repeat(3,1fr);gap:8px}
 .stat{padding:11px 12px 10px}
 .stat .v{font-size:20px}
 .stat .h{display:none}
 .nm{font-size:15px}
 .appcard img{width:62px;height:111px}
}
"""


def render(d, nav="") -> str:
    latest, first, days = d["latest"], d["first"], d["days"]
    parts = []
    A = parts.append

    A(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>四国新游 · {esc(latest)}</title><style>{CSS}</style></head><body><div class="wrap">""")

    nav_html = f'<div class="nav">{nav}</div>' if nav else ""
    A(f"""<div class="top"><div class="brand"><i></i>game-radar <em>新游雷达</em></div>
{nav_html}</div>
<div class="hero"><div class="eyebrow">App Store 免费游戏榜 · 美国 / 中国 / 日本 / 韩国</div>
<h1>四国新游</h1>
<p class="deck">四个市场 <b>Top100</b> 中第一次进榜的产品：
玩法、玩法类型与进榜名次。</p>
<div class="chips">
<span class="chip">快照 <b>{esc(stamp_text(d['stamp']))}</b> · 榜日 <b>{esc(latest)}</b></span>
<span class="chip">每 12 小时更新</span>
</div></div>""")

    # 首屏三卡只回答读者进门会问的三件事：今天进了几个、其中有几个是真新游戏、
    # 打得进去吗。窗口天数、缺日这类排障数字挪到「关于数据」，不占首屏。
    n_ent = len(d["entrants"])
    n_new_top = sum(1 for r in d["entrants"]
                    if r[4] is not None and 0 <= r[4] <= NEW_AGE)
    ranks = sorted(r[2] for r in d["entrants"])
    best = ranks[0] if ranks else None
    rest = ranks[1:]
    if not rest:
        best_hint = "仅此一款" if ranks else "无进榜"
    elif rest[0] == rest[-1]:
        best_hint = f'其余 {n(len(rest))} 款都在 #{n(rest[0])}'
    else:
        best_hint = f'其余 {n(len(rest))} 款在 #{n(rest[0])}–#{n(rest[-1])} 之间'
    A(f"""<div class="stats">
<div class="stat"><div class="k">首次进榜</div>
<div class="v">{n_ent} 款</div>
<div class="h">四国合计 · {esc(latest)}</div></div>
<div class="stat"><div class="k">其中全新上架</div>
<div class="v">{n_new_top} 款</div>
<div class="h">上线 ≤{NEW_AGE} 天 · 其余 {n_ent - n_new_top} 款是老游戏回归或翻新</div></div>
<div class="stat"><div class="k">最好位次</div>
<div class="v">{('#' + n(best)) if best else '—'}</div>
<div class="h">{best_hint}</div></div>
</div>""")

    # 1. 最新榜日进榜（按上架天数分组；卡片常显 玩法 / 类似）
    A('<section class="sec"><div class="sec-h"><span class="idx">01</span>'
      '<h2>首次进榜</h2></div>')
    if d["entrants"]:
        by_group: dict[str, list] = {}
        for row in d["entrants"]:
            by_group.setdefault(age_group(row[4]), []).append(row)
        A('<p class="sec-d">按上架天数分组，<b>新上架</b>（≤30 天）排在前面；'
          '点开任意一款可看商店介绍与截图。</p>')
        for key, label in GROUPS:
            rows = by_group.get(key)
            if not rows:
                continue
            # 组内按位次升序：读者最先该看到的是「打进得最深」的那款。
            # 查询里是按国家排的，直接渲染会像是随机顺序（国家已在元信息行里）。
            rows.sort(key=lambda r: r[2])
            # 全部默认展开。曾按「只有真新品展开」折叠其余组，但遇到
            # 「1 款新上架 + 8 款回榜」的日子会把 8/9 的内容藏起来，台账首页反而空。
            # 组标题仍可点击收起，读者自己决定跳过哪组。
            folded = True
            if folded:
                A('<details class="grp-fold" open>')
                A(f'<summary class="grp">{label}'
                  f'<span class="cnt">{len(rows)} 款</span></summary>')
            else:
                A(f'<div class="grp">{label}<span class="cnt">{len(rows)} 款</span></div>')
            A('<div class="entrants">')
            for row in rows:
                A(render_ent(row, key))
            A('</div>')
            if folded:
                A('</details>')

        # 分组口径、字段来源都不在这里解释：全部搬到末段「数据来源与口径」。
        # 之前这里挂着一大段折叠说明，正文里再没有别的口径，读者要读到 01 才知道
        # 「新上架 ≠ 新游」，位置太靠后，而且和末段说法会各自漂移。
    else:
        A('<p class="sec-d">这一榜日没有新的首次进榜产品。</p>')
    A('</section>')

    # 2. 昨天进榜的那批，今天怎么样
    # 原来这里是「榜位日变动」：按国家统计 4 个榜上全量产品的 升/平/降 条数。
    # 那是市场换手率，不是新品信息——读者看完不知道跟自己有什么关系。
    # 换成逐条的跟进：昨天发现的那几款，今天升了、掉了、还是没了。
    # 标题只说这段是什么数据，不设问、不替读者解读；时间由 chip 和引导语承担。
    h2 = "上次进榜产品的名次变化"
    A(f'<section class="sec"><div class="sec-h"><span class="idx">02</span>'
      f'<h2>{h2}</h2></div>')
    fu = d["follow"]
    if fu:
        n_off = sum(1 for r in fu if r[5] is None)
        A(f'<p class="sec-d">{esc(d["prev_day"])} 首次进榜的 <b>{len(fu)}</b> 款：'
          f'仍在榜 <b>{len(fu) - n_off}</b> 款，已掉出 <b>{n_off}</b> 款。</p>')
        A('<div class="fu">')
        for cc, name, er, cn, rk_p, rk_n in fu:
            nm = esc(name) + (f'<span class="cn">（{esc(cn)}）</span>' if cn else "")
            if rk_n is None:
                rk_html = (f'<span class="fu-rk">#{n(rk_p or er)}</span>'
                           f'<span class="fu-d off">已掉出</span>')
            else:
                p = rk_p or er
                delta = p - rk_n
                if delta > 0:
                    cls, txt = "up", f'↑{n(delta)}'
                elif delta < 0:
                    cls, txt = "down", f'↓{n(-delta)}'
                else:
                    cls, txt = "flat", "持平"
                rk_html = (f'<span class="fu-rk">#{n(p)} → <b>#{n(rk_n)}</b></span>'
                           f'<span class="fu-d {cls}">{txt}</span>')
            A(f'<div class="fu-row"><span class="fu-mkt">{esc(CN.get(cc, cc))}</span>'
              f'<span class="fu-nm">{nm}</span>{rk_html}</div>')
        A('</div>')
    else:
        A('<p class="sec-d">上一榜日没有首次进榜的产品。</p>')
    A('</section>')

    # 3. 榜单构成（年龄 + 品类合到一段，作为「新品要和谁抢位子」的背景）
    # 原来这是两段（03 品类结构 / 05 年龄结构），中间还夹着已删除的留榜段。
    # 合并的理由：两者回答的是同一个问题——这 100 个位子现在被谁占着。
    A('<section class="sec"><div class="sec-h"><span class="idx">03</span>'
      '<h2>当前榜单构成</h2></div>')
    A('<p class="sec-d">每个市场 100 个席位，下面按上架时长与品类分别统计在榜产品。'
      '条形按占比线性绘制（满格 = 100%）：上架时长图四档合计 100 席，'
      '品类图为该品类占的席位数。</p>')
    age_colors = ["#2f5bea", "#0ea5a4", "#f59e0b", "#cbd5e1"]
    age_by = {r[0]: r for r in d["ages"]}
    # 两段网格都是 4 张同款卡片（美国/中国/日本/韩国 各一遍），
    # 不加小标题读者会以为是同一份数据的重复渲染。
    A('<h3 class="lim-h">上架时长分布</h3>')
    A('<div class="env" style="margin-top:10px">')
    for cc in ORDER:
        r = age_by.get(cc)
        if not r:
            continue
        b1, b2, b3, b4, tot_raw = r[1], r[2], r[3], r[4], r[5]
        tot = tot_raw or 1
        seg = [(b1, age_colors[0]), (b2, age_colors[1]),
               (b3, age_colors[2]), (b4, age_colors[3])]
        bar = "".join(f'<i style="width:{max((v or 0) * 100 / tot, 0):.1f}%;'
                      f'background:{c}"></i>' for v, c in seg)
        A(f'<div class="mkt"><div class="name">{esc(CN[cc])}'
          f'<span>上架时长 · 共 {n(tot_raw)} 款</span></div>'
          f'<div class="stack">{bar}</div>'
          f'<div class="stacklab">'
          f'<span><i style="background:{age_colors[0]}"></i>0–30 天 {pct((b1 or 0)/tot,0)}</span>'
          f'<span><i style="background:{age_colors[1]}"></i>31–90 天 {pct((b2 or 0)/tot,0)}</span>'
          f'<span><i style="background:{age_colors[2]}"></i>91–365 天 {pct((b3 or 0)/tot,0)}</span>'
          f'<span><i style="background:{age_colors[3]}"></i>365+ 天 {pct((b4 or 0)/tot,0)}</span>'
          f'</div></div>')
    A('</div>')

    by_cc: dict = {}
    for cc, g, apps, occ in d["genres"]:
        by_cc.setdefault(cc, []).append((g, apps, occ or 0))
    A('<h3 class="lim-h">品类席位 · 前 5</h3>')
    A('<div class="env" style="margin-top:10px">')
    for cc in ORDER:
        rows = by_cc.get(cc, [])
        if not rows:
            continue
        A(f'<div class="mkt"><div class="name">{esc(CN[cc])}'
          f'<span>品类 · 前 5 位</span></div>')
        for g, apps, occ in rows:
            A(f'<div class="row"><div class="lab">{esc(genre_cn(g))}</div>'
              f'<div class="track"><i style="width:{max(occ*100,1):.1f}%"></i></div>'
              f'<div class="val">{apps} 席 · {pct(occ,0)}</div></div>')
        A('</div>')
    A('</div>')
    A('</section>')

    # 4. 口径不再单列 04 板块，压成页脚两三行小字（读者要的是「今天谁进来了」，
    #    口径是脚注不是章节）。文案来自 DAILY_CAVEAT 单一来源，两页共用。
    caveat = DAILY_CAVEAT.format(RETURN_AGE=RETURN_AGE)
    note = SOURCE_NOTE.format(PREVIEW=PREVIEW_CHARS)
    # 缺日必须写在页面上：面板连续 N 天是卖点，但有一天没抓到也是事实，
    # 只藏在告警里等于让读者按「完整面板」去读同比数字。
    gap_note = ""
    if d["gap_dates"]:
        shown = "、".join(g[:10] for g in d["gap_dates"][:3])
        more = " 等" if len(d["gap_dates"]) > 3 else ""
        gap_note = (f'<p class="foot-note">面板共 {d["days"]} 个榜日，其中 '
                    f'{len(d["gap_dates"])} 天整日未采集（{shown}{more}）——'
                    f'Apple 不提供历史榜单，这段补不回来。</p>')
    A(f'<footer><p class="foot-note">{caveat}</p>'
      f'<p class="foot-note">{note}</p>'
      f'{gap_note}'
      f'数据来自 Apple App Store 免费游戏榜 · '
      f'本页生成于 {datetime.now().strftime("%Y-%m-%d %H:%M")} · '
      f'<a href="{REPO_URL}">项目源码</a></footer>')
    A("</div></body></html>")
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--date", default=None,
                    help="指定榜日 YYYY-MM-DD，只生成那一天的单页；默认最新榜日")
    ap.add_argument("--no-yesterday", action="store_true",
                    help="默认运行时不额外生成 yesterday.html")
    ap.add_argument("--no-weekly", action="store_true",
                    help="默认运行时不额外生成 weekly.html")
    ap.add_argument("--no-publish", action="store_true",
                    help="即使开着 .publish-on-build 也不推送到 GitHub")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"库不存在：{args.db}")
        return 1
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        latest = conn.execute("SELECT MAX(snap_date) FROM fact_rank").fetchone()[0]
        if not latest:
            print("库里还没有榜单数据")
            return 1
        prev = conn.execute(
            "SELECT MAX(snap_date) FROM fact_rank WHERE snap_date < ?",
            (latest,)).fetchone()[0]

        def write(target, out, nav):
            d = load(conn, target)
            p = Path(out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(render(d, nav), encoding="utf-8")
            print(f"已生成 {p}　（{d['latest']}，{d['days']} 个榜日，"
                  f"{len(d['entrants'])} 条当日进榜）")

        if args.date:
            write(args.date, args.out, "")
        else:
            wlink = '<a href="weekly.html">周报 →</a>'
            today_nav = (f'<a href="yesterday.html">← 昨天（{prev}）</a>　'
                         f'<b>今天（{latest}）</b>　{wlink}') if prev else wlink
            write(None, args.out, today_nav)
            # 只有写默认 index.html 时才顺带生成昨天页与周报，避免 --out 到别处时多出文件
            if (prev and not args.no_yesterday
                    and Path(args.out).resolve() == OUT_PATH.resolve()):
                y_nav = (f'<b>昨天（{prev}）</b>　'
                         f'<a href="index.html">今天（{latest}）→</a>　{wlink}')
                write(prev, ROOT / "site" / "yesterday.html", y_nav)
            if not args.no_weekly and Path(args.out).resolve() == OUT_PATH.resolve():
                import build_weekly          # 延迟导入，避免模块级互相引用
                build_weekly.generate(args.db, ROOT / "site" / "weekly.html")
    finally:
        conn.close()

    # 构建成功就把三页推到线上：本机是唯一有数据库的地方（Apple 无历史接口），
    # 线上看板只能由本机构建后推上去。开关是仓库根的 .publish-on-build（本地文件、
    # 不入库），免得每次本地试建都往公开仓库推提交；--no-publish 可临时跳过。
    # 只搬页面与 README，内容没变时 publish_site 自己会跳过，不产生空提交。
    if (not args.no_publish and not args.date
            and Path(args.out).resolve() == OUT_PATH.resolve()
            and (ROOT / ".publish-on-build").exists()):
        try:
            import publish_site            # 延迟导入：单独建页时不需要它
            print()
            publish_site.publish()
        except Exception as exc:           # 发布失败不该让建页失败
            print(f"发布跳过：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
