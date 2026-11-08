#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
试用 DeepSeek 网络检索（Search 工具），与 chat 接口独立、同源鉴权。
仅用于验证能力，不接入任何生产链路。

用法：
  python scripts/try_deepseek_search.py            # 跑内置的两个示例查询
  python scripts/try_deepseek_search.py "你的查询"  # 自定义查询

计费：Search 成功且 results 非空才计费，5 元 / 1000 次成功请求（见 DeepSeek 文档）。
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
DB_PATH = ROOT / "data" / "radar.db"

_NON_ASCII = re.compile(r"[^\x00-\x7F]")


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        raise RuntimeError(f"缺少 {ENV_PATH}")
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def search(cfg: dict[str, str], query: str, count: int = 5,
           need_content: bool = False, retries: int = 3) -> dict:
    url = cfg["DEEPSEEK_BASE_URL"].rstrip("/") + "/v1/tools/search"
    key = cfg.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError(".env 里没有 DEEPSEEK_API_KEY")
    body = json.dumps({
        "query": query,
        "count": count,
        "need_content": need_content,
    }, ensure_ascii=False).encode("utf-8")

    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json",
                         "User-Agent": "game-radar/try-search"},
                method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                json.JSONDecodeError, TimeoutError) as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError):
                last = RuntimeError(f"HTTP {exc.code}")
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"Search 调用失败：{last}")


def print_results(query: str, payload: dict) -> None:
    results = payload.get("results", [])
    print(f"\n{'='*70}\n查询：{query}\n返回条数：{len(results)}\n{'='*70}")
    for i, r in enumerate(results, 1):
        print(f"\n[{i}] {r.get('title', '')}")
        print(f"    url      : {r.get('url', '')}")
        print(f"    date     : {r.get('date')}")
        print(f"    authority: {r.get('authority')}  rerank: {r.get('rerank_score')}")
        print(f"    snippet  : {r.get('snippet', '')[:160]}")
        summary = r.get("summary", "")
        if summary:
            print(f"    summary  : {summary[:200]}")


def pick_untranslated_from_db() -> str | None:
    """从本地库挑一个非中文原名且还没有中文译名的游戏，返回其原名。"""
    if not DB_PATH.exists():
        return None
    import sqlite3
    con = sqlite3.connect(str(DB_PATH))
    try:
        rows = con.execute(
            "SELECT app_id, name FROM dim_app WHERE name IS NOT NULL AND name <> ''"
        ).fetchall()
        have_cn = {row[0] for row in con.execute(
            "SELECT app_id FROM fact_tag WHERE tag_key='name_cn'"
        ).fetchall()}
    finally:
        con.close()
    for app_id, name in rows:
        if app_id in have_cn:
            continue
        if _NON_ASCII.search(name):   # 含非 ASCII：韩文/日文/含中文等
            # 排除原名已是中文的（中文名不需要检索译名）
            if re.search(r"[一-鿿]", name):
                continue
            return name
    return None


def main() -> None:
    cfg = load_env()
    custom = sys.argv[1] if len(sys.argv) > 1 else None

    if custom:
        payload = search(cfg, custom, count=5)
        print_results(custom, payload)
        return

    # 示例 1：通用连通性 + 返回质量验证（知名产品）
    q1 = "Block Blast! 手游 玩法 开发商 官网"
    print_results(q1, search(cfg, q1, count=5))

    # 示例 2：业务场景——帮一个缺译名的非中文游戏找资料
    name = pick_untranslated_from_db()
    if name:
        q2 = f"{name} 游戏 中文译名 玩法 开发商"
        print_results(q2, search(cfg, q2, count=5))
    else:
        print("\n（本地库未找到「非中文原名且缺译名」的样本，跳过业务示例）")


if __name__ == "__main__":
    main()
