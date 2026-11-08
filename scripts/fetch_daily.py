#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 每日快照采集（D0）
--------------------------------
采集 Apple App Store 四国（us/cn/jp/kr）**游戏榜** Top100 的排名 + 详情，写入 SQLite。

数据源（2026-10-23 实测）：
  - 榜单：itunes.apple.com/{cc}/rss/topfreeapplications/limit=100/genre={gid}/json
          游戏总榜 genre=6014；子品类 7014=RPG / 7017=Strategy / 7015=Simulation / 7012=Puzzle
          单次上限 100 条（limit=200 也只回 100）
  - 详情：itunes.apple.com/lookup?id=a,b,c&country={cc}&entity=software
          单批 50 个可行；提供 userRatingCount / primaryGenreName / description
  - 榜单无历史接口 → 榜位变化类指标必须靠每日累积，缺一天永久缺一天

用法：
    python scripts/fetch_daily.py                     # 四国 × 游戏总榜
    python scripts/fetch_daily.py --charts games rpg strategy
    python scripts/fetch_daily.py --countries us --dry-run

退出码：0 全部成功 / 1 部分失败 / 2 全部失败
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "radar.db"
SCHEMA_PATH = ROOT / "schema.sql"

CHART_URL = "https://itunes.apple.com/{cc}/rss/topfreeapplications/limit={limit}/genre={gid}/json"
LOOKUP_URL = "https://itunes.apple.com/lookup?id={ids}&country={cc}&entity=software"
UA = "Mozilla/5.0 (compatible; game-radar/0.1; +research)"

DEFAULT_COUNTRIES = ["us", "cn", "jp", "kr"]

# chart 别名 → Apple genre id。chart 名会写进 fact_rank.chart
CHARTS: dict[str, dict] = {
    "games":    {"gid": 6014, "label": "Games（总榜）"},
    "rpg":      {"gid": 7014, "label": "Role Playing"},
    "strategy": {"gid": 7017, "label": "Strategy"},
    "sim":      {"gid": 7015, "label": "Simulation"},
    "puzzle":   {"gid": 7012, "label": "Puzzle"},
    "action":   {"gid": 7001, "label": "Action"},
}
DEFAULT_CHARTS = ["games"]
LOOKUP_BATCH = 50

UPSERT_APP = """
INSERT INTO dim_app (app_id, bundle_id, name, seller, genre, genres_all, release_date,
                     ver_date, rating_avg, rating_count, price, currency,
                     summary, descr, first_seen, last_seen)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(app_id) DO UPDATE SET
    bundle_id   = COALESCE(excluded.bundle_id, dim_app.bundle_id),
    name        = excluded.name,
    seller      = COALESCE(excluded.seller, dim_app.seller),
    genre       = COALESCE(excluded.genre, dim_app.genre),
    genres_all  = COALESCE(excluded.genres_all, dim_app.genres_all),
    release_date= COALESCE(excluded.release_date, dim_app.release_date),
    ver_date    = COALESCE(excluded.ver_date, dim_app.ver_date),
    rating_avg  = COALESCE(excluded.rating_avg, dim_app.rating_avg),
    rating_count= COALESCE(excluded.rating_count, dim_app.rating_count),
    price       = COALESCE(excluded.price, dim_app.price),
    currency    = COALESCE(excluded.currency, dim_app.currency),
    summary     = COALESCE(excluded.summary, dim_app.summary),
    -- descr 已废弃（2026-11-05 下移到 dim_app_country）：这里一直传 NULL，
    -- 于是本列冻结在迁移前的值，不会被继续改写。打标/校验读 dim_app_country.descr。
    last_seen   = excluded.last_seen,
    first_seen  = COALESCE(dim_app.first_seen, excluded.first_seen)
"""

INSERT_RANK = """
INSERT OR REPLACE INTO fact_rank (snap_date, country, chart, rank, app_id)
VALUES (?,?,?,?,?)
"""

# 分店面字段（release_date / rating_count / price ...）必须写进 dim_app_country：
# 同一个 app 在四国返回不同值，dim_app 只有一列、只能存最后写入国的值。
UPSERT_APP_COUNTRY = """
INSERT INTO dim_app_country (app_id, country, release_date, ver_date, name, genre, genres_all,
                             rating_count, rating_avg, price, currency,
                             descr, screenshots, descr_sha1, first_seen, last_seen)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(app_id, country) DO UPDATE SET
    release_date = COALESCE(excluded.release_date, dim_app_country.release_date),
    ver_date     = COALESCE(excluded.ver_date, dim_app_country.ver_date),
    name         = COALESCE(NULLIF(excluded.name, ''), dim_app_country.name),
    genre        = COALESCE(excluded.genre, dim_app_country.genre),
    genres_all   = COALESCE(excluded.genres_all, dim_app_country.genres_all),
    rating_count = COALESCE(excluded.rating_count, dim_app_country.rating_count),
    rating_avg   = COALESCE(excluded.rating_avg, dim_app_country.rating_avg),
    price        = COALESCE(excluded.price, dim_app_country.price),
    currency     = COALESCE(excluded.currency, dim_app_country.currency),
    -- 描述与截图是分店面口径，且描述会随版本更新而变：每次抓到就覆盖，并记下新的指纹。
    -- 历史文本 Apple 不提供，所以「文本变过」只能靠 fact_tag.descr_sha1 与这里比对发现。
    descr        = COALESCE(NULLIF(excluded.descr, ''), dim_app_country.descr),
    screenshots  = COALESCE(excluded.screenshots, dim_app_country.screenshots),
    descr_sha1   = COALESCE(excluded.descr_sha1, dim_app_country.descr_sha1),
    last_seen    = excluded.last_seen,
    first_seen   = COALESCE(dim_app_country.first_seen, excluded.first_seen)
"""

INSERT_METRIC = """
INSERT OR REPLACE INTO fact_metric (snap_date, country, app_id, rating_count, rating_avg, price)
VALUES (?,?,?,?,?,?)
"""

INSERT_LOG = """
INSERT INTO job_log (run_at, snap_date, country, chart, status,
                     n_rank, n_detail, http_code, duration_s, note)
VALUES (?,?,?,?,?,?,?,?,?,?)
"""


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def http_json(url: str, retries: int = 3, timeout: int = 30) -> dict:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                wait = 1.5 * attempt
                log(f"    请求失败（第{attempt}次）：{type(exc).__name__} → {wait:.1f}s 后重试")
                time.sleep(wait)
    raise RuntimeError(f"请求最终失败 {url}: {last_err}")


def _label(node, *path, default=None):
    """安全取 RSS 嵌套 label：_label(e, 'im:name', 'label')"""
    cur = node
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if cur is not None else default


def fetch_chart(cc: str, gid: int, limit: int) -> list[dict]:
    """返回 [{rank, app_id, bundle_id, name, seller, release_date, price, currency, summary}]"""
    url = CHART_URL.format(cc=cc, limit=limit, gid=gid)
    payload = http_json(url)
    entries = payload.get("feed", {}).get("entry", [])
    if isinstance(entries, dict):        # 只有 1 条时 Apple 返回 dict 而不是 list
        entries = [entries]

    rows: list[dict] = []
    for i, e in enumerate(entries, start=1):
        app_id = _label(e, "id", "attributes", "im:id")
        if not app_id:
            continue
        price_attr = _label(e, "im:price", "attributes", default={}) or {}
        try:
            price = float(price_attr.get("amount"))
        except (TypeError, ValueError):
            price = None
        rows.append({
            "rank": i,                       # 数组序号即排名（接口无 rank 字段）
            "app_id": str(app_id),
            "bundle_id": _label(e, "id", "attributes", "im:bundleId"),
            "name": _label(e, "im:name", "label", default="") or "",
            "seller": _label(e, "im:artist", "label"),
            "release_date": (_label(e, "im:releaseDate", "label", default="") or "")[:10] or None,
            "price": price,
            "currency": price_attr.get("currency"),
            "summary": _label(e, "summary", "label"),
        })
    return rows


def fetch_details(cc: str, app_ids: list[str]) -> dict[str, dict]:
    """批量 lookup（实测单批 50 个可行）。返回 {app_id: detail}"""
    out: dict[str, dict] = {}
    for start in range(0, len(app_ids), LOOKUP_BATCH):
        chunk = app_ids[start:start + LOOKUP_BATCH]
        payload = http_json(LOOKUP_URL.format(ids=",".join(chunk), cc=cc))
        for r in payload.get("results", []):
            aid = str(r.get("trackId", ""))
            if not aid:
                continue
            _descr = (r.get("description") or "").strip()
            out[aid] = {
                "bundle_id": r.get("bundleId"),
                "name": r.get("trackName") or "",
                "seller": r.get("sellerName") or "",
                "genre": r.get("primaryGenreName") or "",
                "genres_all": json.dumps(r.get("genres") or [], ensure_ascii=False),
                "release_date": (r.get("releaseDate") or "")[:10] or None,
                "ver_date": (r.get("currentVersionReleaseDate") or "")[:10] or None,
                "rating_avg": r.get("averageUserRating"),
                "rating_count": r.get("userRatingCount"),
                "price": r.get("price"),
                "currency": r.get("currency"),
                # descr / screenshots 是分店面口径：同一款产品四国描述语言不同、截图也不同。
                # 存 sha1 是为了让 fact_tag 能判断「证据对着的那份文本后来变没变」。
                "descr": _descr,
                "screenshots": json.dumps(r.get("screenshotUrls") or [], ensure_ascii=False),
                "descr_sha1": (hashlib.sha1(_descr.encode("utf-8")).hexdigest()[:12]
                               if _descr else None),
            }
        if start + LOOKUP_BATCH < len(app_ids):
            time.sleep(0.8)
    return out


def ensure_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def run_one(conn: sqlite3.Connection, cc: str, chart_key: str, limit: int,
            snap_date: str, dry_run: bool) -> str:
    t0 = time.time()
    run_at = datetime.now().isoformat(timespec="seconds")
    chart_name = f"top-free-{chart_key}"
    gid = CHARTS[chart_key]["gid"]

    try:
        chart_rows = fetch_chart(cc, gid, limit)
    except Exception as exc:  # noqa: BLE001
        log(f"[{cc}/{chart_key}] 榜单失败：{exc}")
        if not dry_run:
            conn.execute(INSERT_LOG, (run_at, snap_date, cc, chart_name, "fail",
                                      0, 0, None, round(time.time() - t0, 1), str(exc)[:300]))
            conn.commit()
        return "fail"

    log(f"[{cc}/{chart_key}] 榜单 {len(chart_rows)} 条")
    app_ids = [r["app_id"] for r in chart_rows]

    try:
        details = fetch_details(cc, app_ids)
    except Exception as exc:  # noqa: BLE001
        log(f"[{cc}/{chart_key}] 详情失败：{exc}")
        details = {}

    log(f"[{cc}/{chart_key}] 详情 {len(details)}/{len(app_ids)} 条")

    if dry_run:
        for r in chart_rows[:5]:
            d = details.get(r["app_id"], {})
            log(f"    #{r['rank']:<3} {r['name'][:26]:<26} | {(d.get('genre') or '?'):<16} "
                f"| 评分数 {d.get('rating_count')} | 上线 {d.get('release_date')}")
        return "ok"

    for r in chart_rows:
        d = details.get(r["app_id"], {})
        conn.execute(UPSERT_APP, (
            r["app_id"], d.get("bundle_id") or r["bundle_id"],
            d.get("name") or r["name"], d.get("seller") or r["seller"],
            d.get("genre"), d.get("genres_all"),
            d.get("release_date") or r["release_date"], d.get("ver_date"),
            d.get("rating_avg"), d.get("rating_count"),
            d.get("price") if d.get("price") is not None else r["price"],
            d.get("currency") or r["currency"],
            r["summary"], None, snap_date, snap_date,   # descr 传 None：dim_app.descr 已废弃、保持冻结
        ))
        # 分店面口径单独落一行：同一 app 在四国日期/评分数/价格/描述都不同
        conn.execute(UPSERT_APP_COUNTRY, (
            r["app_id"], cc,
            d.get("release_date") or r["release_date"], d.get("ver_date"),
            d.get("name") or r["name"], d.get("genre"), d.get("genres_all"),
            d.get("rating_count"), d.get("rating_avg"),
            d.get("price") if d.get("price") is not None else r["price"],
            d.get("currency") or r["currency"],
            d.get("descr"), d.get("screenshots"), d.get("descr_sha1"),
            snap_date, snap_date,
        ))
        conn.execute(INSERT_RANK, (snap_date, cc, chart_name, r["rank"], r["app_id"]))
        if d:
            conn.execute(INSERT_METRIC, (snap_date, cc, r["app_id"], d.get("rating_count"),
                                        d.get("rating_avg"), d.get("price")))

    status = "ok" if len(details) == len(app_ids) else "partial"
    conn.execute(INSERT_LOG, (run_at, snap_date, cc, chart_name, status,
                              len(chart_rows), len(details), 200,
                              round(time.time() - t0, 1),
                              None if status == "ok" else "部分详情缺失"))
    conn.commit()
    return status


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--countries", nargs="+", default=DEFAULT_COUNTRIES)
    ap.add_argument("--charts", nargs="+", default=DEFAULT_CHARTS, choices=list(CHARTS))
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    snap_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log(f"snap_date(UTC)={snap_date}  limit={args.limit}  "
        f"countries={args.countries}  charts={args.charts}  dry_run={args.dry_run}")

    conn = ensure_db()
    results: dict[str, str] = {}
    for cc in args.countries:
        for chart_key in args.charts:
            results[f"{cc}/{chart_key}"] = run_one(conn, cc, chart_key, args.limit,
                                                   snap_date, args.dry_run)
            time.sleep(0.8)

    ok = sum(1 for v in results.values() if v == "ok")
    partial = sum(1 for v in results.values() if v == "partial")
    fail = sum(1 for v in results.values() if v == "fail")
    log(f"完成：ok={ok} partial={partial} fail={fail}")

    if not args.dry_run:
        n_rank = conn.execute("SELECT COUNT(*) FROM fact_rank WHERE snap_date=?",
                              (snap_date,)).fetchone()[0]
        n_app = conn.execute("SELECT COUNT(*) FROM dim_app").fetchone()[0]
        log(f"库内 {snap_date} 榜单记录 {n_rank} 条；dim_app 累计 {n_app} 个产品")
    conn.close()

    if fail == len(results):
        return 2
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
