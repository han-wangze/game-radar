#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 快照写入回归测试
------------------------------
锁住「一份抓取 = 一份完整快照」这件事。这条链路 2026-11-08 前是逐行
INSERT OR REPLACE，主键含 rank，于是次日凌晨那班只回 99 条时，第 100 名
会留着上一班的数据 —— 条数检查照样通过，快照却是两次抓取拼起来的
（实测命中 4 天，见 scripts/check_alerts.py 的 KNOWN_BROKEN）。

覆盖 5 件事：
  1. 完整抓取是整份替换：新旧产品一个都不留
  2. 只回 99 条时不覆盖上一班（一条都不改，交告警）
  3. `--limit 20` 的局部抓取只替换 1–20，不抹掉 21–100
  4. 中间缺一条（#50）不假装完整：写进去，条数就是 99，由告警发现
  5. 空响应不覆盖

用法：python tests/test_snapshot_replace.py        退出码 0 全过 / 1 有失败
不依赖 pytest，也不依赖任何第三方库，不联网。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_daily as fd  # noqa: E402

SCHEMA = (ROOT / "schema.sql").read_text(encoding="utf-8")
D = "2026-01-01"
CC, CH = "aa", "top-free-games"


def rows(tag: str, n: int, skip: int | None = None) -> list[dict]:
    """造一份榜单响应：名次 1..n（可以刻意缺第 skip 名）。"""
    return [{"rank": i, "app_id": f"{tag}{i:03d}"}
            for i in range(1, n + 1) if i != skip]


def simulate(conn: sqlite3.Connection, rs: list[dict], limit: int) -> str:
    """复刻 run_one 的决策顺序：先校验，不完整就什么都不写。"""
    ok, _note = fd.snapshot_check(rs, limit)
    if not ok:
        return "partial"
    fd.replace_rank_snapshot(conn, D, CC, CH, rs)
    conn.commit()
    return "ok"


def snapshot(conn: sqlite3.Connection) -> dict[int, str]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT rank, app_id FROM fact_rank WHERE snap_date=? AND country=? AND chart=?",
        (D, CC, CH))}


CASES: list[tuple[str, callable]] = []


def case(fn):
    CASES.append((fn.__name__, fn))
    return fn


@case
def 完整抓取整份替换(conn):
    assert simulate(conn, rows("a", 100), 100) == "ok"
    assert len(snapshot(conn)) == 100
    assert simulate(conn, rows("b", 100), 100) == "ok"
    s = snapshot(conn)
    assert len(s) == 100, f"条数 {len(s)}"
    stale = [v for v in s.values() if v.startswith("a")]
    assert not stale, f"上一班残留 {len(stale)} 条：{stale[:3]}"


@case
def 只回99条不覆盖(conn):
    assert simulate(conn, rows("a", 100), 100) == "ok"
    assert simulate(conn, rows("b", 99), 100) == "partial"
    s = snapshot(conn)
    assert len(s) == 100, f"条数被改动了：{len(s)}"
    assert all(v.startswith("a") for v in s.values()), "上一班快照被部分覆盖"


@case
def 局部抓取不抹高位次(conn):
    assert simulate(conn, rows("a", 100), 100) == "ok"
    assert simulate(conn, rows("c", 20), 20) == "ok"
    s = snapshot(conn)
    assert len(s) == 100, f"条数 {len(s)}"
    assert all(s[i].startswith("c") for i in range(1, 21)), "1–20 没换掉"
    assert all(s[i].startswith("a") for i in range(21, 101)), "21–100 被误删"


@case
def 中间缺一条不假装完整(conn):
    assert simulate(conn, rows("a", 100), 100) == "ok"
    assert simulate(conn, rows("b", 100, skip=50), 100) == "ok"
    s = snapshot(conn)
    assert len(s) == 99, f"条数应暴露为 99，实际 {len(s)}"
    assert 50 not in s, "缺的名次不该被上一班补上"


@case
def 空响应不覆盖(conn):
    assert simulate(conn, rows("a", 100), 100) == "ok"
    assert simulate(conn, [], 100) == "partial"
    assert len(snapshot(conn)) == 100, "空响应把快照删了"


def main() -> int:
    bad = 0
    for name, fn in CASES:
        tmpdir = Path(tempfile.mkdtemp())
        tmp = tmpdir / "t.db"
        conn = None
        try:
            conn = sqlite3.connect(tmp)
            conn.executescript(SCHEMA)
            fn(conn)
            print(f"  ✓ {name}")
        except AssertionError as exc:
            bad += 1
            print(f"  ✗ {name}：{exc}")
        finally:
            if conn is not None:
                conn.close()          # Windows 下不关连接删不掉文件
            shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n{len(CASES) - bad}/{len(CASES)} 项通过")
    return 1 if bad else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
