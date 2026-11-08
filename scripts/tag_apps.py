#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 打标工作流（v3）
------------------------------
把「取候选 → 打标 → 校验 → 入库 → 抽检」拆成可复现的步骤。
模型负责给标签，脚本负责结构与值域——换模型、换批次都不影响可追踪性。

  export    按品类分层挑候选（结果确定、可复现），导出待打标的 JSON
  validate  校验已打标的 JSON（见下方规则）
  import    校验通过后写入 fact_tag（按 (app_id, tag_key) 覆盖）
  stats     覆盖率、分布、待复核比例

校验规则（v3）：
  1. tag_key 必须在 dim_tag_def 里
  2. 闭集键（value_domain 非 NULL）→ value 必须逐字在值域内
     开集键（value_domain 为 NULL）→ 非空且不超过 max_len
  3. evidence 必填（判断不了也要写清为什么判断不了，不能留空）
  4. evidence_strength ∈ {direct, inferred, none}；不填按 inferred 处理
     · direct  → evidence 必须能在商店描述里找到（去空白后子串匹配）
     · none    → need_review 必须为 1（没有依据的标签不许被当成可用）
  5. play_desc 必填；其余键可缺（覆盖率单独报，不硬凑）
  6. confidence 可选，给了就必须在 [0,1] 内（它是自评，不对外使用）

用法：
    python scripts/tag_apps.py export --sample 10 --out tag_work/in_10.json
    python scripts/tag_apps.py validate --in tag_work/out_10.json
    python scripts/tag_apps.py import   --in tag_work/out_10.json
    python scripts/tag_apps.py stats
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "radar.db"

# 分层用的品类：覆盖这批数据里席位最多的几类，避免 10 条全是 Puzzle
STRATA = ["Puzzle", "Action", "Strategy", "Simulation", "Role Playing",
          "Sports", "Card", "Casino", "Word", "Board"]

DESCR_LIMIT = 1600
# 系列推断的典型措辞。必须用短语而不是单字：「作」「有」「代」在正常描述里到处都是，
# 用单字会把「现代都市」「动作游戏」全判成违规。
SERIES_PHRASES = ("依系列", "系列既有", "既有形象", "与前作", "前作一致", "续作",
                  "系列作品", "系列旧", "同系列", "该系列")


STRENGTHS = {"direct", "inferred", "none"}
REQUIRED_KEYS = {"play_desc"}

# validate() 会往这里放「不算错、但要知道」的事（如描述已变导致无法核对子串）。
WARNINGS: list[str] = []


def conn_ro(db=DB_PATH):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def load_defs(conn):
    out = {}
    for k, name, dom, ml, multi, ver in conn.execute(
            "SELECT tag_key, name_cn, value_domain, max_len, multi_value, ver FROM dim_tag_def"):
        out[k] = {
            "name_cn": name,
            "domain": set(json.loads(dom)) if dom else None,   # None = 开集
            "max_len": ml,
            "multi": bool(multi),
            "ver": ver,
        }
    return out


def norm(s: str) -> str:
    """归一化用于子串比对：去所有空白、全角转半角、小写。"""
    s = (s or "")
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s)
    s = s.replace("\u3000", " ")
    return re.sub(r"\s+", "", s).lower()


def cmd_export(args) -> int:
    conn = conn_ro(args.db)
    snap = conn.execute("SELECT MAX(snap_date) FROM fact_rank").fetchone()[0]

    if args.genre:
        # 按父类取全量：一个产品可能跨多国多日上榜，按 app_id 去重，取名次最好的一条作代表
        tagged = ""
        if args.skip_tagged:
            tagged = "AND v.app_id NOT IN (SELECT app_id FROM fact_tag)"
        rows = conn.execute(f"""
            WITH pick AS (
                SELECT v.country, v.app_id, v.rank, v.sub_genre, v.name, v.age_days,
                       v.rating_count,
                       ROW_NUMBER() OVER (PARTITION BY v.app_id ORDER BY v.rank) AS rn
                  FROM v_rank_sub v
                 WHERE v.snap_date = ? AND v.sub_genre = ? {tagged}
            )
            SELECT app_id, name, country, rank, sub_genre, age_days, rating_count
              FROM pick WHERE rn = 1 ORDER BY rank
        """, (snap, args.genre)).fetchall()
        if not args.all:
            rows = rows[:args.sample]
    else:
        marks = ",".join("?" * len(STRATA))
        rows = conn.execute(f"""
            WITH pick AS (
                SELECT country, app_id, rank, sub_genre, name, age_days, rating_count,
                       ROW_NUMBER() OVER (PARTITION BY sub_genre
                                          ORDER BY country, rank) AS rn
                  FROM v_rank_sub
                 WHERE snap_date = (SELECT MAX(snap_date) FROM fact_rank)
                   AND sub_genre IN ({marks})
            )
            SELECT p.app_id, p.name, p.country, p.rank, p.sub_genre, p.age_days,
                   p.rating_count
              FROM pick p
             WHERE p.rn = 1
             ORDER BY p.sub_genre
             LIMIT ?""", (*STRATA, args.sample)).fetchall()
    conn.close()

    # 描述一律取「产品 × 店面」里优先级最高的那份（DESCR_PREF），并记下它属于哪国、哪个指纹。
    didx = descr_index(args.db)
    items = []
    for aid, name, cc, rank, genre, age, rc in rows:
        src = (didx.get(aid) or {}).get("preferred") or {}
        d = (src.get("text") or "").strip().replace("\r", " ")
        items.append({
            "app_id": aid, "name": name, "country": cc, "rank": rank,
            "sub_genre": genre, "age_days": age, "rating_count": rc,
            "descr_country": src.get("country"),      # 这份描述来自哪个店面
            "descr_sha1": src.get("sha1"),            # 打标时那份文本的指纹
            "descr_len": len(d),
            "descr": d[:DESCR_LIMIT],
            "descr_truncated": len(d) > DESCR_LIMIT,
        })
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"snap_date": snap, "items": items},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出 {len(items)} 条 → {out}（榜日 {snap}）")
    for it in items:
        print(f"  {it['sub_genre']:<13} {it['country']} #{it['rank']:<3} "
              f"{it['name'][:30]:<30} descr {it['descr_len']:>4}字符 "
              f"({it['descr_country'] or '无'})")
    return 0


DESCR_PREF = ("us", "cn", "jp", "kr")   # 打标优先用英文店面，其次该产品在榜的国家


def descr_index(db=DB_PATH):
    """app_id → 该产品在各店面的描述 + 一个默认首选。

    结构：{"preferred": {text,country,sha1}, "by_country": {us: {...}, kr: {...}}}
    为什么要 by_country：**校验必须按标签自己记录的来源店面取文本**。
    历史批次用的是 kr 文本，新批次默认 us；拿 us 去校验 kr 证据会全员误报（实测 74 条）。
    """
    conn = conn_ro(db)
    rows = conn.execute(
        "SELECT app_id, country, descr, descr_sha1 FROM dim_app_country "
        "WHERE descr IS NOT NULL AND TRIM(descr) <> ''").fetchall()
    conn.close()
    rank = {c: i for i, c in enumerate(DESCR_PREF)}
    out: dict[str, dict] = {}
    for aid, cc, text, h in rows:
        rec = out.setdefault(aid, {"by_country": {}, "preferred": None})
        rec["by_country"][cc] = {"text": text, "country": cc, "sha1": h}
    for rec in out.values():
        rec["preferred"] = min(rec["by_country"].values(),
                               key=lambda v: rank.get(v["country"], 99))
    return out


def tag_source_index(db=DB_PATH):
    """(app_id, tag_key) → 库里已记录的证据来源 {country, sha1}。

    为什么校验要看库：历史批次的 JSON 里没有来源字段，但 fact_tag 已经回填过，
    只有从库里取才能按正确的店面校验（否则会拿 us 文本去核 kr 证据，全员误报）。
    """
    conn = conn_ro(db)
    out = {(r[0], r[1]): {"country": r[2], "sha1": r[3]}
           for r in conn.execute(
               "SELECT app_id, tag_key, descr_country, descr_sha1 FROM fact_tag "
               "WHERE descr_country IS NOT NULL OR descr_sha1 IS NOT NULL")}
    conn.close()
    return out


def _pick_source(src, tag: dict):
    """按标签记录的来源挑描述；挑不到返回 None（表示"无法核对"，不是"错了"）。"""
    if not isinstance(src, dict) or "by_country" not in src:
        text = src if isinstance(src, str) else ""
        return {"text": text, "country": None, "sha1": None} if text else None
    cc = tag.get("descr_country")
    if cc and cc in src["by_country"]:
        return src["by_country"][cc]
    sha = tag.get("descr_sha1")
    if sha:
        hit = [v for v in src["by_country"].values() if v.get("sha1") == sha]
        return hit[0] if hit else None
    return src.get("preferred")


def validate(payload, defs, descrs=None, tag_sources=None) -> list[str]:
    errs = []
    WARNINGS.clear()
    items = payload.get("items", [])
    if not items:
        return ["items 为空"]
    descrs = descrs or {}
    tag_sources = tag_sources or {}
    for it in items:
        aid = it.get("app_id")
        if not aid:
            errs.append("存在缺少 app_id 的条目")
            continue
        tags = it.get("tags") or []
        if not tags:
            errs.append(f"{aid}: 没有 tags")
            continue
        src = descrs.get(aid, "")
        per_key = {}
        for t in tags:
            k, v = t.get("tag_key"), (t.get("value") or "").strip()
            if k not in defs:
                errs.append(f"{aid}: 未知 tag_key={k}")
                continue
            d = defs[k]
            if d["domain"] is None:                       # 开集
                if not v:
                    errs.append(f"{aid}: {k} 为空")
                elif d["max_len"] and len(v) > d["max_len"]:
                    errs.append(f"{aid}: {k} 超长（{len(v)}>{d['max_len']}）")
            elif v not in d["domain"]:                    # 闭集
                errs.append(f"{aid}: {k} 的值 {v} 不在值域内")

            ev = (t.get("evidence") or "").strip()
            if not ev:
                errs.append(f"{aid}: {k}={v} 缺少 evidence")

            st = t.get("evidence_strength", "inferred")
            if st not in STRENGTHS:
                errs.append(f"{aid}: {k}={v} 的 evidence_strength 非法（{st}）")
            elif st == "direct" and ev:
                # payload 里没写来源时，退回库里已记录的那条（历史批次就是靠这个对上 kr 文本的）
                known = tag_sources.get((aid, k)) or {}
                pick = {"descr_country": t.get("descr_country") or known.get("country"),
                        "descr_sha1": t.get("descr_sha1") or known.get("sha1")}
                cur = _pick_source(src, pick)
                if cur is None:
                    # 文本已变 / 该店面没抓到描述：不能判「当初写错」，只提示
                    WARNINGS.append(f"{aid}: {k}={v} 找不到打标时的描述，跳过子串校验")
                else:
                    body = norm(cur["text"])
                    if body:
                        for seg in ev.split("|"):
                            if norm(seg) and norm(seg) not in body:
                                errs.append(
                                    f"{aid}: {k}={v} 标为 direct，但原文里找不到"
                                    f"「{seg.strip()[:24]}」（{cur['country'] or '?'} 店面）")
                                break
            elif st == "none" and not int(t.get("need_review", 0)):
                errs.append(f"{aid}: {k}={v} 无依据（none），必须置 need_review=1")

            # art 不得用系列旧作推断（2026-11-05 抽检：PVZ3 被系列印象判成 2D，实际已改 3D）
            # 只能这样推断时，该条必须是 unclear + none + need_review=1，不能给具体值。
            hit = next((x for x in SERIES_PHRASES if x in ev), None) if k == "art" else None
            if hit and v != "unclear":
                errs.append(f"{aid}: art={v} 的 evidence 含系列推断措辞（{hit}）"
                            f"——美术会随作品换代，只能记 unclear + none + need_review=1")

            c = t.get("confidence")
            if c is not None and not (0 <= float(c) <= 1):
                errs.append(f"{aid}: {k}={v} 的 confidence 越界（{c}）")
            per_key.setdefault(k, []).append(v)
        for k in REQUIRED_KEYS:
            if k not in per_key:
                errs.append(f"{aid}: 缺必填键 {k}")
        for k, vs in per_key.items():
            if not defs.get(k, {}).get("multi") and len(set(vs)) > 1:
                errs.append(f"{aid}: {k} 是单值标签，却给了 {vs}")
    if len({it.get("app_id") for it in items}) != len(items):
        errs.append("items 里出现重复 app_id")
    return errs


def cmd_validate(args) -> int:
    conn = conn_ro(args.db)
    defs = load_defs(conn)
    conn.close()
    if not defs:
        print("dim_tag_def 为空，先跑 scripts/seed_tag_def.py")
        return 1
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    errs = validate(payload, defs, descr_index(args.db), tag_source_index(args.db))
    if WARNINGS:
        print(f"提示 {len(WARNINGS)} 条（不算错，但要知道）：")
        for w in WARNINGS[:10]:
            print(f"  · {w}")
    if errs:
        print(f"校验失败，{len(errs)} 个问题：")
        for e in errs:
            print(f"  - {e}")
        return 1
    n_tags = sum(len(it.get("tags") or []) for it in payload["items"])
    print(f"校验通过：{len(payload['items'])} 个产品 / {n_tags} 条标签")
    return 0


def cmd_import(args) -> int:
    conn = sqlite3.connect(args.db)
    defs = load_defs(conn)
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    idx = descr_index(args.db)
    errs = validate(payload, defs, idx, tag_source_index(args.db))
    if WARNINGS:
        print(f"提示 {len(WARNINGS)} 条（不算错，但要知道）：")
        for w in WARNINGS[:10]:
            print(f"  · {w}")
    if errs:
        print(f"校验失败，拒绝入库（{len(errs)} 个问题）：")
        for e in errs[:10]:
            print(f"  - {e}")
        conn.close()
        return 1

    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for it in payload["items"]:
        src = (idx.get(it["app_id"]) or {}).get("preferred") or {}
        # 优先记「打标时」的店面与指纹（export 写下），拿不到才退回当前值
        src_cc = it.get("descr_country") or src.get("country")
        src_sha = it.get("descr_sha1") or src.get("sha1")
        for t in it["tags"]:
            conn.execute("""
                INSERT INTO fact_tag(app_id, tag_key, value, confidence, evidence_strength,
                                     method, model, tag_ver, evidence, descr_country, descr_sha1,
                                     need_review, tagged_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(app_id, tag_key) DO UPDATE SET
                    value             = excluded.value,
                    confidence        = excluded.confidence,
                    evidence_strength = excluded.evidence_strength,
                    method            = excluded.method,
                    model             = excluded.model,
                    tag_ver           = excluded.tag_ver,
                    evidence          = excluded.evidence,
                    descr_country     = COALESCE(excluded.descr_country, fact_tag.descr_country),
                    descr_sha1        = COALESCE(excluded.descr_sha1, fact_tag.descr_sha1),
                    need_review       = excluded.need_review,
                    tagged_at         = excluded.tagged_at
            """, (it["app_id"], t["tag_key"], t["value"].strip(), t.get("confidence"),
                  t.get("evidence_strength", "inferred"),
                  t.get("method", "llm"), t.get("model", "agent-manual"),
                  defs[t["tag_key"]]["ver"], t.get("evidence"),
                  src_cc, src_sha,
                  int(t.get("need_review", 0)), now))
            n += 1
    conn.commit()
    conn.close()
    print(f"已写入 {n} 条标签（{len(payload['items'])} 个产品）")
    return 0


def cmd_sample_review(args) -> int:
    """分层抽检：按 tag_key 配额抽样，need_review 与 none/inferred 优先。
    结果写成 CSV（utf-8-sig，Excel 可直接开），verdict 列留空等人填。"""
    import csv
    quota = dict(p.split("=") for p in args.quota.split(",")) if args.quota else \
        {"subgenre": 40, "theme": 20, "play_desc": 15, "like_of": 10, "art": 10}
    quota = {k: int(v) for k, v in quota.items()}

    conn = conn_ro(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT f.app_id, a.name, f.tag_key, f.value, f.evidence_strength,
               f.need_review, f.evidence, COALESCE(a.descr,'') descr
          FROM fact_tag f JOIN dim_app a USING (app_id)
         WHERE f.tag_key IN ({})
         ORDER BY f.tag_key, f.need_review DESC, f.app_id
    """.format(",".join("?" * len(quota))), tuple(quota)).fetchall()
    conn.close()

    # 组内排序：need_review 优先 → none → inferred → direct；同档按 app_id 保持可复现
    order = {"none": 0, "inferred": 1, "direct": 2}
    by = {}
    for r in rows:
        by.setdefault(r["tag_key"], []).append(r)
    picked = []
    for k, n in quota.items():
        pool = sorted(by.get(k, []), key=lambda r: (-r["need_review"], order.get(r["evidence_strength"], 1), r["app_id"]))
        picked += pool[:n]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["batch", "app_id", "name", "tag_key", "value", "evidence_strength",
                    "need_review", "evidence", "descr_excerpt", "verdict", "human_value", "note"])
        for r in picked:
            w.writerow([args.batch, r["app_id"], r["name"], r["tag_key"], r["value"],
                        r["evidence_strength"], r["need_review"], r["evidence"],
                        (r["descr"] or "")[:300].replace("\n", " "), "", "", ""])
    print(f"已生成抽检表 {len(picked)} 条 → {out}")
    for k in quota:
        n = sum(1 for r in picked if r["tag_key"] == k)
        print(f"  {k:<10}{n:>3} / 配额 {quota[k]}（池 {len(by.get(k, []))}）")
    print("\n填 verdict（correct / wrong / unsure）；判 wrong 时必须在 human_value 填正确值。")
    return 0


def cmd_import_review(args) -> int:
    """回收抽检表：校验后写入 fact_tag_review，并按 tag_key 算准确率。"""
    import csv
    path = Path(args.input)
    if not path.exists():
        print(f"文件不存在：{path}")
        return 1
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    filled = [r for r in rows if (r.get("verdict") or "").strip()]
    if not filled:
        print("没有任何一行填了 verdict，先填再导入")
        return 1

    # 列错位是这类 CSV 最隐蔽的错：字段还在、顺序串了，verdict 校验查不出来。
    # 所以拿 name 与库里的产品名对一遍——对不上说明列错位或拿错了文件。
    conn0 = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db_names = dict(conn0.execute("SELECT app_id, name FROM dim_app").fetchall())
    known_keys = {r[0] for r in conn0.execute("SELECT tag_key FROM dim_tag_def")}
    conn0.close()

    errs = []
    for i, r in enumerate(filled, 2):
        v = r["verdict"].strip().lower()
        if v not in ("correct", "wrong", "unsure"):
            errs.append(f"第 {i} 行 verdict 非法：{v}")
        if v == "wrong" and not (r.get("human_value") or "").strip():
            errs.append(f"第 {i} 行判 wrong 但没填 human_value")
        if r["app_id"] not in db_names:
            errs.append(f"第 {i} 行 app_id 不在库里：{r['app_id']}")
        elif (r.get("name") or "").strip() and r["name"].strip() != (db_names[r["app_id"]] or "").strip():
            errs.append(f"第 {i} 行 name 与库里不一致（疑似列错位）："
                        f"CSV「{r['name']}」 vs 库「{db_names[r['app_id']]}」")
        if r.get("tag_key") not in known_keys:
            errs.append(f"第 {i} 行 tag_key 不在标签字典里：{r.get('tag_key')}")
        if not (r.get("value") or "").strip():
            errs.append(f"第 {i} 行 value 为空")
    if errs:
        print(f"校验失败，{len(errs)} 个问题：")
        for e in errs[:10]:
            print(f"  - {e}")
        if len(errs) > 10:
            print(f"  … 另 {len(errs)-10} 个")
        return 1

    conn = sqlite3.connect(args.db)
    now = datetime.now().isoformat(timespec="seconds")
    for r in filled:
        conn.execute("""
            INSERT INTO fact_tag_review(batch, app_id, tag_key, value, verdict,
                                        human_value, reviewer, reviewed_at, note)
            VALUES (?,?,?,?,?,?,?,?,?)""",
                     (r["batch"] or args.batch, r["app_id"], r["tag_key"], r["value"],
                      r["verdict"].strip().lower(), (r.get("human_value") or "").strip() or None,
                      args.reviewer, now, (r.get("note") or "").strip() or None))
    conn.commit()

    print(f"已写入 {len(filled)} 条抽检记录\n")
    print(f"{'tag_key':<11}{'核对':>5}{'对':>5}{'错':>5}{'存疑':>6}{'准确率':>8}")
    for k, tot, ok, bad, uns in conn.execute("""
            SELECT tag_key, COUNT(*),
                   SUM(verdict='correct'), SUM(verdict='wrong'), SUM(verdict='unsure')
              FROM fact_tag_review GROUP BY tag_key ORDER BY tag_key"""):
        decided = (ok or 0) + (bad or 0)
        acc = f"{100*(ok or 0)/decided:.0f}%" if decided else "—"
        print(f"{k:<11}{tot:>5}{ok or 0:>5}{bad or 0:>5}{uns or 0:>6}{acc:>8}")
    tot, ok, bad, uns = conn.execute("""
        SELECT COUNT(*), SUM(verdict='correct'), SUM(verdict='wrong'), SUM(verdict='unsure')
          FROM fact_tag_review""").fetchone()
    decided = (ok or 0) + (bad or 0)
    print(f"\n合计 {tot} 条；判对 {ok or 0} / 判错 {bad or 0} / 存疑 {uns or 0}"
          f"；准确率 {100*(ok or 0)/decided:.1f}%（不含存疑）" if decided else "\n合计 0 条")
    if bad:
        print("\n判错的（badcase 清单）：")
        for r in conn.execute("""
                SELECT a.name, r.tag_key, r.value, r.human_value, r.note
                  FROM fact_tag_review r JOIN dim_app a USING (app_id)
                 WHERE r.verdict='wrong'"""):
            print(f"  {r[0][:24]:<26}{r[1]:<10}{r[2]:<20}→ {r[3] or '(未填)'}  {r[4] or ''}")
    conn.close()
    return 0


def cmd_audit(args) -> int:
    """对**库内现有标签**跑一遍校验（validate 的输入通常是待入库的 JSON）。

    为什么要有它：改完数据后想知道「库里现在还是不是干净的」，
    以前的唯一办法是内联写一段脚本，既不可复现也会漏掉 descr 来源那一层的判断。
    """
    conn = conn_ro(args.db)
    defs = load_defs(conn)
    rows = conn.execute("""
        SELECT app_id, tag_key, value, evidence_strength, evidence, need_review,
               descr_country, descr_sha1, method, confidence
          FROM fact_tag ORDER BY app_id, tag_key""").fetchall()
    conn.close()
    if not rows:
        print("fact_tag 为空")
        return 0
    items: dict[str, dict] = {}
    for a, k, v, st, ev, nr, cc, sh, mm, cf in rows:
        items.setdefault(a, {"app_id": a, "tags": []})["tags"].append(
            {"tag_key": k, "value": v, "evidence_strength": st, "evidence": ev,
             "need_review": nr, "descr_country": cc, "descr_sha1": sh,
             "method": mm, "confidence": cf})
    errs = validate({"items": list(items.values())}, defs,
                    descr_index(args.db), tag_source_index(args.db))
    print(f"库内 {len(rows)} 条标签 / {len(items)} 个产品 → 错误 {len(errs)}，"
          f"提示 {len(WARNINGS)}")
    for e in errs:
        print(f"  - {e}")
    for w in WARNINGS[:20]:
        print(f"  · {w}")
    return 1 if errs else 0


def cmd_stats(args) -> int:
    conn = conn_ro(args.db)
    try:
        total = conn.execute("SELECT COUNT(*) FROM fact_tag").fetchone()[0]
        apps = conn.execute("SELECT COUNT(DISTINCT app_id) FROM fact_tag").fetchone()[0]
        rev = conn.execute("SELECT COUNT(*) FROM fact_tag WHERE need_review=1").fetchone()[0]
        # 分母用「曾在榜出现过的产品」，不用 dim_app 全表：
        # 后者含只在维度层留痕、从没进过 Top100 的产品（11-05 实测 22 个），会把覆盖率算低。
        n_all = conn.execute("SELECT COUNT(DISTINCT app_id) FROM fact_rank").fetchone()[0]
        print(f"标签 {total} 条 / 覆盖产品 {apps} 个（曾在榜 {n_all} 个）/ 待复核 {rev} 条")
        if total:
            print("\n按标签键（覆盖率 = 有该键的产品 / 库内产品）：")
            for k, c, a, r in conn.execute("""
                SELECT tag_key, COUNT(*) c, COUNT(DISTINCT app_id) a, SUM(need_review) r
                  FROM fact_tag GROUP BY tag_key ORDER BY tag_key"""):
                print(f"  {k:<10} {c:>4} 条 / {a:>4} 产品（{100*a/n_all:5.1f}%）/ 待复核 {r or 0}")
            print("\n证据强度：")
            for s, c in conn.execute("""SELECT COALESCE(evidence_strength,'未标'), COUNT(*)
                                          FROM fact_tag GROUP BY 1 ORDER BY 2 DESC"""):
                print(f"  {s:<10} {c}")
            print("\nsubgenre 分布：")
            for v, c in conn.execute("""SELECT value, COUNT(*) FROM fact_tag
                                         WHERE tag_key='subgenre' GROUP BY 1 ORDER BY 2 DESC"""):
                print(f"  {v:<26} {c}")
            print("\n未分类占比（subgenre=unclear / unknown_genre）：")
            row = conn.execute("""SELECT
                SUM(value='unclear'), SUM(value='other.unknown_genre')
                FROM fact_tag WHERE tag_key='subgenre'""").fetchone()
            print(f"  unclear {row[0] or 0} / unknown_genre {row[1] or 0}")
        rv = conn.execute("SELECT COUNT(*) FROM fact_tag_review").fetchone()[0]
        if rv:
            print(f"\n抽检 {rv} 条：")
            for v, c in conn.execute("""SELECT verdict, COUNT(*) FROM fact_tag_review
                                        GROUP BY verdict ORDER BY 2 DESC"""):
                print(f"  {v:<8} {c}")
    finally:
        conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export")
    e.add_argument("--sample", type=int, default=10)
    e.add_argument("--genre", default=None,
                   help="按父类导出（如 Puzzle）；配合 --all 取该父类全部产品")
    e.add_argument("--all", action="store_true", help="配合 --genre：取该父类全部去重产品")
    e.add_argument("--skip-tagged", action="store_true", help="跳过 fact_tag 里已有标签的产品")
    e.add_argument("--out", default="tag_work/in.json")
    e.set_defaults(func=cmd_export)

    for name, fn in (("validate", cmd_validate), ("import", cmd_import)):
        p = sub.add_parser(name)
        p.add_argument("--in", dest="input", required=True)
        p.set_defaults(func=fn)

    s = sub.add_parser("stats")
    s.set_defaults(func=cmd_stats)

    au = sub.add_parser("audit", help="对库内现有标签跑校验（改完数据后自查）")
    au.set_defaults(func=cmd_audit)

    sr = sub.add_parser("sample-review", help="生成分层抽检表（CSV，待人工填 verdict）")
    sr.add_argument("--out", default="tag_work/review.csv")
    sr.add_argument("--batch", default=datetime.now().strftime("%Y%m%d") + "-A")
    sr.add_argument("--quota", default=None,
                    help="如 subgenre=40,theme=20,play_desc=15,like_of=10,art=10")
    sr.set_defaults(func=cmd_sample_review)

    ir = sub.add_parser("import-review", help="回收抽检表并算准确率")
    ir.add_argument("--in", dest="input", required=True)
    ir.add_argument("--batch", default=datetime.now().strftime("%Y%m%d") + "-A")
    ir.add_argument("--reviewer", default="hwz")
    ir.set_defaults(func=cmd_import_review)

    args = ap.parse_args()
    if not Path(args.db).exists():
        print(f"库不存在：{args.db}")
        return 1
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
