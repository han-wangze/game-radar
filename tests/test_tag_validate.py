#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
game-radar / 标签校验规则回归测试
----------------------------------
守的是 tag_apps.validate 的 8 条规则。用内联的小样本，不依赖数据库与 tag_work/ 里的批量文件，
所以能随时跑、不会因为样本换代而失效。

为什么要有这个文件：
  v3 把 confidence 从"必填"降成"可选"，同时新增了 evidence_strength 与 play_desc 必填。
  这些规则一旦被谁"顺手简化"掉，坏标签会静默入库，而 fact_tag 一旦写进去就长得像真的。
  所以每条规则都要有一个**必须被拦住**的反例。

用法：python tests/test_tag_validate.py
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from tag_apps import validate  # noqa: E402

DEFS = {
    "play_desc": {"name_cn": "玩法一句话", "domain": None, "max_len": 40, "multi": False, "ver": 1},
    "like_of":   {"name_cn": "像谁",     "domain": None, "max_len": 30, "multi": False, "ver": 1},
    "subgenre":  {"name_cn": "细分品类", "domain": {"puzzle.match3", "action.moba"}, "max_len": None,
                  "multi": False, "ver": 1},
    "theme":     {"name_cn": "题材",     "domain": {"fantasy", "modern"}, "max_len": None,
                  "multi": False, "ver": 1},
    "art":       {"name_cn": "美术",     "domain": {"3d_stylized", "unclear"}, "max_len": None,
                  "multi": False, "ver": 1},
}

DESCRS = {
    "A1": "《示例消消乐》滑动手指让三个同色相连即可消除，清新的藤蔓场景。",
    "A2": "示例 MOBA，5V5 三路推塔。",
}

BASE = {
    "items": [
        {"app_id": "A1", "tags": [
            {"tag_key": "play_desc", "value": "滑动三个同色消除过关",
             "evidence_strength": "direct", "evidence": "滑动手指让三个同色相连即可消除"},
            {"tag_key": "subgenre", "value": "puzzle.match3",
             "evidence_strength": "inferred", "evidence": "三消玩法"},
            {"tag_key": "theme", "value": "fantasy", "evidence_strength": "inferred", "evidence": "测试"},
            {"tag_key": "art", "value": "unclear", "evidence_strength": "none",
             "evidence": "描述无画面信息", "need_review": 1},
        ]},
        {"app_id": "A2", "tags": [
            {"tag_key": "play_desc", "value": "5v5 三路推塔",
             "evidence_strength": "direct", "evidence": "5V5 三路推塔"},
            {"tag_key": "subgenre", "value": "action.moba",
             "evidence_strength": "direct", "evidence": "5V5 三路推塔"},
        ]},
    ]
}

FAILS = 0


def check(name, expect_error, mutate=None, defs=None):
    global FAILS
    payload = copy.deepcopy(BASE)
    if mutate:
        mutate(payload)
    errs = validate(payload, defs or DEFS, DESCRS)
    hit = any(expect_error in e for e in errs) if expect_error else not errs
    print(f"  {'✓' if hit else '✗'} {name}"
          + ("" if hit else f"　实际：{errs[:2]}"))
    if not hit:
        FAILS += 1


def drop(item, key):
    item["tags"] = [t for t in item["tags"] if t["tag_key"] != key]


print("正向：合法样本应通过")
check("两条产品全部合规", None)

print("\n反向：每条规则都要拦住")
check("闭集取值越界",
      "不在值域内",
      lambda d: d["items"][0]["tags"][1].__setitem__("value", "puzzle.sort"))
check("开集超长（play_desc>40）",
      "超长",
      lambda d: d["items"][0]["tags"][0].__setitem__("value", "长" * 41))
check("开集超长（like_of>30）",
      "超长",
      lambda d: d["items"][1]["tags"].append(
          {"tag_key": "like_of", "value": "长" * 31,
           "evidence_strength": "inferred", "evidence": "测试"}))
check("必填键缺失（play_desc）",
      "缺必填键",
      lambda d: drop(d["items"][0], "play_desc"))
check("evidence 为空",
      "缺少 evidence",
      lambda d: d["items"][0]["tags"][2].__setitem__("evidence", "  "))
check("direct 但原文找不到",
      "原文里找不到",
      lambda d: d["items"][0]["tags"][0].__setitem__("evidence", "这句原文里没有"))
check("none 未置 need_review",
      "必须置 need_review=1",
      lambda d: d["items"][0]["tags"][3].pop("need_review"))
check("evidence_strength 非法值",
      "非法",
      lambda d: d["items"][0]["tags"][0].__setitem__("evidence_strength", "maybe"))
check("单值键给两个值",
      "单值标签",
      lambda d: d["items"][0]["tags"].append(
          {"tag_key": "theme", "value": "modern",
           "evidence_strength": "inferred", "evidence": "测试"}))
check("confidence 越界（可选，但给了就要合法）",
      "越界",
      lambda d: d["items"][0]["tags"][0].__setitem__("confidence", 1.4))
check("未知 tag_key",
      "未知 tag_key",
      lambda d: d["items"][1]["tags"].append(
          {"tag_key": "mechanic", "value": "merge",
           "evidence_strength": "inferred", "evidence": "测试"}))

def _series_art(d, val="3d_stylized", ev="依系列既有形象推断为 3D 卡通", st="inferred", rev=0):
    t = d["items"][0]["tags"][3]
    t.update(value=val, evidence=ev, evidence_strength=st, need_review=rev)


check("art 用系列旧作推断给具体值", "系列推断措辞",
      lambda d: _series_art(d))
check("art 写「与前作一致」", "系列推断措辞",
      lambda d: _series_art(d, ev="与前作保持一致的美术风格"))
# 同一条线索的反面：判不了就该记 unclear + none + need_review=1，这是允许的
check("art 判不了记 unclear（含系列措辞也放行）", None,
      lambda d: _series_art(d, val="unclear", ev="依系列推断，描述无画面信息",
                            st="none", rev=1))
# 不能误伤：正常描述里出现「现代」「动作」不该被拦
check("art 正常描述不被误伤", None,
      lambda d: _series_art(d, ev="现代都市题材，3D 卡通渲染"))

print("\n变异测试：故意把规则拿掉，对应断言必须失效（否则上面的通过是假的）")
import tag_apps  # noqa: E402

saved = tag_apps.REQUIRED_KEYS
try:
    tag_apps.REQUIRED_KEYS = set()
    p = copy.deepcopy(BASE)
    drop(p["items"][0], "play_desc")
    leaked = not any("缺必填键" in e for e in validate(p, DEFS, DESCRS))
finally:
    tag_apps.REQUIRED_KEYS = saved
print(f"  {'✓' if leaked else '✗'} 清空 REQUIRED_KEYS 后，缺 play_desc 确实漏过 → 证明这条断言在起作用")
if not leaked:
    FAILS += 1

saved_defs = copy.deepcopy(DEFS)
try:
    for d in DEFS.values():
        d["domain"] = None          # 全部当开集 → 闭集校验失效
    p = copy.deepcopy(BASE)
    p["items"][0]["tags"][1]["value"] = "puzzle.sort"
    leaked = not any("不在值域内" in e for e in validate(p, DEFS, DESCRS))
finally:
    DEFS.clear()
    DEFS.update(saved_defs)
print(f"  {'✓' if leaked else '✗'} 把闭集改成开集后，越界值确实漏过 → 证明值域校验在起作用")
if not leaked:
    FAILS += 1

print(f"\n{'全部通过' if FAILS == 0 else f'{FAILS} 项失败'}")
sys.exit(1 if FAILS else 0)
