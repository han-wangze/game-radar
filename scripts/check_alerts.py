#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 运行告警检查
--------------------------
无人值守系统最危险的不是出错，是**出错了没人知道**。
health.py 只打印给人看；本脚本给"判断能不能信"这件事下结论，并留下可审计的记录。

七类检查（每类都对应一种真实失败模式）：

  A stale_run        最近一次任务距今 > N 小时   → CRITICAL（自动化停了 / 机器没开）
  B missing_days     某国某榜某整日无数据        → CRITICAL（缺日不可回补）
  C short_day        某国某日条数 != 100         → WARN（数据源波动或抓取被截断）
  D latest_lag       最新榜日落后 UTC 今天 > 1 天 → CRITICAL（没抓到新数据）
  E run_fail         最近 2 天出现 fail/partial   → WARN（重试后可能已恢复）
  F field_null       release_date 空值率 > 1%    → WARN（维度回填漏了）
  G snapshot_broken  条数与 job_log 不符 / 名次不连续 → WARN（快照被截断或被拼接）

用法：
    python scripts/check_alerts.py
    python scripts/check_alerts.py --json-print      # 额外打印 JSON
    python scripts/check_alerts.py --db data/_x.db   # 对副本检查（测试用）
    python scripts/check_alerts.py --stale-hours 30

退出码：0 无 CRITICAL（可能有 WARN）／1 有 CRITICAL／2 脚本或库异常

记录的落地：
    logs/alerts.json   最近一次检查的结构化结果（覆盖写）
    logs/alerts.log    追加一行摘要（时间 / 结论 / 触发项），用于回溯"哪天开始坏的"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "radar.db"
LOG_DIR = ROOT / "logs"

EXPECTED_PER_DAY = 100

# 已知的历史遗留：2026-11-08 之前，采集用 INSERT OR REPLACE 逐行写，
# 次日凌晨那班只返回 99 条时会漏掉第 100 名，于是那一行继续留着前一天的
# （同一天 16:00 那班的）数据 —— 四天的快照是「99 条新的 + 1 条旧的」拼成的。
# 当时没有行级来源可以分辨哪一条是旧的，事后再改只会把错换成另一种错，
# 所以登记在这里、不再重复报警；新代码（整份替换 + 条数守卫）不会再产生。
# 不加白名单的话这条 WARN 会永远亮着，最后没人再看告警。
KNOWN_BROKEN = {
    ("2026-10-30", "us"): "次日凌晨只回 99 条，第 100 名沿用 16:00 那班",
    ("2026-11-03", "cn"): "同上",
    ("2026-11-05", "cn"): "同上",
    ("2026-11-05", "kr"): "同上",
}

# 已知的整日缺口：那天定时任务根本没跑（机器没开/被跳过），不是采集代码的问题。
# Apple 没有历史榜单接口，这一天永远补不回来，报 CRITICAL 也修不了 ——
# 但也不能装作没有，所以单独打印一行「已知缺日」，页面上同样写明。
# 目的只有一个：让告警对**新增**缺口保持灵敏，而不是天天亮红灯变成背景噪音。
KNOWN_MISSING = {
    "2026-11-11": "当天定时任务未运行，Apple 无历史接口，无法回补",
}


def add(findings, level, code, msg, detail=None):
    findings.append({"level": level, "code": code, "msg": msg, "detail": detail or {}})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--stale-hours", type=int, default=26,
                    help="最近一次任务距今超过多少小时算停摆（默认 26，略大于 12h×2）")
    ap.add_argument("--json-print", action="store_true")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"库不存在：{args.db}")
        return 2
    try:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT snap_date FROM fact_rank ORDER BY snap_date")]
        if not days:
            print("还没有任何榜日数据")
            return 2
    except sqlite3.Error as exc:
        print(f"读库失败：{exc}")
        return 2

    findings: list[dict] = []
    now = datetime.now()
    today_utc = datetime.now(timezone.utc).date()
    first, last = days[0], days[-1]

    # ── A 最近一次任务距今多久 ────────────────────────────────
    row = conn.execute("SELECT run_at, status, country FROM job_log "
                       "ORDER BY run_id DESC LIMIT 1").fetchone()
    if row is None:
        add(findings, "CRITICAL", "stale_run", "job_log 为空：从未成功记录过任务")
    else:
        try:
            last_run = datetime.fromisoformat(row["run_at"])
            hours = (now - last_run).total_seconds() / 3600
            if hours > args.stale_hours:
                add(findings, "CRITICAL", "stale_run",
                    f"最近一次任务在 {hours:.1f} 小时前（阈值 {args.stale_hours}h）",
                    {"last_run_at": row["run_at"], "last_status": row["status"]})
        except ValueError:
            add(findings, "WARN", "stale_run", f"run_at 格式无法解析：{row['run_at']}")

    # ── B 整日缺失（按国家×榜造日历反证）────────────────────
    dims = [(r["country"], r["chart"]) for r in conn.execute(
        "SELECT DISTINCT country, chart FROM fact_rank")]
    have = {(r["snap_date"], r["country"], r["chart"]): r["n"] for r in conn.execute(
        "SELECT snap_date, country, chart, COUNT(*) n FROM fact_rank GROUP BY 1,2,3")}
    d0, d1 = date.fromisoformat(first), date.fromisoformat(last)
    missing = []
    d = d0
    while d <= d1:
        for cc, ch in dims:
            if (d.isoformat(), cc, ch) not in have:
                missing.append(f"{d.isoformat()}/{cc}")
        d += timedelta(days=1)
    if missing:
        known_days = sorted({m.split("/")[0] for m in missing
                             if m.split("/")[0] in KNOWN_MISSING})
        missing = [m for m in missing if m.split("/")[0] not in KNOWN_MISSING]
        if known_days:
            print(f"已知缺日 {len(known_days)} 天（{', '.join(known_days)}），"
                  f"见 KNOWN_MISSING，不计入告警")
    if missing:
        add(findings, "CRITICAL", "missing_days",
            f"整日缺失 {len(missing)} 处（缺日不可回补）",
            {"missing": missing[:20]})

    # ── C 条数不足 ───────────────────────────────────────────
    short = [(k[0], k[1], v) for k, v in have.items() if v != EXPECTED_PER_DAY]
    if short:
        add(findings, "WARN", "short_day",
            f"{len(short)} 个（榜日×国家）条数不等于 {EXPECTED_PER_DAY}",
            {"short": [{"date": s[0], "country": s[1], "n": s[2]} for s in short[:20]]})

    # ── D 最新榜日是否落后 ───────────────────────────────────
    lag = (today_utc - date.fromisoformat(last)).days
    if lag > 1:
        add(findings, "CRITICAL", "latest_lag",
            f"最新榜日 {last} 落后 UTC 今天 {lag} 天（没抓到新数据）",
            {"latest": last, "today_utc": today_utc.isoformat()})

    # ── E 近两天是否有失败任务 ───────────────────────────────
    since = (now - timedelta(days=2)).isoformat(timespec="seconds")
    bad = conn.execute(
        "SELECT run_at, country, status, note FROM job_log "
        "WHERE run_at >= ? AND status IN ('fail','partial') ORDER BY run_id DESC LIMIT 10",
        (since,)).fetchall()
    if bad:
        add(findings, "WARN", "run_fail",
            f"近 2 天有 {len(bad)} 次任务非 ok",
            {"runs": [{"run_at": b["run_at"], "country": b["country"],
                       "status": b["status"], "note": b["note"]} for b in bad]})

    # ── F 分店面维度空值率 ───────────────────────────────────
    tot = conn.execute("SELECT COUNT(*) FROM dim_app_country").fetchone()[0]
    nul = conn.execute(
        "SELECT COUNT(*) FROM dim_app_country WHERE release_date IS NULL").fetchone()[0]
    if tot and nul / tot > 0.01:
        add(findings, "WARN", "field_null",
            f"release_date 空值率 {nul}/{tot} = {nul / tot:.1%}（超过 1%，检查是否漏回填）")

    # ── G 快照完整性（条数对得上吗、名次连续吗）───────────────
    # job_log 记「这次抓了多少条」，fact_rank 记「库里存了多少条」，两者必须一致。
    # 只比各地最后一次 ok 的运行：同日双跑时后者本应整份覆盖前者。
    # 名次必须 1..n 连续：中间有洞说明写入被截断，或某一行被单独删过。
    drift = conn.execute("""
        SELECT l.snap_date, l.country, l.chart, l.n_rank, f.n AS n_in_db
          FROM (SELECT snap_date, country, chart, n_rank,
                       ROW_NUMBER() OVER (PARTITION BY snap_date, country, chart
                                          ORDER BY run_id DESC) rn
                  FROM job_log WHERE status = 'ok') l
          JOIN (SELECT snap_date, country, chart, COUNT(*) n
                  FROM fact_rank GROUP BY 1,2,3) f
            ON f.snap_date = l.snap_date
           AND f.country  = l.country
           AND f.chart    = l.chart
         WHERE l.rn = 1 AND l.n_rank <> f.n
    """).fetchall()
    gaps = conn.execute("""
        SELECT snap_date, country, chart, COUNT(*) n, MIN(rank) lo, MAX(rank) hi
          FROM fact_rank GROUP BY 1,2,3
        HAVING lo <> 1 OR hi <> n
    """).fetchall()
    known = [r for r in drift if (r["snap_date"], r["country"]) in KNOWN_BROKEN]
    drift = [r for r in drift if (r["snap_date"], r["country"]) not in KNOWN_BROKEN]
    if known:
        print(f"已知历史遗留 {len(known)} 处（{', '.join(sorted({r['snap_date'] for r in known}))}）"
              f"，见 KNOWN_BROKEN，不计入告警")
    if drift or gaps:
        parts = []
        if drift:
            parts.append(f"{len(drift)} 处条数与 job_log 记录不符")
        if gaps:
            parts.append(f"{len(gaps)} 处名次不连续")
        add(findings, "WARN", "snapshot_broken",
            "；".join(parts) + "（快照可能被截断，或由两次抓取拼成）",
            {"drift": [dict(r) for r in drift[:10]],
             "gaps": [dict(r) for r in gaps[:10]]})

    # ── 汇总输出 ─────────────────────────────────────────────
    crit = [f for f in findings if f["level"] == "CRITICAL"]
    warn = [f for f in findings if f["level"] == "WARN"]
    verdict = "CRITICAL" if crit else ("WARN" if warn else "OK")

    print(f"数据窗口 {first} → {last}（{len(days)} 个榜日）｜检查时间 "
          f"{now.isoformat(timespec='seconds')}")
    print(f"结论：{verdict}    CRITICAL {len(crit)} / WARN {len(warn)}")
    for f in findings:
        print(f"  [{f['level']:<8}] {f['code']:<13} {f['msg']}")
        if f["level"] == "CRITICAL" and f["detail"]:
            print(f"             {json.dumps(f['detail'], ensure_ascii=False)[:400]}")
    if not findings:
        print("  ✓ 七类检查全部通过")

    payload = {
        "checked_at": now.isoformat(timespec="seconds"),
        "verdict": verdict,
        "window": {"first": first, "last": last, "days": len(days)},
        "findings": findings,
    }
    if args.json_print:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    # 落盘（只在主库上写，对副本检查时不污染）
    if Path(args.db).resolve() == DB_PATH.resolve():
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "alerts.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        with (LOG_DIR / "alerts.log").open("a", encoding="utf-8") as fh:
            codes = ",".join(f["code"] for f in findings) or "-"
            fh.write(f"{payload['checked_at']}\t{verdict}\t{codes}\t"
                     f"{first}→{last}\t{len(days)}d\n")

    conn.close()
    return 1 if crit else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
