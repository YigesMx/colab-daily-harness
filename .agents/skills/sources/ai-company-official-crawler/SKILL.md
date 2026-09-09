---
name: ai-company-official-crawler
description: 抓取 OpenAI、Hugging Face、Google AI、Google DeepMind、NVIDIA、Anthropic 和 Meta 的官方新闻、博客、研究与产品发布，在前一个完整 Asia/Shanghai 自然日窗口内生成 `working_tmp/ai_company_official/` 记录。只要任务提到 AI 公司官网动态、官方 RSS、公司博客、Anthropic/Meta/OpenAI/Google/NVIDIA/Hugging Face 新闻汇总，或需要扩充官方来源列表，就使用本 skill；这些公司官方内容不要求先与具身智能强相关。
compatibility: 需要 uv、项目根目录 `.venv` 中的 Python 3.10、`feedparser`、`requests` 和网络访问。
---

# AI Company Official Crawler

## 职责

读取同目录的 `sources.json`，抓取配置中的公司官方 feed 或官方 sitemap，保存最近时间窗口内的重要新闻、发布、研究文章和博客文章。该来源本身已经经过公司和栏目筛选，当前不要求内容必须与 `consensus.md` 的具身方向强相关；进入后续统一筛选时再评估相关性和重要性。

本 skill 不负责跨来源去重、总结、最终评分、final publication 写入、candidate 整理或发布。

在 phase 中运行时返回结构化进度，不直接写 final publication：开始为 `正在抓取 AI 公司官方新闻、研究和产品发布。`；成功为 `AI 公司官方来源抓取完成，共生成 {record_count} 条来源记录。`；部分成功和失败需附 record/error 数及一句短错误。计数以 manifest 的 `record_count` 为准，成功空结果是 0 条成功。

## 来源配置

唯一来源清单位于：

```text
.agents/skills/sources/ai-company-official-crawler/sources.json
```

新增来源时只编辑该文件，不修改抓取主逻辑。支持三类来源：

- `rss`：RSS 或 Atom feed；
- `sitemap`：官方 sitemap，使用 `include_url_prefixes` 选择栏目，并抓取文章页补充真实发布日期和正文；
- `blog_listing`：无 RSS/sitemap 的官方博客列表页，从列表页提取文章链接和可见发布日期，再抓取文章页正文。

可选配置：

- `topic_keywords`：仅在某个官方入口范围过宽时启用。当前 Meta Newsroom sitemap 使用该字段排除非 AI 公司新闻；其他公司来源不做共识过滤。
- `article_url_prefixes`：`blog_listing` 类型的文章路径前缀，默认 `["/blog/"]`；只解析与列表页同域的链接，日期以列表页展示值为准。
- `date_source`：RSS 日期是否可信。默认 `feed`；设为 `page` 时必须从官方文章页提取真实发布日期后再做窗口过滤。
- `feed_date_granularity`：feed 日期精度，默认 `exact`；`month` 表示日期只能用于月份候选预筛，不能作为最终发布日期。
- `allowed_article_hosts`：允许补抓的官方文章域名。镜像或聚合 feed 应配置此项，避免跟随到非官方页面。
- `page_enrichment`：RSS 文章页补抓策略，默认 `auto`（仅短正文补抓）；`always` 始终补抓；`never` 不补抓。已知页面拒绝自动请求时使用 `never`，短摘要记录会明确标为 `partial`。
- `notes`：解释镜像、替代入口或限制，不参与执行。

当前来源：

- OpenAI News RSS；
- Hugging Face Blog RSS；
- Google AI Blog RSS；
- Google DeepMind Blog XML 镜像，条目必须指向 DeepMind 或 Google 官方域名，并以官方页面日期为准；
- NVIDIA Blog RSS；
- Physical Intelligence 官方 sitemap 中的 `/blog/`；
- Generalist AI 官方博客列表页；
- Perceptron 官方博客列表页；
- Anthropic 官方 sitemap 中的 `/news/`、`/research/`、`/engineering/`；
- Meta 官方 Newsroom sitemap 中的 AI 相关文章。

OpenAI 文章页对当前 HTTP crawler 返回 403，因此只保存官方 RSS 摘要并标记 `partial`，不重复发起已知失败的页面请求。Google DeepMind 镜像的 `pubDate` 是每月第一天的粗粒度占位值，不能用于自然日窗口判断；脚本先用月份与窗口是否相交缩小候选，再只打开允许域名上的官方页面并使用 `article:published_time`。Generalist AI 和 Perceptron 没有可用 RSS/sitemap 日期：Generalist 无 feed 和 sitemap，Perceptron 是 Framer 站点且 sitemap 无 `lastmod`；两家均使用官方博客列表页的服务端渲染链接和展示日期，文章页只用于补齐标题、描述、正文和图片。Physical Intelligence sitemap 条目位于 `physicalintelligence.company` 域名并重定向到当前 `pi.website` 域名，两个域名的 `/blog/` 前缀均在允许列表中。Anthropic 没有可用的公开 RSS，使用官方 sitemap。Meta AI 常见 RSS 路径无效，且 `ai.meta.com` 的 robots policy 限制自动采集，因此不绕过该限制；使用 Meta 官方 `about.fb.com` Newsroom sitemap，并按 AI 主题词过滤。

## 运行

```bash
uv sync --locked

uv run python \
  .agents/skills/sources/ai-company-official-crawler/scripts/crawl_ai_companies.py
```

独立运行且未提供窗口参数时，在启动时按 `Asia/Shanghai` 计算前一个完整自然日，并以该日的本地 `00:00` 到下一日 `00:00` 作为 `[since, until)`；由编排调用时使用调用方冻结的精确窗口。固定窗口示例：

```bash
uv run python \
  .agents/skills/sources/ai-company-official-crawler/scripts/crawl_ai_companies.py \
  --since 2026-07-31T16:00:00+00:00 \
  --until 2026-08-01T16:00:00+00:00 \
  --cycle-id daily-2026-08-01 \
  --output-dir /tmp/colab-daily-company
```

只查询、不写文件：

```bash
uv run python \
  .agents/skills/sources/ai-company-official-crawler/scripts/crawl_ai_companies.py \
  --dry-run
```

## 时间语义

- RSS/Atom 条目默认优先使用 `published`，缺失时使用 `updated`。
- 配置为 `date_source: page` 的 RSS 不使用 feed 占位日期做最终过滤；可按声明的 feed 日期精度缩小候选，然后以页面真实发布日期执行 `[since, until)` 校验。
- Sitemap 的 `lastmod` 只用于缩小需要打开的候选页面，不能直接当成发布日期，因为旧文章编辑也会更新 `lastmod`。
- Sitemap 文章页按 `article:published_time`、Schema.org `datePublished`、Anthropic `publishedOn` 等官方元数据提取真实发布日期。
- 无法获得真实发布日期的 sitemap 页面标记错误并跳过，不能把页面更新时间冒充新文章。
- 所有时间转换为带时区的 UTC ISO 8601，并按 `[since, until)` 再次校验。
- 不同公司来源并行抓取；同一来源内的文章页保持顺序请求，避免对单个官方站点造成突发并发。

## 内容范围

RSS 条目保存 feed 提供的标题、摘要、完整内容、作者、分类、链接和图片；如果 feed 只提供短摘要，脚本会请求官方文章页补充正文。

Sitemap 条目保存文章页的：

- canonical URL；
- 标题和描述；
- 真实发布日期和更新时间；
- 作者、栏目和关键词（页面提供时）；
- `article` 或 `main` 中的可读正文；
- Open Graph 图片。

正文是原始可读文本，不翻译、不总结。为了避免把导航和脚本写入记录，HTML 解析忽略 `script`、`style`、`nav`、`header`、`footer` 等区域。

## 参数

- `--sources`：来源配置，默认 skill 目录的 `sources.json`。
- `--output-dir`：输出根目录，默认 `<repo>/working_tmp`；固定使用其下的 `ai_company_official/`。
- `--since`、`--until`：带时区的显式半开窗口。
- `--cycle-id`：可选稳定周期 ID；独立运行可省略并在 manifest 中写入 `null`，生产调用必须传入非空冻结 CycleID。
- `--days`：显式指定滚动时长；未显式传入时不参与默认窗口计算。
- `--max-entries-per-source`：单来源在日期过滤前最多解析的 feed 条目或 sitemap 候选，默认 200。
- `--dry-run`：打印结果，不写文件。
- `--verbose`：输出请求和解析日志。

## 输出契约

```text
working_tmp/
└── ai_company_official/
    ├── crawl_manifest.json
    └── <SOURCE_NAME>--<STABLE_HASH>/
        └── record.md
```

目录身份由来源名和 canonical URL 的 SHA-256 前缀决定，不使用标题作为唯一身份。不同公司的同标题文章不会互相覆盖。`crawl_manifest.json` 显式包含 `cycle_id`；独立运行未传 `--cycle-id` 时为 `null`，生产调用必须回读为冻结的非空 CycleID。

`record.md` 至少包含：

```yaml
source: ai_company_official
category_hint: News
content_status: full
source_role: primary
source_name: anthropic_official
publisher: Anthropic
source_id: "anthropic_official:abc123..."
title: "..."
authors: ["..."]
published_at: "2026-08-01T04:00:00+00:00"
updated_at: "2026-08-01T08:14:55+00:00"
retrieved_at: "2026-08-01T16:00:00+00:00"
window_since: "2026-07-31T16:00:00+00:00"
window_until: "2026-08-01T16:00:00+00:00"
url: "https://www.anthropic.com/news/..."
feed_url: "https://www.anthropic.com/sitemap.xml"
homepage_url: "https://www.anthropic.com/news"
categories: ["News"]
image_urls: ["..."]
content_type: "sitemap_article"
status: complete
attachments: []
errors: []
```

正文保存原始摘要和正文，以及官方来源链接。完整正文记录使用 `content_status: full`；正文补抓失败但 feed 摘要仍可用时使用 `content_status: summary_only`，记录的 `status` 为 `partial` 并在 `errors` 中说明降级原因。`category_hint: News` 和 `source_role: primary` 只表达公司一手新闻来源属性，不能代替 rating 的最终分类和质量判断。`crawl_manifest.json` 记录时间窗口、配置文件、来源级 `category_hint: News`、`source_role: primary`、记录级 `content_status` 汇总（`full`、`summary_only` 或 `mixed`）、每个来源的成功、部分成功或失败状态、读取 feed/sitemap 条目数、实际文章页请求数、窗口内条目数、最终记录数和记录目录。

## 重复执行与失败

- 同一 canonical URL 重复运行使用相同目录并覆盖 crawler 产物。
- feed 内重复项和同来源重复 canonical URL 会去重；跨公司内容留给后续统一去重。
- 单个来源失败或单条正文补抓失败不阻断其他来源，manifest 标记部分成功并保留错误。
- 全部来源失败时返回失败；请求成功但前一个完整自然日内无文章属于成功空结果。
- RSS 条目缺少日期或链接时跳过并计入错误；sitemap 页面缺少真实发布日期时同样跳过。
- 不清空 `working_tmp`，不写 final publication，不修改正式站点内容，不调用后续 phase，不触发发布。

## 验收

1. `uv sync --locked` 后可直接执行。
2. 所有启用来源在 manifest 中都有明确状态，不静默遗漏。
3. 输出记录的真实发布日期位于指定窗口，旧文章仅更新不得误入。
4. 公司官方文章不因与具身方向不相关而在 crawler 阶段被删除。
5. Meta 只通过允许自动访问的官方 Newsroom 入口采集 AI 内容，不绕过 `ai.meta.com` 的限制。
6. `--dry-run` 不写文件；部分失败和空结果可观察。
