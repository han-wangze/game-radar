"""把构建好的三页与 README 同步到发布镜像，并推送到 GitHub。

为什么需要这么个脚本：本机是唯一有数据库的地方（Apple 不提供历史榜单接口，
GitHub Actions 抓不到这段历史），所以「线上看板」只能由本机构建后推上去。
这个脚本只做搬运，不采集、不改页面、不碰源数据。

它被 `build_site.py` 在构建成功后调用（需要仓库根有 `.publish-on-build` 开关文件），
也可以单独运行：

    python scripts/publish_site.py             # 同步 + 提交 + 推送
    python scripts/publish_site.py --dry-run   # 只报差异，不写不推

只在内容真的变了时才提交：页面里的「本页生成于 …」是构建时刻，同一天跑两次
数据没变、时间变了，那种提交对读者没有价值，只会把仓库历史刷成噪音。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLISH = Path(os.environ.get("RADAR_PUBLISH_DIR") or (ROOT.parent / "game-radar-publish"))

# 只搬这几个文件；镜像里的 sample 数据、报告、脚本由人决定，脚本不擅自覆盖
FILES = [
    "site/index.html",
    "site/yesterday.html",
    "site/weekly.html",
    "README.md",
]

# 构建时刻：两页的口径不同（日报「本页生成于」，周报「生成于 …（本机时区）」）
TS_PATTERNS = [
    re.compile(r"本页生成于 \d{4}-\d{2}-\d{2} \d{2}:\d{2}"),
    re.compile(r"生成于 \d{4}-\d{2}-\d{2} \d{2}:\d{2}（本机时区）"),
]


def normalize(text: str) -> str:
    """抹掉构建时刻，只比内容。"""
    for pat in TS_PATTERNS:
        text = pat.sub("<生成时刻>", text)
    return text


def snap_date_of(html: str) -> str:
    m = re.search(r"榜日\s*<b>(\d{4}-\d{2}-\d{2})</b>", html)
    return m.group(1) if m else ""


def run(cmd: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def publish(dry_run: bool = False, push: bool = True, quiet: bool = False) -> int:
    say = (lambda *a: None) if quiet else print
    if not PUBLISH.exists():
        say(f"发布镜像不存在：{PUBLISH}（设环境变量 RADAR_PUBLISH_DIR 指定）")
        return 2
    if not (PUBLISH / ".git").exists():
        say(f"发布镜像不是 git 仓库：{PUBLISH}")
        return 2

    changed, same = [], []
    for rel in FILES:
        src, dst = ROOT / rel, PUBLISH / rel
        if not src.exists():
            say(f"缺文件，跳过：{rel}")
            continue
        if not dst.exists():
            changed.append(rel)
            continue
        a = normalize(src.read_text(encoding="utf-8"))
        b = normalize(dst.read_text(encoding="utf-8"))
        (same if a == b else changed).append(rel)

    if not changed:
        say("内容与线上一致（只差生成时刻），不提交")
    if dry_run:
        if changed:
            say("变更：" + "、".join(changed))
        return 0

    if changed:
        say("变更：" + "、".join(changed))
        for rel in changed:
            (PUBLISH / rel).write_text((ROOT / rel).read_text(encoding="utf-8"),
                                       encoding="utf-8", newline="\n")

        add = run(["git", "add", "--"] + changed, PUBLISH)
        if add.returncode != 0:
            say("git add 失败：" + (add.stderr or "").strip())
            return 1

        staged = run(["git", "diff", "--cached", "--quiet"], PUBLISH)
        if staged.returncode != 0:
            snap = snap_date_of((ROOT / "site/index.html").read_text(encoding="utf-8"))
            msg = (f"看板更新：{snap} 榜日" if snap else "看板更新") + "\n\n" + \
                  "由 scripts/publish_site.py 在构建后自动同步：\n" + \
                  "\n".join(f"- {r}" for r in changed) + "\n"
            commit = run(["git", "commit", "-q", "-m", msg], PUBLISH)
            if commit.returncode != 0:
                say("提交失败：" + (commit.stderr or commit.stdout or "").strip())
                return 1

    # 上一次可能提交成功、推送失败（断网/代理 502）。那种情况下文件是一致的，
    # 但本地 branch 领先远端 —— 只比文件就会永远推不上去，所以要单独看落后了几条。
    ahead = run(["git", "rev-list", "--count", "origin/main..HEAD"], PUBLISH)
    n_ahead = int((ahead.stdout or "0").strip() or 0) if ahead.returncode == 0 else 0
    if n_ahead == 0:
        say("已提交，远端已是最新")
        return 0

    if not push:
        say(f"有 {n_ahead} 条提交待推送（--no-push）")
        return 0

    sent = None
    for attempt, wait in enumerate((0, 5, 15), 1):
        if wait:
            time.sleep(wait)
        sent = run(["git", "push", "origin", "main"], PUBLISH)
        if sent.returncode == 0:
            break
        out = ((sent.stdout or "") + (sent.stderr or "")).strip().splitlines()
        last = out[-1] if out else ""
        say(f"第 {attempt} 次推送失败：{last}")

    out = ((sent.stdout or "") + (sent.stderr or "")).strip().splitlines()
    if sent.returncode != 0:
        say("推送失败（提交已留在本地，下次构建会补推）")
        return 1

    # 远端 SHA 才是真相：push 退出码 0 也可能什么都没推上去
    head = run(["git", "rev-parse", "HEAD"], PUBLISH).stdout.strip()
    remote = run(["git", "ls-remote", "origin", "main"], PUBLISH).stdout.split()
    if remote and remote[0] == head:
        say(f"已推送 {head[:7]}（{n_ahead} 条），Pages 会在 1 分钟内重新部署")
        return 0
    say(f"推了但远端 SHA 对不上（本地 {head[:7]}）")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报差异")
    ap.add_argument("--no-push", action="store_true", help="提交但不推送")
    args = ap.parse_args()
    return publish(dry_run=args.dry_run, push=not args.no_push)


if __name__ == "__main__":
    raise SystemExit(main())
