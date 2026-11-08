#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重算 reports/2026-11-06_v3.md 里仍在使用的数字，并与报告值逐条比对。

校验范围
--------
只校验报告里仍在使用、且能按固定截点复现的数字。报告 §3 的「累计评价份额」
与「品类内 CR4」已于 2026-11-06 从页面移除、在报告里降级为存档，本节不校验
（原因见 CHECKS 里 §3 的注释）。存档段的数字留作历史记录，不参与可复现声明。

为什么需要这个脚本
------------------
报告里的数字如果只存在于正文，谁也没法判断它是不是算错了、口径变了、或者
只是数据库被后来的抓取覆盖过。这个脚本把「报告里的每个数字」翻译成一条带截点
的 SQL，跑一遍就知道：

  ✓ 一致      —— 数字可溯源，且当前库里能复现
  ✗ 不一致    —— 要么报告写错了，要么口径变了，要么数据被覆盖
  ? 无法判定  —— SQL 返回 NULL（通常是该场景下没有样本）

关于截点
--------
所有 SQL 都带 `snap_date <= --cut`。默认 2026-11-05，即 v3 报告的 14 个榜日窗口。
**为什么不跟着库里的最新日期走**：写报告时库里已有 11-05 的榜日，但当天那班
会在次日凌晨被覆盖，当天的数字不能引用（约束 13）。截点写死，才不会今天对得上、
明天对不上。

一个必须知道的坑
----------------
每天会被自动化跑两次（下午一次、次日凌晨一次），**后者覆盖前者**。
所以「某一天」的数据在当天下午读和次日读不是同一份：
  · 10-30 16:10 读到 153 次进榜，10-31 04:12 覆盖后变成 156
本脚本记录的是**覆盖之后的终版**。报告里的数字以本脚本为准。

用法
----
  python scripts/verify_numbers.py
  python scripts/verify_numbers.py --cut 2026-10-30   # 校验 v1 的窗口（多数会不一致，正常）
  python scripts/verify_numbers.py --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "radar.db"
DEFAULT_CUT = "2026-11-05"

# 报告里出现的每个数字：名称 → (SQL, 期望值)
# SQL 里统一用 :cut 参数，返回单个标量。
CHECKS: list[tuple[str, str, object]] = [
    # ── 头部：面板与规模 ────────────────────────────────────────────
    ("面板榜日数", "select count(distinct snap_date) from fact_rank where snap_date<=:cut", 14),
    ("榜位事实行数", "select count(*) from fact_rank where snap_date<=:cut", 5600),
    ("在榜出现过的产品数",
     "select count(distinct app_id) from fact_rank where snap_date<=:cut", 479),
    ("在榜出现过的（产品×店面）数",
     "select count(*) from (select distinct country,app_id from fact_rank where snap_date<=:cut)", 629),

    # ── §1 进榜 ───────────────────────────────────────────────────
    ("进榜次数（合计）", "select count(*) from ne where censored=0", 229),
    ("进榜产品数（去重）", "select count(distinct app_id) from ne where censored=0", 198),
    ("跨国进榜产品数", """select count(*) from (
        select app_id from ne where censored=0 group by 1 having count(distinct country)>1)""", 28),
    ("进榜次数 jp", "select count(*) from ne where censored=0 and country='jp'", 73),
    ("进榜次数 kr", "select count(*) from ne where censored=0 and country='kr'", 71),
    ("进榜次数 cn", "select count(*) from ne where censored=0 and country='cn'", 46),
    ("进榜次数 us", "select count(*) from ne where censored=0 and country='us'", 39),
    ("首日均名次 jp", """select round(avg(f.rank),1) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and ne.country='jp'""", 74.9),
    ("首日均名次 kr", """select round(avg(f.rank),1) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and ne.country='kr'""", 67.3),
    ("首日均名次 cn", """select round(avg(f.rank),1) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and ne.country='cn'""", 73.9),
    ("首日均名次 us", """select round(avg(f.rank),1) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and ne.country='us'""", 77.2),
    ("首日最好名次 jp", """select min(f.rank) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and ne.country='jp'""", 1),
    ("首日最好名次 kr", """select min(f.rank) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and ne.country='kr'""", 1),
    ("首日最好名次 cn", """select min(f.rank) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and ne.country='cn'""", 3),
    ("首日最好名次 us", """select min(f.rank) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and ne.country='us'""", 1),
    ("首日进前 20 的次数", """select count(*) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and f.rank<=20""", 27),
    ("首日进前 20 jp", """select count(*) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and ne.country='jp' and f.rank<=20""", 9),
    ("首日进前 20 kr", """select count(*) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and ne.country='kr' and f.rank<=20""", 9),
    ("首日进前 20 cn", """select count(*) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and ne.country='cn' and f.rank<=20""", 6),
    ("首日进前 20 us", """select count(*) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and ne.country='us' and f.rank<=20""", 3),
    ("首日在 50 名外的次数", """select count(*) from ne join fact_rank f
        on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        where ne.censored=0 and f.rank>50""", 182),
    ("逐日进榜-10-24", "select count(*) from ne where censored=0 and fs='2026-10-24'", 51),
    ("逐日进榜-11-04", "select count(*) from ne where censored=0 and fs='2026-11-04'", 12),

    # ── §1 进榜品类（去重产品）────────────────────────────────────
    ("进榜品类 Puzzle", "select count(distinct app_id) from ne_g where sub_genre='Puzzle'", 42),
    ("进榜品类 Simulation", "select count(distinct app_id) from ne_g where sub_genre='Simulation'", 29),
    ("进榜品类 Action", "select count(distinct app_id) from ne_g where sub_genre='Action'", 24),
    ("进榜品类 Strategy", "select count(distinct app_id) from ne_g where sub_genre='Strategy'", 21),
    ("进榜品类 Role Playing", "select count(distinct app_id) from ne_g where sub_genre='Role Playing'", 16),
    ("进榜品类 Adventure", "select count(distinct app_id) from ne_g where sub_genre='Adventure'", 15),
    ("jp 进榜 Puzzle", "select count(distinct app_id) from ne_g where country='jp' and sub_genre='Puzzle'", 27),
    ("jp 进榜 Action", "select count(distinct app_id) from ne_g where country='jp' and sub_genre='Action'", 9),
    ("jp 进榜 Strategy", "select count(distinct app_id) from ne_g where country='jp' and sub_genre='Strategy'", 7),
    ("kr 进榜 Puzzle", "select count(distinct app_id) from ne_g where country='kr' and sub_genre='Puzzle'", 15),
    ("kr 进榜 Action", "select count(distinct app_id) from ne_g where country='kr' and sub_genre='Action'", 11),
    ("kr 进榜 Strategy", "select count(distinct app_id) from ne_g where country='kr' and sub_genre='Strategy'", 9),
    ("us 进榜 Puzzle", "select count(distinct app_id) from ne_g where country='us' and sub_genre='Puzzle'", 8),
    ("us 进榜 Action", "select count(distinct app_id) from ne_g where country='us' and sub_genre='Action'", 8),
    ("us 进榜 Simulation", "select count(distinct app_id) from ne_g where country='us' and sub_genre='Simulation'", 6),
    ("us 进榜 Strategy", "select count(distinct app_id) from ne_g where country='us' and sub_genre='Strategy'", 4),
    ("cn 进榜 Simulation", "select count(distinct app_id) from ne_g where country='cn' and sub_genre='Simulation'", 7),
    ("cn 进榜 Adventure", "select count(distinct app_id) from ne_g where country='cn' and sub_genre='Adventure'", 6),
    ("cn 进榜 Strategy", "select count(distinct app_id) from ne_g where country='cn' and sub_genre='Strategy'", 5),
    ("cn 进榜 Role Playing", "select count(distinct app_id) from ne_g where country='cn' and sub_genre='Role Playing'", 5),
    # 赌场错标修复后：中国区 4 款被 Apple 标「娱乐场」的非赌场游戏（捕鱼/找茬/消除）
    # 已正确回落到 Casual/Board/Puzzle；真赌场全部在 us（CrownCoins、Tang Luck、
    # Jackpot Nights、Victory Blast、Bruno Reels），由 v_rank_sub 第③层兜底识别，
    # 无一遗漏。原断言写 cn=5 是旧 bug 计数（把中国区误标当成赌场），现改为锁
    # us 当前在榜真赌场数（按 v_rank_sub 全快照去重）。
    ("us 在榜 Casino", "select count(distinct app_id) from v_rank_sub where snap_date<=:cut and country='us' and sub_genre='Casino'", 5),
    ("cn 可识别品类数", """select count(distinct sub_genre) from ne_g
        where country='cn' and sub_genre<>'Unclassified'""", 12),

    # ── §2 留榜：混合口径 + cohort ───────────────────────────────
    ("仍在榜次数（混合口径）", """select count(*) from ne where censored=0 and exists(
        select 1 from fact_rank f where f.snap_date=:cut and f.country=ne.country and f.app_id=ne.app_id)""", 91),
    ("cohort fixed7 分母", "select n_at_risk from cohort where basis='fixed7' and days_since_entry=1", 143),
    ("cohort D+1 连续", "select p_continuous from cohort where basis='fixed7' and days_since_entry=1", 0.79),
    ("cohort D+2 连续", "select p_continuous from cohort where basis='fixed7' and days_since_entry=2", 0.566),
    ("cohort D+3 连续", "select p_continuous from cohort where basis='fixed7' and days_since_entry=3", 0.392),
    ("cohort D+5 连续", "select p_continuous from cohort where basis='fixed7' and days_since_entry=5", 0.273),
    ("cohort D+7 连续", "select p_continuous from cohort where basis='fixed7' and days_since_entry=7", 0.238),
    ("cohort D+7 时点", "select p_point from cohort where basis='fixed7' and days_since_entry=7", 0.483),
    ("cohort D+7 回锅条数", "select n_back from cohort where basis='fixed7' and days_since_entry=7", 35),
    ("cohort D+7 当天在榜条数", "select n_point from cohort where basis='fixed7' and days_since_entry=7", 69),
    ("cohort D+7 连续条数", "select n_continuous from cohort where basis='fixed7' and days_since_entry=7", 34),
    ("cohort k=14 分母为 0", "select n_at_risk from cohort where basis='all' and days_since_entry=14", 0),

    # ── §2 连续性分布（最新榜日 400 个在榜产品）───────────────────
    ("连续 13 天在榜", """with b as (select country,app_id,snap_date,
          row_number() over (partition by country,app_id order by snap_date) rn
          from fact_rank where snap_date<=:cut),
        cl as (select *,cast(julianday(snap_date) as integer)-rn isl from b),
        st as (select *,count(*) over (partition by country,app_id,isl) streak from cl)
        select count(*) from st where snap_date=:cut and streak>=13""", 277),
    ("连续 8–12 天", """with b as (select country,app_id,snap_date,
          row_number() over (partition by country,app_id order by snap_date) rn
          from fact_rank where snap_date<=:cut),
        cl as (select *,cast(julianday(snap_date) as integer)-rn isl from b),
        st as (select *,count(*) over (partition by country,app_id,isl) streak from cl)
        select count(*) from st where snap_date=:cut and streak between 8 and 12""", 37),
    ("连续 4–7 天", """with b as (select country,app_id,snap_date,
          row_number() over (partition by country,app_id order by snap_date) rn
          from fact_rank where snap_date<=:cut),
        cl as (select *,cast(julianday(snap_date) as integer)-rn isl from b),
        st as (select *,count(*) over (partition by country,app_id,isl) streak from cl)
        select count(*) from st where snap_date=:cut and streak between 4 and 7""", 36),
    ("连续 1–3 天", """with b as (select country,app_id,snap_date,
          row_number() over (partition by country,app_id order by snap_date) rn
          from fact_rank where snap_date<=:cut),
        cl as (select *,cast(julianday(snap_date) as integer)-rn isl from b),
        st as (select *,count(*) over (partition by country,app_id,isl) streak from cl)
        select count(*) from st where snap_date=:cut and streak<=3""", 50),

    # ── §2 榜位日变动 ─────────────────────────────────────────────
    ("日变动样本 cn", "select count(*) from mv where country='cn'", 1223),
    ("日变动上升 cn", "select sum(prev_rank>rank) from mv where country='cn'", 522),
    ("日变动不动 cn", "select sum(prev_rank=rank) from mv where country='cn'", 149),
    ("日变动下降 cn", "select sum(prev_rank<rank) from mv where country='cn'", 552),
    ("日变动平均 cn", "select round(avg(prev_rank-rank),2) from mv where country='cn'", -0.32),
    ("日变动最大涨 cn", "select max(prev_rank-rank) from mv where country='cn'", 46),
    ("日变动最大跌 cn", "select min(prev_rank-rank) from mv where country='cn'", -41),
    ("日变动样本 us", "select count(*) from mv where country='us'", 1214),
    ("日变动上升 us", "select sum(prev_rank>rank) from mv where country='us'", 519),
    ("日变动不动 us", "select sum(prev_rank=rank) from mv where country='us'", 134),
    ("日变动下降 us", "select sum(prev_rank<rank) from mv where country='us'", 561),
    ("日变动平均 us", "select round(avg(prev_rank-rank),2) from mv where country='us'", -0.28),
    ("日变动最大涨 us", "select max(prev_rank-rank) from mv where country='us'", 49),
    ("日变动最大跌 us", "select min(prev_rank-rank) from mv where country='us'", -56),
    ("日变动样本 jp", "select count(*) from mv where country='jp'", 1199),
    ("日变动上升 jp", "select sum(prev_rank>rank) from mv where country='jp'", 491),
    ("日变动不动 jp", "select sum(prev_rank=rank) from mv where country='jp'", 113),
    ("日变动下降 jp", "select sum(prev_rank<rank) from mv where country='jp'", 595),
    ("日变动平均 jp", "select round(avg(prev_rank-rank),2) from mv where country='jp'", -0.89),
    ("日变动最大涨 jp", "select max(prev_rank-rank) from mv where country='jp'", 58),
    ("日变动最大跌 jp", "select min(prev_rank-rank) from mv where country='jp'", -56),
    ("日变动样本 kr", "select count(*) from mv where country='kr'", 1182),
    ("日变动上升 kr", "select sum(prev_rank>rank) from mv where country='kr'", 436),
    ("日变动不动 kr", "select sum(prev_rank=rank) from mv where country='kr'", 131),
    ("日变动下降 kr", "select sum(prev_rank<rank) from mv where country='kr'", 615),
    ("日变动平均 kr", "select round(avg(prev_rank-rank),2) from mv where country='kr'", -1.22),
    ("日变动最大涨 kr", "select max(prev_rank-rank) from mv where country='kr'", 47),
    ("日变动最大跌 kr", "select min(prev_rank-rank) from mv where country='kr'", -67),

    # ── §2 净变化（报告写的是"已删除，但两个时点方向相反"，数字要能复现）──
    ("净变化 cn", """select round(avg(f.rank)-avg(v.rank),1) from ne
        join fact_rank f on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        join v_rank_sub v on v.snap_date=:cut and v.country=ne.country and v.app_id=ne.app_id
        where ne.censored=0 and ne.country='cn'""", 7.1),
    ("净变化 us", """select round(avg(f.rank)-avg(v.rank),1) from ne
        join fact_rank f on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        join v_rank_sub v on v.snap_date=:cut and v.country=ne.country and v.app_id=ne.app_id
        where ne.censored=0 and ne.country='us'""", 0.3),
    ("净变化 kr", """select round(avg(f.rank)-avg(v.rank),1) from ne
        join fact_rank f on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        join v_rank_sub v on v.snap_date=:cut and v.country=ne.country and v.app_id=ne.app_id
        where ne.censored=0 and ne.country='kr'""", 7.3),
    ("净变化 jp", """select round(avg(f.rank)-avg(v.rank),1) from ne
        join fact_rank f on f.snap_date=ne.fs and f.country=ne.country and f.app_id=ne.app_id
        join v_rank_sub v on v.snap_date=:cut and v.country=ne.country and v.app_id=ne.app_id
        where ne.censored=0 and ne.country='jp'""", 4.5),

    # ── §3 占位席位（最新榜日单日截面）─────────────────────────
    # 累计评价份额与品类内 CR4 已于 2026-11-06 从页面移除，v3 报告里也降级为
    # 「仅存档」。两者都依赖 v_rank_sub.rating_count（分店面的当前值，每天被
    # 采集覆盖），用固定 cut 重算必然漂移。指标已退役，这里不再校验。
    ("us Puzzle 席位", "select count(*) from v_rank_sub where snap_date=:cut and country='us' and sub_genre='Puzzle'", 44),
    ("jp Puzzle 席位", "select count(*) from v_rank_sub where snap_date=:cut and country='jp' and sub_genre='Puzzle'", 40),
    ("kr Puzzle 席位", "select count(*) from v_rank_sub where snap_date=:cut and country='kr' and sub_genre='Puzzle'", 36),
    ("cn Action 席位", "select count(*) from v_rank_sub where snap_date=:cut and country='cn' and sub_genre='Action'", 19),
    ("cn Adventure 席位", "select count(*) from v_rank_sub where snap_date=:cut and country='cn' and sub_genre='Adventure'", 14),

    # ── §3 标签（四个键，全部由本地模型生成）──────────────────────────
    # 覆盖数随生成推进增长，是「回填完成」时的稳定值：
    # 2026-11-06 跑 gen_cn_blurb --all 回填 207 个历史产品，
    # 再跑 gen_descr_cn 翻 110 条日韩商店介绍，之后不再增长（幂等：descr_sha1 未变即跳过）。
    # 译名/译文只给需要的产品生成，故覆盖数低于 play_desc，这是预期不是缺陷。
    # 2026-11-07 把 like_of 的口径从「标杆产品 like」改成「玩法类型短语」，
    # 全量重生成 215 条并清掉 34 条旧口径残留。下面两个覆盖数是这次重生成后的
    # 实测值，属**有意的数据修正**：play_desc 顺带补了 2 条空简介（255→257），
    # like_of 因留空与清理下降（227→195）。同 commit 更新，其余断言一律不动。
    ("play_desc 覆盖产品数", """select count(distinct app_id) from fact_tag
        where tag_key='play_desc' and value<>''
          and app_id in (select distinct app_id from fact_rank where snap_date<=:cut)""", 257),
    ("like_of 覆盖产品数", """select count(distinct app_id) from fact_tag
        where tag_key='like_of' and value<>''
          and app_id in (select distinct app_id from fact_rank where snap_date<=:cut)""", 195),
    ("name_cn 覆盖产品数", """select count(distinct app_id) from fact_tag
        where tag_key='name_cn' and value<>''
          and app_id in (select distinct app_id from fact_rank where snap_date<=:cut)""", 154),
    # 译文只翻日韩：207 个进榜产品里 110 个是日/韩文描述，其余是中文或英文。
    # 2026-11-07 P0 用 gen_descr_cn.py --only-missing 补了 10 条确实缺译文的产品，
    # 其中 Gardenscapes(1105855019) 是窗口内的老产品（韩文店面描述此前漏翻），
    # 覆盖数 106→107。这是**有意的数据修正**，不是生成漂移；同 commit 更新期望值。
    # 其余 110 条存量译文未被重翻（--only-missing 不看 sha、直接跳过）。
    ("descr_cn 覆盖产品数", """select count(distinct app_id) from fact_tag
        where tag_key='descr_cn' and value<>''
          and app_id in (select distinct app_id from fact_rank where snap_date<=:cut)""", 107),

    # ── §4 年龄结构（最新榜日，四档）─────────────────────────────
    ("us 0–30 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='us' and age_days between 0 and 30""", 2),
    ("us 31–90 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='us' and age_days between 31 and 90""", 4),
    ("us 91–365 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='us' and age_days between 91 and 365""", 37),
    ("us 365+ 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='us' and age_days>365""", 57),
    ("jp 0–30 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='jp' and age_days between 0 and 30""", 16),
    ("jp 31–90 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='jp' and age_days between 31 and 90""", 9),
    ("jp 91–365 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='jp' and age_days between 91 and 365""", 36),
    ("jp 365+ 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='jp' and age_days>365""", 39),
    ("kr 0–30 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='kr' and age_days between 0 and 30""", 11),
    ("kr 31–90 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='kr' and age_days between 31 and 90""", 11),
    ("kr 91–365 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='kr' and age_days between 91 and 365""", 30),
    ("kr 365+ 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='kr' and age_days>365""", 48),
    ("cn 0–30 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='cn' and age_days between 0 and 30""", 11),
    ("cn 31–90 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='cn' and age_days between 31 and 90""", 4),
    ("cn 91–365 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='cn' and age_days between 91 and 365""", 9),
    ("cn 365+ 天", """select count(*) from v_rank_sub
        where snap_date=:cut and country='cn' and age_days>365""", 76),

    # ── §5 日增（最新一天）──────────────────────────────────────
    ("11-05 日增实际跨度（小时）", """select round(hours_gap,1) from v_metric_diff
        where snap_date=:cut and country='us' and hours_gap is not null limit 1""", 24.1),
    ("11-05 日增第一（us）", """select delta_1d from v_metric_diff
        where snap_date=:cut and country='us' order by delta_1d desc limit 1""", 10614),
    ("11-05 日增第一（jp）", """select delta_1d from v_metric_diff
        where snap_date=:cut and country='jp' order by delta_1d desc limit 1""", 4329),
    ("11-05 日增第二（jp）", """select delta_1d from v_metric_diff
        where snap_date=:cut and country='jp' order by delta_1d desc limit 1 offset 1""", 3677),

    # ── §6 边界里引用的数字 ─────────────────────────────────────
    ("Unclassified 榜行", """select count(*) from v_rank_sub
        where snap_date<=:cut and sub_genre='Unclassified'""", 105),
    ("age_days 取不到的榜行", """select count(*) from v_rank_sub
        where snap_date<=:cut and age_days is null""", 2),
    ("左删失行数（起点日）", "select count(*) from ne where censored=1", 400),
]


def build(conn: sqlite3.Connection, cut: str) -> None:
    """把报告用到的中间表一次性建好，避免每条 SQL 里重复写窗口逻辑。"""
    conn.executescript(
        """
        DROP TABLE IF EXISTS temp.ne;
        DROP TABLE IF EXISTS temp.ne_g;
        DROP TABLE IF EXISTS temp.mv;
        DROP TABLE IF EXISTS temp.cohort;
        """
    )
    conn.execute(
        """
        CREATE TEMP TABLE ne AS
        WITH b AS (SELECT * FROM fact_rank WHERE snap_date <= :cut),
             ss AS (SELECT country, chart, MIN(snap_date) AS s FROM b GROUP BY 1, 2),
             e  AS (SELECT country, chart, app_id, MIN(snap_date) AS fs FROM b GROUP BY 1, 2, 3)
        SELECT e.country, e.chart, e.app_id, e.fs,
               ss.s AS subset_start,
               (e.fs = ss.s) AS censored
          FROM e JOIN ss ON ss.country = e.country AND ss.chart = e.chart
        """,
        {"cut": cut},
    )
    conn.execute(
        """
        CREATE TEMP TABLE ne_g AS
        SELECT ne.country, ne.app_id, COALESCE(v.sub_genre, 'Unclassified') AS sub_genre
          FROM ne
          JOIN v_rank_sub v ON v.snap_date = ne.fs
                           AND v.country   = ne.country
                           AND v.app_id    = ne.app_id
         WHERE ne.censored = 0
        """
    )
    conn.execute(
        """
        CREATE TEMP TABLE mv AS
        WITH b AS (
            SELECT country, app_id, snap_date, rank,
                   LAG(rank)      OVER w AS prev_rank,
                   LAG(snap_date) OVER w AS prev_date
              FROM fact_rank
             WHERE snap_date <= :cut
            WINDOW w AS (PARTITION BY country, app_id ORDER BY snap_date)
        )
        SELECT * FROM b
         WHERE prev_rank IS NOT NULL
           AND julianday(snap_date) - julianday(prev_date) = 1
        """,
        {"cut": cut},
    )
    # cohort 直接复用 sql/07，避免同一口径写成两份；传入 :cut 使其与报告定版对齐
    sql07 = (ROOT / "sql" / "07_cohort_survival.sql").read_text(encoding="utf-8")
    body = sql07.rstrip().rstrip(";")
    body = "\n".join(line for line in body.splitlines()
                     if not line.strip().startswith("--"))
    conn.execute(f"CREATE TEMP TABLE cohort AS {body}", {"cut": cut})


def _tol(expect) -> float:
    """按报告值的量级与小数位数决定容差。

    不能用固定 0.051：那是为「一位小数的均值」设计的，套到三位小数的比例上，
    0.907 与 0.865 也会被判成一致（差值 0.042 < 0.051）——等于假绿。
    规则：
      · 比例（|v| < 1）一律按三位小数，容差 0.0005
      · 其余按位数给半个最小单位：75.3 → 0.05
    """
    v = float(expect)
    if abs(v) < 1:
        return 0.0005
    s = repr(v)
    if "." in s:
        return 0.5 * 10 ** (-len(s.split(".")[1])) + 1e-9
    return 1e-9


def main() -> int:
    ap = argparse.ArgumentParser(description="重算报告里的每个数字并比对")
    ap.add_argument("--cut", default=DEFAULT_CUT, help="数据截点（含），默认 2026-11-05")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"找不到数据库：{args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    build(conn, args.cut)

    stamp = conn.execute(
        "SELECT MAX(run_at) FROM job_log WHERE snap_date <= ?", (args.cut,)
    ).fetchone()[0]

    rows, bad = [], 0
    for label, sql, expect in CHECKS:
        try:
            row = conn.execute(sql, {"cut": args.cut}).fetchone()
            got = row[0] if row else None      # 0 行时不能直接取 [0]
        except sqlite3.Error as e:
            got = f"SQL 错误: {e}"
        if isinstance(got, float) and isinstance(expect, (int, float)):
            ok = abs(got - expect) <= _tol(expect)
        else:
            ok = got == expect
        bad += 0 if ok else 1
        rows.append({"指标": label, "报告值": expect, "重算值": got, "一致": ok})

    if args.as_json:
        print(json.dumps(
            {"cut": args.cut, "snapshot_stamp": stamp, "mismatch": bad, "rows": rows},
            ensure_ascii=False, indent=2))
        return 1 if bad else 0

    w = max(len(r["指标"]) for r in rows) + 2
    print(f"数据截点 {args.cut}　快照版本 {stamp}")
    print(f"{'指标':<{w}}{'报告值':>16}{'重算值':>16}   ")
    print("-" * (w + 38))
    for r in rows:
        mark = "✓" if r["一致"] else "✗"
        print(f"{r['指标']:<{w}}{str(r['报告值']):>16}{str(r['重算值']):>16}   {mark}")
    total = len(rows)
    print("-" * (w + 38))
    if bad:
        print(f"{total} 项中 {bad} 项与报告不一致")
        return 1
    print(f"{total} 项全部一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
