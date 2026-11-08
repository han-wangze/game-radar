-- sql/03_rating_velocity.sql
-- 增长代理：评分数日增 + 7 个相邻观测的滑动均值。
--
-- 口径（三条，缺一条结果就不可比）：
--   1) 只用 days_gap = 1 的相邻榜日（产品中途掉榜时，跨多天的 delta 不能当日增用）。
--   2) 只用同一国家店面内的差分（评分数是分店面口径，us 与 jp 差 20 倍）。
--   3) 滑动窗口是「最近 7 个相邻日观测」，不是「最近 7 个自然日」——
--      中间掉过榜的产品，窗口里会有断点，用 obs_n 标明实际观测数。
--
-- 越界提醒（2026-11-04 收紧）：
--   这里得到的是「新增评分人数」，**不是下载量、不是新增用户、不是活跃**。
--   评分人数与下载量之间没有稳定换算系数——它取决于评分弹窗设计（iOS 每年限 3 次）、
--   版本是否重置过评分摘要、以及地区评分习惯。任何「用评分反推下载」的做法都会得到伪精度。
--   报告里出现「下载量」「新增用户」「买量」即越界。
--   ⚠️ 早先这里写过「评分/下载约 1%–3%，只能给区间」，已删除：那个系数没有可靠来源，
--      而且「给区间」实质上是在为反推开口子。
--
-- 2026-11-04 补第 4 条口径（比上面三条更容易出事）：
--   4) delta_1d 的间隔不是 24 小时，而是两次抓取的实际间隔（实测 24.0–31.8h，最新一天 12.0h）。
--      同一个「10-30 Roblox 日增」在 13.0h 那版是 +3,993，25.1h 那版是 +7,061，差 77%。
--      ⇒ 本查询必须同时输出 hours_gap；跨产品比大小前先确认间隔相近。
--      ⇒ 最新榜日的 delta 只有半天，不能与往日并列（它的 hours_gap 会明显小于 24）。
--   ⚠️ 连带问题：ma7 是「7 个不等长观测」的均值，其中若混入 short 日（12h）会把均值压低。
--      要跨日比较，先按 gap_kind 过滤，或改用 delta_per_24h 重算（注意它是线性折算，陡增段会高估）。

WITH base AS (
    SELECT country,
           snap_date,
           app_id,
           rating_count,
           delta_1d,
           hours_gap
      FROM v_metric_diff
     WHERE days_gap = 1
),
roll AS (
    SELECT country,
           snap_date,
           app_id,
           rating_count,
           delta_1d,
           hours_gap,
           AVG(delta_1d) OVER w AS ma7,
           COUNT(*)      OVER w AS obs_n
      FROM base
    WINDOW w AS (PARTITION BY country, app_id ORDER BY snap_date
                 ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
)
SELECT r.country,
       r.snap_date,
       a.name,
       r.rating_count,
       r.delta_1d,
       r.hours_gap,                                  -- 必看：增量对应的真实小时数
       CAST(r.ma7 AS INTEGER) AS ma7,
       r.obs_n,
       CASE WHEN r.obs_n >= 3 THEN 1 ELSE 0 END AS ma7_reliable,
       CASE WHEN r.hours_gap IS NULL THEN 'unknown'
            WHEN r.hours_gap < 18      THEN 'short'   -- 半天班，不能与常规日并列
            WHEN r.hours_gap > 28      THEN 'long'    -- 跨了缺日或首日，需单独说明
            ELSE 'normal' END AS gap_kind
  FROM roll r
  JOIN dim_app a USING (app_id)
 ORDER BY r.snap_date DESC, r.country, r.delta_1d DESC;
