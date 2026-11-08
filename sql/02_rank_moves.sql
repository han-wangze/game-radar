-- sql/02_rank_moves.sql
-- 榜位日变动 / 连续在榜天数 / 是否首次进榜或断档后回归。
--
-- 三个字段的口径（写清楚，否则下游各算一版）：
--   rank_delta      = prev_rank - rank，**正数 = 上升**（名次变小）
--   streak_days     = 截至今日的连续在榜天数（掉榜一次即重算，用 gaps-and-islands）
--   days_since_entry= 距该 (国家 × 产品) 首次被看到的天数
--   is_new_entry    = 1 表示面板内首次出现（**含左删失**，见下）
--   is_reentry      = 1 表示不是首次，但上一次在榜不是「昨天」→ 中间掉过榜，属于重新进榜
--   is_censored     = 1 表示该产品的首次出现就在面板起点日 ⇒ 它「早就在榜」，不是当天进榜
--
-- ⚠️ 真实新进榜 = is_new_entry = 1 AND is_censored = 0。
--    不加这个条件的 is_new_entry 是「全量首次观察数」，其中起点日那批（每天 400 条国家×产品）
--    属于左删失，直接当「新游」会把整批老产品算成新面孔。
--    交叉校验：is_new_entry=1 AND is_censored=0 的条数，**必须等于 `v_new_entrant` 的条数**。
--    两者都会随面板增长，所以**不要把数字写死在这里**，由 `tests/test_kou_jing.py` 的断言守。
--
-- 为什么不用计划文档里那句「LAG(rank) IS NULL 就是新进榜」：
--   那只抓得到首次出现。一个产品 10-24 在榜、10-25 掉出、10-27 回来，
--   LAG 给的是 10-24 的 rank（非 NULL），会被误判成「连续在榜」。
--   所以这里同时看 LAG(snap_date)，用天数差区分「连续」与「断档后回归」。

WITH base AS (
    SELECT snap_date,
           country,
           chart,
           rank,
           app_id,
           LAG(rank)      OVER w AS prev_rank,
           LAG(snap_date) OVER w AS prev_snap_date,
           ROW_NUMBER()   OVER w AS rn,
           MIN(snap_date) OVER (PARTITION BY country, chart, app_id) AS first_seen
      FROM fact_rank
    WINDOW w AS (PARTITION BY country, chart, app_id ORDER BY snap_date)
),
calc AS (
    SELECT *,
           CAST(julianday(snap_date) - julianday(first_seen)     AS INTEGER) AS days_since_entry,
           CAST(julianday(snap_date) - julianday(prev_snap_date) AS INTEGER) AS gap_days,
           -- 连续段编号：同一段内 (日期序数 - 行号) 恒定，换段即变
           CAST(julianday(snap_date) AS INTEGER) - rn AS island_id
      FROM base
)
SELECT snap_date,
       country,
       rank,
       prev_rank,
       prev_rank - rank                AS rank_delta,
       days_since_entry,
       COUNT(*) OVER (PARTITION BY country, chart, app_id, island_id) AS streak_days,
       CASE WHEN prev_rank IS NULL THEN 1 ELSE 0 END AS is_new_entry,
       CASE WHEN prev_rank IS NOT NULL AND gap_days > 1 THEN 1 ELSE 0 END AS is_reentry,
       CASE WHEN first_seen = (SELECT MIN(snap_date) FROM fact_rank) THEN 1 ELSE 0 END AS is_censored,
       app_id
  FROM calc
 ORDER BY snap_date DESC, country, rank;
