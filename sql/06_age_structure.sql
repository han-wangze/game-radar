-- sql/06_age_structure.sql
-- 年龄结构：每个市场当日在榜产品的「上架时长」构成。
--
-- 口径：age_days = 快照日 − 该店面上架日（dim_app_country.release_date，分店面）。
--   四档：0–30 / 31–90 / 91–365 / 365+；另有预注册（age_days<0，已排除在分母外）
--   与取不到上架日的（age_days IS NULL，同样排除在分母外，单独在 n_all 与样本差异里体现）。
--
-- 为什么看结构不看均值：
--   榜单里有上架 6,400 多天的产品，均值会被极值拉偏，所以只比形状，不比绝对天数。
--
-- ⚠️ 它能回答什么、不能回答什么（2026-11-04 补）：
--   能：「当天在榜的产品里，有多少是最近 N 天上架的」——这是**结构**。
--   不能：「这个市场换代快不快 / 是新血不断还是存量把持」——那是**时间维度**，
--        要看进榜频率与留存，单日截面答不了。一个市场可以某天有 17 个新品在榜，
--        之后两周一个都没进。
--   所以不要用本文件的输出说「换血 / 沉淀 / 存量市场」，只能说「30 天内的占比是 X%」。
--
-- ⚠️ 跨市场只比占比，不比中位数：
--   releaseDate 是分店面口径，且上架地区变化时可能被重置，中位数跨市场不可比。

WITH valid AS (
    SELECT snap_date, country, age_days
      FROM v_rank_sub
     WHERE age_days >= 0                     -- 排除预注册（未来日期）
),
lab AS (
    SELECT snap_date, country, age_days,
           CASE WHEN age_days <= 30  THEN 1
                WHEN age_days <= 90  THEN 2
                WHEN age_days <= 365 THEN 3
                ELSE 4 END AS bucket_no,
           CASE WHEN age_days <= 30  THEN '0-30 天'
                WHEN age_days <= 90  THEN '31-90 天'
                WHEN age_days <= 365 THEN '91-365 天'
                ELSE '365+ 天' END AS bucket
      FROM valid
),
agg AS (
    SELECT snap_date, country, bucket_no, bucket, COUNT(*) AS n
      FROM lab
     GROUP BY snap_date, country, bucket_no, bucket
),
tot AS (
    SELECT snap_date, country, COUNT(*) AS n_all
      FROM valid
     GROUP BY snap_date, country
),
dist AS (
    SELECT snap_date, country, age_days,
           CUME_DIST() OVER (PARTITION BY snap_date, country ORDER BY age_days) AS cd
      FROM valid
),
med AS (
    SELECT snap_date, country,
           MAX(CASE WHEN cd <= 0.25 THEN age_days END) AS p25_age,
           MAX(CASE WHEN cd <= 0.50 THEN age_days END) AS median_age,
           MAX(CASE WHEN cd <= 0.75 THEN age_days END) AS p75_age
      FROM dist
     GROUP BY snap_date, country
)
SELECT a.snap_date,
       a.country,
       a.bucket,
       a.n,
       ROUND(1.0 * a.n / t.n_all, 3) AS share,
       t.n_all,
       m.p25_age,
       m.median_age,
       m.p75_age
  FROM agg a
  JOIN tot t ON t.snap_date = a.snap_date AND t.country = a.country
  JOIN med m ON m.snap_date = a.snap_date AND m.country = a.country
 ORDER BY a.snap_date DESC, a.country, a.bucket_no;
