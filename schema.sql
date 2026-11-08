-- game-radar schema v0.1
-- 设计原则：事实表按「榜日 + 国家」存快照，维度表只存最新值，日指标表存可做差分的累计量。
-- rank 不可用时留空，不伪造。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 产品维度（最新值；first_seen 一旦写入不再改）
CREATE TABLE IF NOT EXISTS dim_app (
    app_id        TEXT PRIMARY KEY,          -- Apple trackId（字符串，避免前导零丢失）
    bundle_id     TEXT,
    name          TEXT NOT NULL,
    seller        TEXT,
    genre         TEXT,                      -- primaryGenreName（子品类，如 Puzzle / Strategy）
    genres_all    TEXT,                      -- JSON 数组
    release_date  TEXT,                      -- ⚠️ 旧字段：分店面值的「最后写入者」，已由 dim_app_country 取代，勿用
    ver_date      TEXT,                      -- 当前版本发布日期 YYYY-MM-DD
    rating_avg    REAL,
    rating_count  INTEGER,                   -- 累计评价数（差分 = 新增评价）
    price         REAL,
    currency      TEXT,
    summary       TEXT,                      -- 榜单接口自带的一句话简介
    -- ⚠️ descr 已**废弃**（2026-11-05）：它曾是「最后写入国」的那一份，语言不可控、
    --    且会随 app 更新而变，导致 direct 级证据悬空。现由 dim_app_country.descr 取代。
    --    本列不再写入（冻结在迁移前的值），**打标、校验、报告都不要读它**。
    descr         TEXT,                      -- 【废弃】商店描述，最后写入国胜；改用 dim_app_country.descr
    first_seen    TEXT,                      -- 首次进榜日（自建字段）
    last_seen     TEXT                       -- 最近一次在榜日
);

-- 榜单事实（每日每国一份；同日重跑覆盖）
CREATE TABLE IF NOT EXISTS fact_rank (
    snap_date  TEXT NOT NULL,                -- UTC 日期 YYYY-MM-DD
    country    TEXT NOT NULL,
    chart      TEXT NOT NULL,                -- 目前只有 top-free
    rank       INTEGER NOT NULL,             -- 1..100
    app_id     TEXT NOT NULL,
    PRIMARY KEY (snap_date, country, chart, rank)
);
CREATE INDEX IF NOT EXISTS idx_rank_app ON fact_rank(app_id, country, snap_date);

-- 产品 × 店面维度（分店面字段的正确归属地）
-- 为什么需要这张表：Apple 的 releaseDate / userRatingCount / price 都是**分店面**口径，
-- 同一 app 在四国返回不同值（实测 Police Chief：us 2026-05-08 / kr 2026-09-18 / jp 2026-10-23）。
-- 这类字段不能放在「一产品一行」的 dim_app 里 —— 那样只能存最后抓取国的值，跨市场分析必串味。
-- dim_app 只保留与店面无关的字段：bundle_id / seller / 全局 first_seen。
-- （descr 严格说不属于这一类——它是本地化的，但暂留此处，见该列的注释。）
CREATE TABLE IF NOT EXISTS dim_app_country (
    app_id       TEXT NOT NULL,
    country      TEXT NOT NULL,
    release_date TEXT,                       -- 该店面口径的上架日
    ver_date     TEXT,                       -- 该店面当前版本发布日
    name         TEXT,                       -- 该店面返回的名称（可能是本地化名，如 us 店面的韩文名）
    genre        TEXT,
    genres_all   TEXT,
    rating_count INTEGER,                    -- 该店面最近一次抓到的累计评价数（差分仍用 fact_metric）
    rating_avg   REAL,
    price        REAL,
    currency     TEXT,
    -- 2026-11-05 从 dim_app 下移：描述与截图都是分店面口径。
    -- 实测 Pokémon GO：us 英文 1,755 字符 / kr 韩文 1,773 / jp 日文 989，三份 sha1 全不同。
    descr        TEXT,                       -- 该店面的商店完整描述（打标与 direct 校验的唯一来源）
    screenshots  TEXT,                       -- JSON 数组，该店面的截图 URL（lookup 的 screenshotUrls）
    descr_sha1   TEXT,                       -- 上面这份描述的 sha1 前 12 位，用于判断「文本是否变过」
    first_seen   TEXT,                       -- 该店面的首次在榜日
    last_seen    TEXT,
    PRIMARY KEY (app_id, country)
);
CREATE INDEX IF NOT EXISTS idx_appcc_country ON dim_app_country(country, app_id);

-- 每日指标快照（用于差分：新增评价数）
-- ⚠️ 必须带 country：评分数与评分是**分店面口径**，同一个 app 在 us 和 jp 的
--    userRatingCount 完全不同，跨国家不能用同一个值。
CREATE TABLE IF NOT EXISTS fact_metric (
    snap_date    TEXT NOT NULL,
    country      TEXT NOT NULL,
    app_id       TEXT NOT NULL,
    rating_count INTEGER,
    rating_avg   REAL,
    price        REAL,
    PRIMARY KEY (snap_date, country, app_id)
);

-- ════════════════════════════════════════════════════════════════════
-- 标签体系（2026-10-30）：定义 / 值 / 抽检 三张表
-- ════════════════════════════════════════════════════════════════════
-- 为什么换掉原来的宽表 dim_tag（5 个固定列 category/theme/core/art/monet）：
--   ① 没有定义层：没人知道合法值域是什么、谁定的、什么时候改过；
--   ② 没有抽检：标签对不对没有任何记录，准确率无从谈起；
--   ③ 把两个正交的轴压成一列：RPG/SLG 是**品类**，merge/sort 是**玩法机制**。
--      依据 Newzoo Games Taxonomy（见 refs/newzoo_taxonomy.md），它是四轴分离的：
--      Subgenre / Mechanics / Themes / Art Styles。「喵骑战域」既是 Role Playing
--      又是 idle-shooter，本来就不该挤在同一个格子里。
DROP TABLE IF EXISTS dim_tag;

-- 标签字典：一行 = 一个标签键的定义。这就是"标签数据字典"这个交付物本身。
-- value_domain 为 NULL 表示**开集**（自由文本，只限长度）；非 NULL 表示**闭集**（JSON 数组，逐值校验）。
CREATE TABLE IF NOT EXISTS dim_tag_def (
    tag_key      TEXT PRIMARY KEY,           -- subgenre / play_desc / like_of / theme / art
    name_cn      TEXT NOT NULL,
    definition   TEXT NOT NULL,              -- 判定标准：什么算、什么不算、边界案例
    value_domain TEXT,                       -- JSON 数组 = 闭集；NULL = 自由文本（开集）
    max_len      INTEGER,                    -- 开集的长度上限（闭集为 NULL）
    source       TEXT,                       -- 值域依据：newzoo / 自定 / —
    multi_value  INTEGER NOT NULL DEFAULT 0, -- v3 起全部为 0：复合玩法写进 play_desc
    ver          INTEGER NOT NULL DEFAULT 1,
    changed_at   TEXT,
    note         TEXT
);

-- 标签值。tag_ver 对应 dim_tag_def.ver，规则改版后旧标签不会被静默覆盖。
-- evidence 存描述里的原文片段：抽检时先看它，再决定对不对；direct 级的必须能在 descr 中找到。
-- 主键 (app_id, tag_key)：v3 起所有标签键都是单值，重打即覆盖，同一产品同一键不会有两行。
--   （复合玩法不再靠多值表达 —— 它属于 play_desc 那一句话。）
CREATE TABLE IF NOT EXISTS fact_tag (
    app_id      TEXT NOT NULL,
    tag_key     TEXT NOT NULL,
    value       TEXT NOT NULL,
    -- confidence 保留但**不对外使用**：它是模型自评、无外部基准、不可对账，
    -- 在跑完抽检分箱验证前不进任何视图/页面/报告。要判断"这条多可靠"请看下面两列。
    confidence  REAL,
    evidence_strength TEXT,                  -- direct(原文直述) / inferred(间接推断) / none(无依据)
    method      TEXT,                        -- llm / rule / human
    model       TEXT,
    tag_ver     INTEGER,
    evidence    TEXT,
    -- 证据对着哪份文本（2026-11-05 加）：只存 evidence 不够，文本一改就无法复核。
    -- descr_country = 打标时用的店面（优先 us，其次该产品在榜的国家）；
    -- descr_sha1    = 当时 dim_app_country.descr 的指纹。校验时两者不一致 ⇒ 文本已变，
    --                 不能判「当初写错」，只能提示「需对着新文本复核」。
    descr_country TEXT,
    descr_sha1    TEXT,
    need_review INTEGER DEFAULT 0,           -- 1 = 需要人看一眼（证据不足 / 边界案例 / 值域外）
    tagged_at   TEXT,
    PRIMARY KEY (app_id, tag_key)
);

-- 抽检记录：一行 = 一次人工核对。准确率 = correct / 总数；unsure 单独报。
CREATE TABLE IF NOT EXISTS fact_tag_review (
    review_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    batch       TEXT NOT NULL,               -- 批次，如 20261030-A
    app_id      TEXT NOT NULL,
    tag_key     TEXT NOT NULL,
    value       TEXT NOT NULL,               -- 机器给出的值
    verdict     TEXT NOT NULL,               -- correct / wrong / unsure
    human_value TEXT,                        -- verdict=wrong 时人工给的正确值
    reviewer    TEXT,
    reviewed_at TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tag_review_batch ON fact_tag_review(batch, tag_key);

-- 品类字典：把四国本地化的子品类名映射到统一英文规范名
-- 种子见 scripts/seed_genre_map.py；未收录的字符串会在该脚本里被报出来
CREATE TABLE IF NOT EXISTS dim_genre_map (
    raw        TEXT PRIMARY KEY,             -- 原始字符串（中/日/韩/英）
    canonical  TEXT NOT NULL,                -- 规范名（英文）
    is_top     INTEGER DEFAULT 0             -- 1 = 顶级品类（Games/Entertainment），0 = 子品类
);

-- 运行日志（缺日审计 + 断点依据）
CREATE TABLE IF NOT EXISTS job_log (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT NOT NULL,               -- 本地时间 ISO
    snap_date   TEXT NOT NULL,               -- UTC 日期
    country     TEXT,
    chart       TEXT,
    status      TEXT NOT NULL,               -- ok / partial / fail
    n_rank      INTEGER DEFAULT 0,
    n_detail    INTEGER DEFAULT 0,
    http_code   INTEGER,
    duration_s  REAL,
    note        TEXT
);

-- 便捷视图：把排名、产品、子品类、上线天数拼好
-- age_days / rating_count / price 全部取**该店面**的值（dim_app_country），
-- 不再用 dim_app 上的同名旧字段（那是最后写入国的值，跨市场会串味）。
-- age_days 为 NULL = 该 (产品, 店面) 没取到上架日（App 在该店面不可用，或还没回填）。
-- age_days < 0 表示尚未上线／预注册。
--
-- sub_genre 的取值规则（2026-10-30 显式化；此前是隐式且错的）：
--   旧写法 `... WHERE m.raw IN (SELECT value FROM json_each(...)) LIMIT 1` 没有 ORDER BY，
--   SQLite 按 dim_genre_map 主键（raw）扫描 ⇒ 实际取的是「raw 字典序最小的命中项」，
--   与 Apple 给的品类顺序无关，同一产品在不同店面会拿到不同标签。
--   现改为：按 **genres 数组顺序**取第一个非顶级类命中项（ORDER BY j.key）。
--   为什么不用 primaryGenreName：实测 548 条 dim_app_country 里 547 条是 "Games"（顶级类），
--   对子品类没有信息量。
--   为什么会有 'Unclassified'：实测 62 行榜数据的 genres_all 只有 ["Games"]/["游戏"]/["게임"]/["ゲーム"]，
--   Apple 本身没返回子品类 —— 这是**上游数据缺失**，不是字典缺条目，必须单列统计而不是留 NULL。
--
-- 视图统一「先 DROP 再 CREATE」：CREATE VIEW IF NOT EXISTS 对已存在的视图会**静默跳过**，
-- 改完定义却忘了手工迁移时，库里还是旧视图 —— 2026-10-27 踩过（回填完 dim_app_country 后
-- age_days 仍是旧值，因为 v_rank_sub 根本没被替换）。本文件要能当迁移脚本重复执行。
DROP VIEW IF EXISTS v_rank_sub;
CREATE VIEW v_rank_sub AS
SELECT r.snap_date,
       r.country,
       r.chart,
       r.rank,
       r.app_id,
       a.name,
       ac.release_date,
       CAST(julianday(r.snap_date) - julianday(ac.release_date) AS INTEGER) AS age_days,
       ac.rating_count,
       ac.rating_avg,
       ac.price,
       COALESCE(
           -- ① 第一优先：具体玩法子类（is_top=0 且非 Casino），按 Apple 给的数组顺序取第一个。
           --   疯狂消方块[娱乐场,益智解谜] → Puzzle、解救猪猪[娱乐场,桌面] → Board、
           --   pachislo[カジノ,シミュレーション] → Simulation 都走这里。
           (SELECT m.canonical
              FROM json_each(COALESCE(ac.genres_all, a.genres_all)) j
              JOIN dim_genre_map m ON m.raw = j.value AND m.is_top = 0
             WHERE m.canonical <> 'Casino'
             ORDER BY j.key
             LIMIT 1),
           -- ② 没有具体子类时，才退到 Casual（休闲）。
           --   仅用于 Apple 中国区把消除/找茬类错标成「娱乐场」(Casino) 且同时带「休闲」的场景
           --   （找茬我最强、鱼乐达人 等）；休闲 is_top=1，① 里不会被选中，所以单独放一层。
           --   注意：带具体子类的游戏（如 [休闲,动作]）已被 ① 取到动作，不会掉到这层。
           (SELECT m.canonical
              FROM json_each(COALESCE(ac.genres_all, a.genres_all)) j
              JOIN dim_genre_map m ON m.raw = j.value AND m.canonical = 'Casual'
             ORDER BY j.key
             LIMIT 1),
           -- ③ 兜底才用 Casino：真赌场（Bruno Reels Casino 等）genres_all 只有 [Games, Casino]，
           --   ① ② 都取不到，这里拿到 Casino；既不是真赌场、又没有任何子类的极个别游戏也会落到这，
           --   由人工审计（见 方块我最强 的 genres_all 修正）。
           (SELECT m.canonical
              FROM json_each(COALESCE(ac.genres_all, a.genres_all)) j
              JOIN dim_genre_map m ON m.raw = j.value AND m.is_top = 0
             ORDER BY j.key
             LIMIT 1),
           'Unclassified') AS sub_genre
FROM fact_rank r
JOIN dim_app a USING (app_id)
LEFT JOIN dim_app_country ac
       ON ac.app_id = r.app_id AND ac.country = r.country;

-- 评分数的日差分视图：增长代理的唯一正确入口
-- 为什么要 JOIN fact_rank：fact_metric 粒度比 fact_rank 宽，同日两次抓取只覆盖 fact_rank，
-- 早班快照里掉出终榜的 app 会残留在 fact_metric（实测 157 行）→ 不 join 会把不在榜的产品算进差分。
-- 为什么要有 days_gap：产品中途掉榜时 LAG 的上一行不是「昨天」，delta 会跨多天。
--
-- ⚠️ delta_1d 这个名字是错的，它是「两个快照之间的增量」，间隔不是 24 小时。
--    实测每对相邻榜日的真实抓取间隔在 24.0–31.8 小时之间，且当天结束时会被次日凌晨那次重跑覆盖：
--    10-30 在 16:10 被抓过一次（距上一天 13.0h），当晚读到的 Roblox 日增是 3,993；
--    次日 04:12 重跑覆盖后同一天变成 25.1h，同一格数字变成 7,061。
--    ⇒ 引用日增必须同时报 hours_gap；跨产品/跨天比大小前先看间隔是否相近。
--    delta_per_24h 是按线性速率折算的参考值，遇到首发陡增段会高估，不要单独用。
DROP VIEW IF EXISTS v_metric_diff;
CREATE VIEW v_metric_diff AS
WITH cap AS (
    -- 每个榜日每个店面的**最后一次**抓取时刻。同日双跑时后者覆盖前者，所以取 max。
    SELECT snap_date, country, MAX(run_at) AS captured_at
      FROM job_log
     GROUP BY snap_date, country
),
m AS (
    SELECT f.snap_date, f.country, f.app_id, f.rating_count, cap.captured_at
      FROM fact_metric f
      JOIN fact_rank   r ON r.snap_date = f.snap_date
                        AND r.country   = f.country
                        AND r.app_id    = f.app_id
      LEFT JOIN cap ON cap.snap_date = f.snap_date AND cap.country = f.country
)
SELECT snap_date,
       country,
       app_id,
       captured_at,
       rating_count,
       LAG(rating_count)  OVER w AS prev_rating_count,
       LAG(snap_date)     OVER w AS prev_snap_date,
       LAG(captured_at)   OVER w AS prev_captured_at,
       CAST(julianday(snap_date) - julianday(LAG(snap_date) OVER w) AS INTEGER) AS days_gap,
       ROUND((julianday(captured_at) - julianday(LAG(captured_at) OVER w)) * 24, 1) AS hours_gap,
       rating_count - LAG(rating_count) OVER w AS delta_rating_count,
       CASE WHEN julianday(snap_date) - julianday(LAG(snap_date) OVER w) = 1
            THEN rating_count - LAG(rating_count) OVER w END AS delta_1d,
       CASE WHEN julianday(captured_at) - julianday(LAG(captured_at) OVER w) > 0
            THEN ROUND(1.0 * (rating_count - LAG(rating_count) OVER w)
                 / ((julianday(captured_at) - julianday(LAG(captured_at) OVER w)) * 24) * 24, 1)
       END AS delta_per_24h
FROM m
WINDOW w AS (PARTITION BY country, app_id ORDER BY snap_date);

-- ════════════════════════════════════════════════════════════════════════
-- 「新游」的正式口径 = 首次进榜（first entry），不是上架日期
-- ════════════════════════════════════════════════════════════════════════
-- 为什么不用 releaseDate + 30 天：
--   ① releaseDate 是分店面口径（2026-10-27 已迁到 dim_app_country，见上）；
--      即便日期准确了，它测的仍是「上架新」而非「进榜新」——一个上架 200 天、
--      本周才第一次进榜的产品，对「这个月有哪些新游进榜」才是答案
--   ② 美区在榜产品的最小年龄就有 75 天，用 30 天阈值会恒为 0（那不是市场事实，是阈值 artifact）
-- 代价：面板起点当天在榜的产品是**左删失**（早就在榜，不是当天进榜）→ 必须排除。

-- 面板边界：起点日之后才是可观测的「进榜事件」
DROP VIEW IF EXISTS v_panel;
CREATE VIEW v_panel AS
SELECT MIN(snap_date)          AS panel_start,
       MAX(snap_date)          AS panel_end,
       COUNT(DISTINCT snap_date) AS n_days
FROM fact_rank;

-- 每个（国家 × 榜 × 产品）在面板内的首次/最近观察日
-- is_censored = 1 表示「该国家×榜的起点日就在榜」，其 first_seen 不是真实进榜日，不可当新游
-- ⚠️ 边界按「国家 × 榜」各自算，不是全局：以后新增一个国家或一个子品类榜时，
--    那个子集的第一天在榜产品同样是左删失，否则会被整批误判成「新进榜」。
DROP VIEW IF EXISTS v_entry;
CREATE VIEW v_entry AS
SELECT f.country,
       f.chart,
       f.app_id,
       MIN(f.snap_date) AS first_seen,
       MAX(f.snap_date) AS last_seen,
       COUNT(DISTINCT f.snap_date) AS n_days_in,
       (SELECT MIN(x.snap_date) FROM fact_rank x
         WHERE x.country = f.country AND x.chart = f.chart) AS subset_start,
       (MIN(f.snap_date) = (SELECT MIN(x.snap_date) FROM fact_rank x
                             WHERE x.country = f.country AND x.chart = f.chart)) AS is_censored
FROM fact_rank f
GROUP BY f.country, f.chart, f.app_id;

-- 新进榜清单：已排除左删失，可直接当「新游」用
-- age_days 取该店面的上架日（dim_app_country），不再用 dim_app 的旧字段
DROP VIEW IF EXISTS v_new_entrant;
CREATE VIEW v_new_entrant AS
SELECT e.country,
       e.chart,
       e.first_seen AS entry_date,
       CAST(julianday(e.first_seen) - julianday(e.subset_start) AS INTEGER) AS entry_day_no,
       e.app_id,
       r.rank AS entry_rank,          -- 首次被看到时的名次
       a.name,
       a.seller,
       ac.release_date,               -- 该店面口径的上架日
       ac.rating_count,
       CAST(julianday(e.first_seen) - julianday(ac.release_date) AS INTEGER) AS age_days
FROM v_entry e
JOIN dim_app  a  ON a.app_id = e.app_id
LEFT JOIN dim_app_country ac ON ac.app_id = e.app_id AND ac.country = e.country
JOIN fact_rank r ON r.snap_date = e.first_seen
                AND r.country   = e.country
                AND r.chart     = e.chart
                AND r.app_id    = e.app_id
WHERE e.is_censored = 0;
