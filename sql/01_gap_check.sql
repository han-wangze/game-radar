-- sql/01_gap_check.sql
-- 面板缺口审计：用递归 CTE 造出「应该有的每一天 × 每个国家 × 每个榜」，
-- 与实际数据 LEFT JOIN，列出「整日缺失」和「条数不足 100」的行。
--
-- 为什么要造日历而不是直接 GROUP BY：
--   GROUP BY 只能看到「有的日子」，看不到「没有的日子」。
--   而 Apple 只给当前快照、缺一天永久缺一天，所以「缺哪天」必须靠日历反证。
--
-- 预期结果：正常情况下 0 行。
-- 验证方式：删掉某天某国的一批行，本查询必须报出 missing / short（否则等于没写）。

WITH RECURSIVE
bounds AS (
    SELECT MIN(snap_date) AS d0, MAX(snap_date) AS d1 FROM fact_rank
),
cal(d) AS (
    SELECT d0 FROM bounds
    UNION ALL
    SELECT date(d, '+1 day') FROM cal, bounds WHERE d < d1
),
dims AS (                                   -- 实际出现过的 (国家 × 榜) 组合
    SELECT DISTINCT country, chart FROM fact_rank
),
expected AS (
    SELECT cal.d AS snap_date, dims.country, dims.chart
      FROM cal CROSS JOIN dims
),
actual AS (
    SELECT snap_date, country, chart, COUNT(*) AS n_rank
      FROM fact_rank
     GROUP BY snap_date, country, chart
)
SELECT e.snap_date,
       e.country,
       e.chart,
       COALESCE(a.n_rank, 0) AS n_rank,
       CASE WHEN a.n_rank IS NULL THEN 'missing' ELSE 'short' END AS status
  FROM expected e
  LEFT JOIN actual a
         ON a.snap_date = e.snap_date
        AND a.country   = e.country
        AND a.chart     = e.chart
 WHERE a.n_rank IS NULL OR a.n_rank <> 100
 ORDER BY e.snap_date, e.country;
