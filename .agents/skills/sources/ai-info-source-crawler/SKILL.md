---
name: ai-info-source-crawler
description: 抓取配置中的 AI 媒体、研究资讯源和具身智能社区来源，在前一个完整 Asia/Shanghai 自然日窗口内按来源 scope 生成 `working_tmp/ai_info_source/` 记录。只要任务提到 AI 资讯源、AI 媒体 RSS、机器之心/PaperWeekly、具身智能之心、新智元、量子位、MIT Technology Review AI、AI Insider、Towards AI，或要求按共识筛选资讯文章，就使用本 skill。
compatibility: 需要 uv、项目根目录 `.venv` 中的 Python 3.10、`feedparser`、`requests`、`python-dotenv` 和网络访问。
---

# AI Info Source Crawler

## 职责

读取同目录 `sources.json` 中配置的来源，按时间窗口和来源 scope 预筛选，输出来源原始标题、摘要、正文、作者、分类、图片、时间语义和可解释的相关性证据。默认配置包含八个来源：五个 RSS、一个 Zhihu 专栏 API、两个 RSS 加 WordPress API 的中文资讯源。所有配置来源的 `category_hint` 都是 `News`，它只是来源提示，不是后续 rating 的最终分类。

本 skill 不逐篇访问文章页，不负责跨来源去重、总结、最终评分、final publication 写入、candidate 整理或发布。

在 phase 中运行时返回结构化进度，不直接写 final publication：开始为 `正在抓取 AI 媒体与社区资讯并执行方向预筛选。`；成功为 `AI 资讯来源抓取完成，共生成 {record_count} 条来源记录。`；部分成功、失败和显式跳过需附 record/error 数及一句短错误。计数以 manifest 的 `record_count` 为准，成功空结果是 0 条成功。进度中不得包含机器之心 token 或带 token URL。

## 来源

- MIT Technology Review Artificial Intelligence；
- AI Insider；
- Towards AI；
- 机器之心官方免费用户 RSS；
- PaperWeekly 今天看啥 RSS。
- 具身智能之心公开 Zhihu 专栏 API；使用 `www.zhihu.com/api/v4/columns/c_1823331372888109056/articles`，按 `limit`/`offset` 做有界分页；
- 新智元（Aiera）RSS 与公开 WordPress posts API；
- 量子位 RSS 与公开 WordPress posts API。

来源清单位于 `.agents/skills/sources/ai-info-source-crawler/sources.json`。标准 RSS/Atom 一般只需新增配置，不修改 Python。每个来源声明：

- `name`、`publisher`、`url`、`homepage`；
- `date_policy: entry`：使用条目 `published`、`updated` 或 `created`；
- `access_mode`：`rss`、`zhihu_column_api` 或 `rss_plus_wordpress_api`；
- `source_role`、`category_hint`：写入 manifest 和 `record.md`，供后续评分使用；配置来源的 `category_hint` 必须为 `News`，它不决定最终 rating 分类；
- `scope`：`consensus` 保留既有直接/二级主题筛选，`broad_news` 保留日期窗口内的新闻条目供后续 rating 判断；
- `api_url`：`rss_plus_wordpress_api` 的公开 WordPress posts API；
- `page_size`、`max_pages`：API 批量大小和每次运行的来源级页数上限；
- `token_env`：可选。需要 token 的 RSS 从指定环境变量读取凭据，并作为请求参数发送；配置、日志和产物只保存无 token 的基础 URL。

## 时间语义

独立运行且未提供窗口参数时，在启动时按 `Asia/Shanghai` 计算前一个完整自然日，并以该日的本地 `00:00` 到下一日 `00:00` 作为 `[since, until)`；由编排调用时使用调用方冻结的精确窗口。

RSS 来源按条目 `published`、`updated` 或 `created` 的精确时间过滤，然后做共识筛选。Zhihu API 使用文章 `created` 或 `updated` 时间；WordPress API 只补充 RSS 已发现条目的标题、摘要和正文，不改变 RSS 的发布日期边界。

机器之心官方免费 RSS 需要 `JIQIZHIXIN_RSS_TOKEN`，且服务端限制每 60 分钟最多请求一次。项目正常每两三天运行一次，不需要为了探测状态额外请求。推荐复制根目录 `.env.example` 为 `.env`，然后只在本机填写：

```dotenv
JIQIZHIXIN_RSS_TOKEN=<your token>
```

脚本自动读取仓库根目录 `.env`；如果进程环境中已经设置同名变量，则进程环境优先。`.env` 已加入 `.gitignore`。不要把 token 写入 `sources.json`、命令行参数、manifest、`record.md` 或日志。变量缺失或机器之心返回 HTTP 401 时，只将机器之心来源标记为 `partial` 且不生成记录，其他来源继续。tokenized request 不重试。

PaperWeekly 替代 feed 提供逐条发布时间、摘要和 `content:encoded` 全文，因此不再依赖微信原帖页面或快照时间。

## 共识筛选

`scope: consensus` 时读取仓库根目录 `consensus.md` 的“关键词组”和“中文资讯别名”。中文别名映射到既有主题组，但不进入 arXiv API 查询。中英文关键词使用同一规则：

- `embodied_robotics`、`arm_vla`、`drone_vla_vln`、`embodied_infra`、`world_model` 是直接主题，标题、摘要、feed 正文或分类命中一个关键词即可保留；
- `llm_vlm_methods` 范围较宽，标题命中一个关键词可以保留；若只在摘要、feed 正文或分类命中，则至少需要两个不同关键词；
- 英文缩写使用边界匹配，中文词使用普通文本匹配；
- 每条记录保存 `matched_query_groups`、`matched_keywords` 和 `relevance_reasons`。

`scope: broad_news` 时不因缺少具身智能关键词丢弃新闻条目；仍执行日期窗口过滤，并保存实际关键词命中（可以为空）和 `broad_news` 保留理由，交由后续 rating 判断。默认未配置 scope 的自定义来源仍使用 `consensus`，因此既有直接主题单关键词和二级主题规则不变。

筛选只基于 feed 已提供内容，不额外打开文章页。新智元和量子位只通过有限的 WordPress `include` 批量请求补全 RSS 已发现条目，不逐篇访问文章页。短标题镜像可能因信息不足漏筛，manifest 和记录状态会明确暴露这一限制。

## 运行

```bash
uv sync --locked

uv run python \
  .agents/skills/sources/ai-info-source-crawler/scripts/crawl_ai_info_sources.py
```

固定窗口：

```bash
uv run python \
  .agents/skills/sources/ai-info-source-crawler/scripts/crawl_ai_info_sources.py \
  --since 2026-07-31T16:00:00+00:00 \
  --until 2026-08-01T16:00:00+00:00 \
  --cycle-id daily-2026-08-01 \
  --output-dir /tmp/colab-daily-ai-info
```

Dry-run：

```bash
uv run python \
  .agents/skills/sources/ai-info-source-crawler/scripts/crawl_ai_info_sources.py \
  --dry-run
```

安全跳过受限来源（不会对该来源创建 HTTP session 或发出请求）：

```bash
uv run python \
  .agents/skills/sources/ai-info-source-crawler/scripts/crawl_ai_info_sources.py \
  --skip-source jiqizhixin_official
```

参数：

- `--sources`：默认 skill 目录的 `sources.json`；
- `--consensus`：默认仓库根目录 `consensus.md`；
- `--output-dir`：默认仓库 `working_tmp`；
- `--since`、`--until`：带时区的显式窗口；
- `--cycle-id`：可选稳定周期 ID；独立运行可省略并在 manifest 中写入 `null`，生产调用必须传入非空冻结 CycleID。
- `--days`：显式指定滚动时长；未显式传入时不参与默认窗口计算；
- `--max-entries-per-source`：每个 feed 最多解析的条目数，默认 100；
- `--max-api-pages`：每个 API 来源最多读取的页数，默认 2；
- `--max-article-fetches`：每次运行最多通过 WordPress API 补全的文章数，默认 10；
- `--skip-source`：按配置 `name` 显式跳过来源，可重复传入；未知名称直接报错；
- `--dry-run`：不创建或修改输出；
- `--verbose`：详细日志。

来源并行请求。普通 RSS 每个来源至少发一个 feed 请求；Zhihu API 和 WordPress API 请求使用非重试 HTTP session，并受 `--max-api-pages`、`--max-article-fetches` 以及来源配置的 `page_size`、`max_pages` 共同限制。WordPress API 使用 `include` 批量补全文本，不逐篇打开文章页。`fetched_item_count` 是解析的条目数，不是网页请求数。manifest 和每个 source report 分别记录 `http_request_count`、`feed_request_count`、`api_request_count`、`page_fetch_count` 和 `article_fetch_count`。机器之心受 60 分钟限制，缺 token 或 HTTP 401 时不生成内容，只标记为非阻断 `partial`；tokenized request 禁用底层自动重试，一次运行最多发一个请求。

## 输出契约

```text
working_tmp/
└── ai_info_source/
    ├── crawl_manifest.json
    └── <SOURCE_NAME>--<URL_HASH>/
        └── record.md
```

`record.md` 至少包含：

```yaml
source: ai_info_source
source_name: "mit_technology_review_ai"
publisher: "MIT Technology Review"
source_id: "mit_technology_review_ai:..."
title: "..."
authors: ["..."]
published_at: "2026-08-01T10:15:19+00:00"
listed_at: "2026-08-01T10:15:19+00:00"
date_basis: "entry_published"
retrieved_at: "..."
window_since: "2026-07-31T16:00:00+00:00"
window_until: "2026-08-01T16:00:00+00:00"
url: "..."
feed_url: "..."
homepage_url: "..."
categories: ["..."]
image_urls: ["..."]
matched_query_groups: ["llm_vlm_methods"]
matched_keywords: ["LLM"]
relevance_reasons: ["keyword in title (llm_vlm_methods): LLM"]
status: complete
attachments: []
errors: []
```

正文保存 feed 原始摘要和可用的 feed 正文，不翻译、不总结。

`crawl_manifest.json` 显式包含 `cycle_id`（独立运行未传 `--cycle-id` 时为 `null`），并保存窗口、共识路径、八个来源状态、HTTP 请求分项、每源读取条目数、有日期条目数、窗口内条目数、相关条目数、最终记录数、不完整记录数、feed 更新时间、错误和记录目录。生产调用回读时必须验证 CycleID 与冻结输入一致。只提供摘要而没有正文的记录显示 `content_status: summary_only` 和 `status: partial`；显式跳过的来源显示为 `skipped`、请求数为零，整体运行显示为 `partial`。

## 重复执行与失败

- 稳定身份基于来源名和文章 URL 的 SHA-256 前 16 位；重复运行覆盖同一 crawler 产物，不创建重复目录。
- 单个 feed 失败不阻断其他来源；manifest 标记 `partial`。
- 所有 feed 失败时返回失败；请求成功但窗口内无条目或无相关条目属于成功空结果。
- 机器之心 token 缺失或 HTTP 401 时该来源为 `partial`、请求数为零或一且不阻断其他来源；其他 tokenized HTTP 错误也不重试并保留脱敏错误。
- 不清空 `working_tmp`，不写 final publication，不修改正式站点内容，不调用后续 phase。

## 验收

1. `uv sync --locked` 后可直接运行。
2. 八个来源都有明确状态且 `category_hint` 均为 `News`；RSS 请求、API 分页和 WordPress 批量补全都受上限约束。
3. 精确日期 feed 的输出位于 `[since, until)`。
4. 机器之心 token 仅从进程环境或根目录 `.env` 读取，不出现在配置和任何输出产物中；测试使用 mock，不调用 live RSS；缺失或 HTTP 401 为非阻断 `partial` 且不生成记录。
5. `consensus` 来源输出命中 `consensus.md` 并保存匹配证据；`broad_news` 来源保存日期窗口内新闻，供后续 rating 筛选。
6. `--dry-run` 不创建文件，重复运行目录稳定。
