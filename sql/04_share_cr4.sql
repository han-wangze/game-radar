-- sql/04_share_cr4.sql
-- 品类份额与集中度：**两种口径同时出，但它们的分量完全不同，不是并列关系**。
--
--   口径① 榜位份额 occupancy_share  ← 唯一可用于结论的口径
--     分母 = 该国当日 Top100 的总榜位数（=100）
--     含义 = 这个品类占了多少「曝光位置」。看的是品类在货架上的存在感。
--     适用 = 「哪个品类在榜单上更活跃」。干净，不依赖任何代理指标。
--
--   口径② 评分数份额 rating_share  ← **已降级：仅描述，不作体量/规模解释**
--     分母 = 该国当日 100 个产品的 rating_count 之和
--     含义 = 这个品类占了多少「累计评分人数」。
--     ⚠️ 它不是收入、不是用户数、不是下载量、不是「钱和用户堆在哪」。
--        且与上架时长高度共线（实测 Block Out! 上架 367 天、日均 945 条，
--        Candy Crush 上架 5,101 天、日均 792 条）——跨品类比较时，
--        它主要测出的是「谁的产品更老」，不是「谁的规模更大」。
--        评分转化率还按品类聚集（休闲类弹窗频繁、硬核类几乎不打扰），
--        这是系统性偏差，不是噪声。
--     适用 = 只在**同一年龄档内部**做比较；不用于跨品类体量排名、
--            不用于「最大品类是谁」这类结论。报告里须写「累计评价份额」，
--            不得简写为「体量」。
--
-- 两者会不一致，这是正常的：一个品类可以占 43% 榜位却只占 28% 累计评价。
-- 但**不要把这个差额当成「体量大/小」的证据**——差额的主要来源是年龄结构，
-- 而不是单品规模。要判别，必须控制 age_days 后重算。
--
-- CR4 用的是口径②（品类内评分数前 4 名占该品类评分数之和）。
-- ⚠️ 因此这个 CR4 测的是「品类内头部产品之间的年龄断层」，**不是竞争集中度**：
--    老产品扎堆的品类，CR4 天然接近 1，与「是不是被寡头锁死」无关。
--    只在同一批次、同一品类内部做纵向对比时可用；不跨品类比。
-- Unclassified 单列，不参与品类排名。

WITH d AS (
    SELECT country,
           snap_date,
           sub_genre,
           rating_count,
           ROW_NUMBER() OVER (PARTITION BY country, snap_date, sub_genre
                              ORDER BY rating_count DESC) AS rn_in_genre
      FROM v_rank_sub
),
tot AS (   -- 每日每国的两个分母
    SELECT country,
           snap_date,
           COUNT(*)               AS n_slots,
           SUM(rating_count)      AS sum_rating
      FROM v_rank_sub
     GROUP BY country, snap_date
),
agg AS (
    SELECT country,
           snap_date,
           sub_genre,
           COUNT(*)                                        AS n_apps,
           SUM(rating_count)                               AS sum_rating,
           SUM(CASE WHEN rn_in_genre <= 4 THEN rating_count ELSE 0 END) AS top4_rating
      FROM d
     GROUP BY country, snap_date, sub_genre
)
SELECT a.snap_date,
       a.country,
       a.sub_genre,
       a.n_apps,
       ROUND(1.0 * a.n_apps / t.n_slots, 4)                                  AS occupancy_share,
       ROUND(1.0 * a.sum_rating / t.sum_rating, 4)                           AS rating_share,
       ROUND(1.0 * a.top4_rating / a.sum_rating, 4)                          AS cr4_in_genre,
       a.sum_rating                                                          AS genre_rating_total
  FROM agg a
  JOIN tot t ON t.country = a.country AND t.snap_date = a.snap_date
 WHERE a.sub_genre <> 'Unclassified'
 ORDER BY a.snap_date DESC, a.country, a.n_apps DESC;
