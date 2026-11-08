#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 口径回归测试
--------------------------
把此前"人工跑一次、看一眼输出"的反向验证固化成可重跑的断言。
改任何一个视图或 SQL 之后跑一遍，能立刻知道有没有把口径改坏。

覆盖 8 件事：
  1. 左删失：面板起点就在榜的产品，is_censored=1，且不进 v_new_entrant
  2. 新游口径不依赖上架日期：releaseDate 是 5 年前、但首次进榜，仍算新游
  3. 断档：中途掉榜再回来，streak 重置、is_reentry=1
  4. 同日残留：fact_metric 里不在 fact_rank 的行，不得进 v_metric_diff
  5. 间隔可量：hours_gap 按 job_log 的抓取时刻算出（相邻日应为 24.0h，首行为 NULL）
  6. 日志缺失时不崩：job_log 为空 → hours_gap 全 NULL，但 delta_1d 仍在
  7. 整日缺失：01_gap_check 报 missing
  8. 条数不足：01_gap_check 报 short（不是 missing）

用法：python tests/test_kou_jing.py            退出码 0 全过 / 1 有失败
不依赖 pytest，也不依赖任何第三方库。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "schema.sql").read_text(encoding="utf-8")
GAP_SQL = (ROOT / "sql" / "01_gap_check.sql").read_text(encoding="utf-8")
MOVES_SQL = (ROOT / "sql" / "02_rank_moves.sql").read_text(encoding="utf-8")

D1, D2, D3 = "2026-01-01", "2026-01-02", "2026-01-03"
CC, CH = "aa", "top-free-games"
A, B, C, Z = "1001", "1002", "1003", "1099"   # A 全在 / B 次日进 / C 中间掉一天 / Z 只出现在指标表


def build(tmp: Path, combo: int) -> sqlite3.Connection:
    """combo 0=正常三天；1=删掉 D2 全天；2=删掉 D3 一条"""
    db = tmp / f"t{combo}.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)

    pads = [f"{i:04d}" for i in range(2000, 2098)]        # 98 个填充产品
    day1 = [A, C] + pads                                  # 100
    day2 = [A, B] + pads                                  # 100
    day3 = [A, C, B] + pads[:-1]                          # 100

    for aid in set(day1 + day2 + day3):
        conn.execute("INSERT INTO dim_app(app_id,name,first_seen,last_seen) VALUES(?,?,?,?)",
                     (aid, f"app-{aid}", D1, D3))
        # release_date 故意设成很久以前：用来证明「新游」不看上架日期
        conn.execute("""INSERT INTO dim_app_country(app_id,country,release_date,genres_all,
                        rating_count,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)""",
                     (aid, CC, "2020-01-01", '["Games","Puzzle"]', 1000, D1, D3))

    for day, ids in ((D1, day1), (D2, day2), (D3, day3)):
        for rank, aid in enumerate(ids, 1):
            conn.execute("INSERT INTO fact_rank VALUES(?,?,?,?,?)",
                         (day, CC, CH, rank, aid))

    if combo == 1:
        conn.execute("DELETE FROM fact_rank WHERE snap_date=? AND country=?", (D2, CC))
    if combo == 2:
        conn.execute("DELETE FROM fact_rank WHERE snap_date=? AND country=? AND rank=100",
                     (D3, CC))

    # 指标：A 0→100→250，B 50→120，外加一行 Z（不在任何一天的榜单里）
    for day, aid, rc in ((D1, A, 0), (D2, A, 100), (D3, A, 250),
                         (D2, B, 50), (D3, B, 120), (D2, Z, 999)):
        conn.execute("INSERT INTO fact_metric(snap_date,country,app_id,rating_count) "
                     "VALUES(?,?,?,?)", (day, CC, aid, rc))

    # 任务日志：v_metric_diff 靠它算 hours_gap。三天都设在 08:00 → 相邻间隔应为 24.0h。
    for day in (D1, D2, D3):
        conn.execute("""INSERT INTO job_log(run_at,snap_date,country,chart,status,n_rank,n_detail)
                        VALUES(?,?,?,?,?,?,?)""",
                     (f"{day}T08:00:00", day, CC, CH, "ok", 100, 100))
    conn.commit()
    return conn


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def test_left_censored(conn):
    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT app_id, is_censored FROM v_entry WHERE country=?", (CC,))}
    assert rows[A] == 1, f"A 在起点日就在榜，应 is_censored=1，实际 {rows[A]}"
    assert rows[C] == 1, f"C 在起点日就在榜，应 is_censored=1，实际 {rows[C]}"
    assert rows[B] == 0, f"B 是次日才进榜，应 is_censored=0，实际 {rows[B]}"


@case
def test_new_entrant_only_b(conn):
    rows = conn.execute("SELECT app_id, entry_rank FROM v_new_entrant").fetchall()
    assert len(rows) == 1, f"新进榜应只有 B 一条，实际 {len(rows)} 条：{rows}"
    assert rows[0][0] == B and rows[0][1] == 2, f"应为 B/第2名，实际 {rows[0]}"


@case
def test_new_entrant_ignores_release_date(conn):
    rd, age = conn.execute(
        "SELECT release_date, age_days FROM v_new_entrant WHERE app_id=?", (B,)).fetchone()
    assert rd == "2020-01-01" and age > 2000, \
        f"B 的上架日应是 5 年前（证明新游口径不看它），实际 {rd} / age={age}"


@case
def test_reentry_and_streak(conn):
    rows = conn.execute(MOVES_SQL).fetchall()
    cols = [d[0] for d in conn.execute(MOVES_SQL).description]
    rec = [dict(zip(cols, r)) for r in rows]
    c3 = [r for r in rec if r["app_id"] == C and r["snap_date"] == D3][0]
    assert c3["streak_days"] == 1, f"C 断档后应重新计数为 1，实际 {c3['streak_days']}"
    assert c3["is_reentry"] == 1, f"C 断档后回归应 is_reentry=1，实际 {c3['is_reentry']}"
    a3 = [r for r in rec if r["app_id"] == A and r["snap_date"] == D3][0]
    assert a3["streak_days"] == 3, f"A 连续三天应 streak=3，实际 {a3['streak_days']}"
    assert a3["is_reentry"] == 0, f"A 没断档，is_reentry 应为 0"


@case
def test_metric_residue_excluded(conn):
    n_z = conn.execute("SELECT COUNT(*) FROM v_metric_diff WHERE app_id=?", (Z,)).fetchone()[0]
    assert n_z == 0, f"Z 不在任何榜单里，不应出现在差分视图（实际 {n_z} 行）"
    d2 = conn.execute("SELECT delta_1d FROM v_metric_diff WHERE app_id=? AND snap_date=?",
                      (A, D2)).fetchone()[0]
    assert d2 == 100, f"A 在 D2 的日增应为 100，实际 {d2}"


@case
def test_hours_gap(conn):
    """delta_1d 的间隔必须能被量出来。没有这一列，增量的单位就是未知的。"""
    rows = {(r[0]): r[1] for r in conn.execute(
        "SELECT snap_date, hours_gap FROM v_metric_diff WHERE app_id=?", (A,))}
    assert rows.get(D2) == 24.0, f"D2 的间隔应为 24.0h，实际 {rows.get(D2)}"
    assert rows.get(D3) == 24.0, f"D3 的间隔应为 24.0h，实际 {rows.get(D3)}"
    assert rows.get(D1) is None, f"首行没有上一行，hours_gap 应为 NULL，实际 {rows.get(D1)}"


@case
def test_hours_gap_without_job_log(conn):
    """job_log 为空时（新库 / 日志被清），视图不能报错，只能让 hours_gap 变 NULL。"""
    conn.execute("DELETE FROM job_log")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM v_metric_diff WHERE delta_1d IS NOT NULL").fetchone()[0]
    assert n > 0, "job_log 清空后不应连带丢掉 delta_1d"
    n_null = conn.execute(
        "SELECT COUNT(*) FROM v_metric_diff WHERE hours_gap IS NOT NULL").fetchone()[0]
    assert n_null == 0, f"job_log 为空时 hours_gap 应全为 NULL，实际 {n_null} 行非空"


@case
def test_gap_missing(conn):
    rows = conn.execute(GAP_SQL).fetchall()
    assert len(rows) == 1, f"应报 1 处缺失，实际 {len(rows)} 处：{rows}"
    assert rows[0][0] == D2 and rows[0][4] == "missing", f"应报 {D2} missing，实际 {rows[0]}"


@case
def test_gap_short(conn):
    rows = conn.execute(GAP_SQL).fetchall()
    assert len(rows) == 1, f"应报 1 处不足，实际 {len(rows)} 处：{rows}"
    assert rows[0][3] == 99 and rows[0][4] == "short", \
        f"应报 99 条 short，实际 {rows[0]}"


# ── 留榜 cohort（sql/07）─────────────────────────────────────────────
# 这条查询最容易写错的不是 SQL 语法，是口径：把「第 k 天在榜」当成留存。
# 场景：B 连续在榜；C 中途掉一天又回来。两者在「时点」上都在，但在「连续」上不同。
COHORT_SQL = (ROOT / "sql" / "07_cohort_survival.sql").read_text(encoding="utf-8")


def build_cohort(tmp: Path) -> sqlite3.Connection:
    """10 天面板：98 个填充产品全程在榜，B 从 D2 起连续在榜，C 在 D4 掉一天再回来。"""
    db = tmp / "cohort.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)

    days = [f"2026-03-{d:02d}" for d in range(1, 11)]      # D1..D10
    d1, d2 = days[0], days[1]
    pads = [f"{i:04d}" for i in range(3000, 3098)]
    filler = pads + ["9001"]                                # 9001 全程在榜（起点日 → 左删失）
    b_id, c_id = "9002", "9003"

    def day_set(i):
        s = list(filler)
        if i >= 1:                 # D2 起
            s.append(b_id)
        if i >= 1 and i != 3:      # D2、D3 在，D4 掉，D5 起回来
            s.append(c_id)
        return s

    for aid in set(filler) | {b_id, c_id}:
        conn.execute("INSERT INTO dim_app(app_id,name,first_seen,last_seen) VALUES(?,?,?,?)",
                     (aid, f"app-{aid}", d1, days[-1]))
        conn.execute("""INSERT INTO dim_app_country(app_id,country,release_date,genres_all,
                        rating_count,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)""",
                     (aid, CC, "2025-01-01", '["Games","Puzzle"]', 100, d1, days[-1]))
    for i, day in enumerate(days):
        for rank, aid in enumerate(day_set(i), 1):
            conn.execute("INSERT INTO fact_rank VALUES(?,?,?,?,?)", (day, CC, CH, rank, aid))
        conn.execute("""INSERT INTO job_log(run_at,snap_date,country,chart,status,n_rank,n_detail)
                        VALUES(?,?,?,?,?,?,?)""",
                     (f"{day}T08:00:00", day, CC, CH, "ok", 100, 100))
    conn.commit()
    return conn


@case
def test_cohort_continuous_vs_point(conn):
    # sql/07 自 2026-11-06 起接收 :cut（面板末日），不传会用库内 MAX(snap_date)；
    # 这里显式传本用例的末日 D10，既测绑参也保持原期望值。
    rows = {(r[0], r[1]): r for r in conn.execute(COHORT_SQL, {"cut": "2026-03-10"})}
    # fixed7：进榜日 D2 + 7 天 = D9 ≤ D10，B 与 C 都进固定分母 → 分母恒为 2
    for k in (1, 2, 3, 5, 7):
        assert rows[("fixed7", k)][2] == 2, f"fixed7 在 k={k} 的分母应恒为 2"

    # D+1：B、C 都在 → 连续与时点都是 2/2
    assert rows[("fixed7", 1)][4] == 1.0, f"D+1 连续应 1.0，实际 {rows[('fixed7', 1)][4]}"
    assert rows[("fixed7", 1)][6] == 1.0, f"D+1 时点应 1.0，实际 {rows[('fixed7', 1)][6]}"

    # D+2（D4）：C 掉榜 → 连续 1/2，时点也 1/2
    assert rows[("fixed7", 2)][4] == 0.5, f"D+2 连续应 0.5，实际 {rows[('fixed7', 2)][4]}"
    assert rows[("fixed7", 2)][6] == 0.5, f"D+2 时点应 0.5，实际 {rows[('fixed7', 2)][6]}"

    # D+3（D5）：C 回来了 → 连续仍 1/2，时点回到 2/2，回锅 1 条
    assert rows[("fixed7", 3)][4] == 0.5, f"D+3 连续应 0.5，实际 {rows[('fixed7', 3)][4]}"
    assert rows[("fixed7", 3)][6] == 1.0, f"D+3 时点应 1.0，实际 {rows[('fixed7', 3)][6]}"
    assert rows[("fixed7", 3)][7] == 1, f"D+3 回锅数应为 1，实际 {rows[('fixed7', 3)][7]}"

    # 连续口径必须单调不增——这是它能当衰减曲线的前提
    seq = [rows[("fixed7", k)][4] for k in (1, 2, 3, 5, 7)]
    assert all(seq[i] >= seq[i + 1] for i in range(len(seq) - 1)), f"连续口径出现回升：{seq}"


def main() -> int:
    # 每个用例一份独立库。多数用例共用同一个 3 天场景（combo 0/1/2），
    # cohort 用例需要 10 天面板，单独一个构造器。
    plan = [(test_left_censored, 0), (test_new_entrant_only_b, 0),
            (test_new_entrant_ignores_release_date, 0),
            (test_reentry_and_streak, 0), (test_metric_residue_excluded, 0),
            (test_hours_gap, 0), (test_hours_gap_without_job_log, 0),
            (test_gap_missing, 1), (test_gap_short, 2),
            (test_cohort_continuous_vs_point, "cohort")]

    tmp = Path(tempfile.mkdtemp(prefix="gr-test-"))
    fails = 0
    try:
        for i, (fn, combo) in enumerate(plan):
            sub = tmp / f"case{i}"          # 每个用例一份独立库，避免互相污染
            sub.mkdir()
            conn = build_cohort(sub) if combo == "cohort" else build(sub, combo)
            try:
                fn(conn)
                print(f"  ✓ {fn.__name__}")
            except AssertionError as exc:
                fails += 1
                print(f"  ✗ {fn.__name__}\n      {exc}")
            finally:
                conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(plan) - fails}/{len(plan)} 通过")
    return 1 if fails else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
