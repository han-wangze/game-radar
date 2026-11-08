-- sql/05_quantile.sql
-- 按评分数四分层，看头/腰/尾各自长什么样。
--
-- ⚠️ 2026-11-04 降级：本查询的结论已从报告正文移入附录 C。
--    原因：评分数与上架时长高度共线（评分数少 ≈ 上架时间短），所以「尾部」大概率就是「新品」。
--    这层关系测出来的很可能是「新品榜位好」，而不是「评分数对榜位的作用」——混淆变量没控制。
--    要用的话，必须先按 age_days 分档，再在档内分位。否则不要引用本文件的任何结论。
--
-- ⚠️ 命名说明：Q1 头部 / Q4 尾部 只是分位命名，不代表市场地位或公司规模。
--
-- 为什么用 NTILE 而不是手写阈值：
--   评分数跨 3 个数量级（从 0 到 2000 万），绝对阈值（如「10 万以上算头部」）在
--   us 和 kr 之间不可比。分位数把每个市场各自切成四段，比的是「结构」而不是绝对值。
--
-- 输出每层：产品数、平均榜位、最好榜位、Top10 占位率、品类数。
--   top10_rate  = 该层里排进当日前 10 名的产品数 / 该层产品数
--   genre_n     = 该层覆盖多少个不同品类（看层内是否集中）

WITH q AS (
    SELECT country,
           snap_date,
           sub_genre,
           rating_count,
           rank,
           NTILE(4) OVER (PARTITION BY country, snap_date
                          ORDER BY rating_count DESC) AS quartile
      FROM v_rank_sub
)
SELECT country,
       snap_date,
       quartile,
       CASE quartile WHEN 1 THEN 'Q1 头部'
                     WHEN 2 THEN 'Q2 上腰'
                     WHEN 3 THEN 'Q3 下腰'
                     ELSE 'Q4 尾部' END            AS layer,
       COUNT(*)                                    AS n_apps,
       ROUND(AVG(rank), 1)                         AS avg_rank,
       MIN(rank)                                   AS best_rank,
       ROUND(1.0 * SUM(CASE WHEN rank <= 10 THEN 1 ELSE 0 END) / COUNT(*), 3) AS top10_rate,
       COUNT(DISTINCT sub_genre)                   AS genre_n
  FROM q
 GROUP BY country, snap_date, quartile
 ORDER BY snap_date DESC, country, quartile;
