-- sql/07_cohort_survival.sql
-- 进榜 cohort 的留榜情况：固定暴露时长，两种分母、两个口径并列。
--
-- 为什么必须换掉报告 §2 的「51.3%」
--   那是「累计进榜里到某天仍在榜的比例」，把「10-24 进榜、有 6 天可以掉榜」和
--   「11-04 进榜、0 天可以掉榜」混进一个分母，末期批次必然被算作仍在榜 → 系统性偏高。
--
-- 为什么要两个口径（本查询最容易做错的地方）
--   只做「第 k 天当天在不在榜」会得到**非单调**的曲线：实测 10-24 那批
--   D+5 是 27%、D+7 反跳到 57%。不是算错——是产品掉榜后又回来了。
--   ⇒ 「第 k 天在榜」测的是**时点状态**，不是留存，不能画衰减曲线。
--   ⇒ 留榜曲线必须要求「从进榜日起**连续**在榜」，那才必然单调。
--   两个都出：p_continuous 是曲线，p_point 是时点，n_back 量化「回锅」有多少。
--
-- 为什么要两种分母
--   k 越大，能观察到它的 cohort 越少（越早进榜的才够天数）。用各自的 at-risk 分母时，
--   不同 k 的分子分母都在换，横向比较会被 cohort 构成差异污染。
--   ⇒ basis='fixed7' 只取「进榜后至少能观察 7 天」的那批，k=1..7 共用同一个分母，
--      **这条才是可以直接连成曲线的**；
--   ⇒ basis='all' 是每个 k 各自的最大样本，样本量大但构成会变，只作参考。
--
-- 两种删失
--   左删失：面板起点（10-23）就在榜的产品不是「进榜」，v_new_entrant 已排除。
--   右删失：进榜日 + k 天超出面板末日的记录没有观测到，从分母剔除（不能当掉榜）。
--
-- 输出列
--   basis        fixed7 / all
--   n_at_risk    该 k 下的分母
--   p_continuous 连续 k+1 天都在榜的比例（**衰减曲线用这个**，单调）
--   p_point      第 k 天当天在榜的比例（允许中间掉榜再回来，非单调）
--   n_back       在 D+k 仍在榜、但中间掉过榜的条数
--   n_censored   因右删失被剔除的条数

WITH p AS (
    SELECT MIN(snap_date) AS panel_start,
           -- 收口到 :cut（verify_numbers 传入）才能和报告中「14 面板定版」对齐；
           -- 独立跑 run_sql 时不传 :cut，则回退到库内最新日（当前面板）。
           COALESCE(:cut, (SELECT MAX(snap_date) FROM fact_rank)) AS panel_end
      FROM fact_rank
),
b AS (
    SELECT country, app_id, snap_date,
           ROW_NUMBER() OVER (PARTITION BY country, app_id ORDER BY snap_date) AS rn
      FROM fact_rank
),
isl AS (
    SELECT country, app_id, snap_date,
           CAST(julianday(snap_date) AS INTEGER) - rn AS island
      FROM b
),
runs AS (
    -- 连续在榜的每一段；只关心「从进榜那天开始」那一段的长度
    SELECT country, app_id, island, MIN(snap_date) AS run_start, COUNT(*) AS run_len
      FROM isl
     GROUP BY country, app_id, island
),
ne AS (
    SELECT n.country, n.app_id, n.entry_date, COALESCE(r.run_len, 1) AS entry_run_len,
           (SELECT panel_end FROM p) AS panel_end
      FROM v_new_entrant n
      LEFT JOIN runs r
             ON r.country = n.country AND r.app_id = n.app_id AND r.run_start = n.entry_date
),
ks(k) AS (VALUES (1), (2), (3), (5), (7), (10), (14)),
grid AS (
    SELECT ne.country, ne.app_id, ne.entry_date, ne.entry_run_len, ne.panel_end, ks.k,
           date(ne.entry_date, '+' || ks.k || ' day') AS target,
           -- 这批 cohort 能不能观察到 D+7：决定它进不进 fixed7 组
           (date(ne.entry_date, '+7 day') <= ne.panel_end) AS in_fixed7
      FROM ne, ks
),
calc AS (
    SELECT g.*,
           (g.target <= g.panel_end) AS observable,
           (g.entry_run_len >= g.k + 1) AS still_continuous,
           EXISTS (SELECT 1 FROM fact_rank f
                    WHERE f.snap_date = g.target
                      AND f.country  = g.country
                      AND f.app_id   = g.app_id) AS in_rank
      FROM grid g
),
agg AS (
    SELECT 'fixed7' AS basis, k AS days_since_entry,
           SUM(observable)                                    AS n_at_risk,
           SUM(observable AND still_continuous)                AS n_continuous,
           ROUND(1.0 * SUM(observable AND still_continuous)
                 / NULLIF(SUM(observable), 0), 3)              AS p_continuous,
           SUM(observable AND in_rank)                         AS n_point,
           ROUND(1.0 * SUM(observable AND in_rank)
                 / NULLIF(SUM(observable), 0), 3)              AS p_point,
           SUM(observable AND in_rank AND NOT still_continuous) AS n_back,
           SUM(NOT observable)                                 AS n_censored
      FROM calc WHERE in_fixed7 AND k <= 7 GROUP BY k
    UNION ALL
    SELECT 'all', k,
           SUM(observable), SUM(observable AND still_continuous),
           ROUND(1.0 * SUM(observable AND still_continuous) / NULLIF(SUM(observable), 0), 3),
           SUM(observable AND in_rank),
           ROUND(1.0 * SUM(observable AND in_rank) / NULLIF(SUM(observable), 0), 3),
           SUM(observable AND in_rank AND NOT still_continuous),
           SUM(NOT observable)
      FROM calc GROUP BY k
)
SELECT basis, days_since_entry, n_at_risk, n_continuous, p_continuous,
       n_point, p_point, n_back, n_censored
  FROM agg
 ORDER BY basis, days_since_entry;
