---
name: huggingface-trending-crawler
description: 抓取 Hugging Face Daily Papers 前一个完整 Asia/Shanghai 自然日的 daily 榜和相交 weekly 榜，从热门论文中筛出符合仓库 consensus.md 研究方向的记录，并写入 `working_tmp/huggingface/`。只要任务提到 Hugging Face Daily Papers、HF trending papers、daily/weekly 热门论文，或要求为 Colab Daily 获取 Hugging Face 论文来源，就使用本 skill；不要用它抓模型、数据集或 Spaces 榜单。
compatibility: 需要 uv、项目根目录 `.venv` 中的 Python 3.10、`requests` 和网络访问。
---

# Hugging Face Trending Crawler

## 职责

读取仓库根目录的 `consensus.md`，获取 Hugging Face Daily Papers 的 daily 和 weekly 榜单，将榜单中近期出现且符合研究方向的论文写入 `working_tmp/huggingface/<paper_id>/record.md`。

本 skill 只负责 Hugging Face Papers 来源抓取、可解释的方向预过滤和榜单信号保存，不负责跨来源去重、最终评分、总结、final publication 写入、candidate 整理或发布。arXiv 与 Hugging Face 命中同一论文是正常现象，后续 `rating-filter-organize` 再跨来源合并。

在 phase 中运行时返回结构化进度，不直接写 final publication：开始为 `正在抓取 Hugging Face Daily 和 Weekly Papers 热门论文。`；成功为 `Hugging Face Papers 抓取完成，共生成 {record_count} 条来源记录。`；部分成功和失败需附 record/error 数及一句短错误。计数以 manifest 的 `filtered_record_count` 为准，成功空结果是 0 条成功。

## 运行

干净 session 先在仓库根目录同步项目环境：

```bash
uv sync --locked
```

默认抓取前一个完整的 Asia/Shanghai 自然日：

```bash
uv run python .agents/skills/sources/huggingface-trending-crawler/scripts/crawl_huggingface.py
```

固定窗口测试：

```bash
uv run python .agents/skills/sources/huggingface-trending-crawler/scripts/crawl_huggingface.py \
  --since 2026-07-31T16:00:00+00:00 \
  --until 2026-08-01T16:00:00+00:00 \
  --cycle-id daily-2026-08-01 \
  --output-dir /tmp/colab-daily-huggingface
```

只查询、不写文件：

```bash
uv run python .agents/skills/sources/huggingface-trending-crawler/scripts/crawl_huggingface.py \
  --dry-run
```

## 数据来源

### Daily

对时间窗口覆盖的 UTC 日期调用公开 JSON 接口：

```text
https://huggingface.co/api/daily_papers?date=YYYY-MM-DD&sort=publishedAt&limit=100
```

默认每个 daily 榜读取前 50 条。Hugging Face 的 daily 页面本身是社区选出的 Daily Papers，列表顺序和 `upvotes` 作为来源热度信号保存，但不等同于项目最终评分。

### Weekly

读取与时间窗口相交的 ISO 周榜页面：

```text
https://huggingface.co/papers/week/YYYY-Www
```

周榜的服务端 HTML 在 `DailyPapers` 组件属性中提供结构化论文数据。脚本解析该结构化 JSON，不依赖按钮点击、登录或易变的视觉选择器。默认每个 weekly 榜读取前 100 条。

如果 Hugging Face 后续提供稳定的 weekly JSON API，应优先迁移到公开 API，并保持本 skill 的输出契约不变。

## 时间语义

- 独立运行且未提供窗口参数时，在启动时按 `Asia/Shanghai` 计算前一个完整自然日，并以该日的本地 `00:00` 到下一日 `00:00` 作为 `[since, until)`；由编排调用时使用调用方冻结的精确窗口。
- `--since` 和 `--until` 必须使用带时区的 ISO 8601 时间。
- 论文进入窗口时优先使用 Hugging Face 的 `submittedOnDailyAt`，即进入 Daily Papers 的时间；字段缺失时才回退到榜单记录的 `publishedAt`。
- 周榜可能包含更早论文。脚本只保留其 Hugging Face 列出时间位于窗口内的论文，因此 weekly 的作用是补充近期论文的周榜排名，不把整周旧记录全部写入本次结果。
- 为覆盖跨周窗口，脚本读取窗口相交的全部 ISO 周榜，再按记录时间过滤。

## 热度与相关性

Daily/weekly 排名、upvotes 和评论数是来源信号，不在 crawler 中转换成最终质量分。

方向预过滤读取 `consensus.md` 的关键词组：

- `embodied_robotics`、`arm_vla`、`drone_vla_vln`、`embodied_infra`、`world_model` 属于直接主题。标题或摘要命中即可保留。
- `llm_vlm_methods` 范围较宽。标题命中可以保留；如果只在摘要命中，需要至少两个不同关键词，或同时位于 daily/weekly 前 10 名，避免仅因一个泛化的 `agent`、`LLM` 或 `reinforcement learning` 词汇纳入大量无关论文。
- 匹配不区分大小写，并按英文单词边界处理 `VLA`、`VLN`、`LLM`、`VLM` 等短词，避免子串误匹配。
- 每条输出都保存 `matched_query_groups`、`matched_keywords` 和 `relevance_reasons`，后续 skill 可以复核或推翻这个预过滤结果。

机构、作者和 upvotes 不作为方向准入条件，避免明星机构或高热度掩盖方向不相关；这些字段只保留给后续评分。

## 参数

- `--consensus`：共识文件，默认 `<repo>/consensus.md`。
- `--output-dir`：输出根目录，默认 `<repo>/working_tmp`；固定使用其下的 `huggingface/`。
- `--since`、`--until`：显式半开时间窗口。
- `--cycle-id`：可选稳定周期 ID；独立运行可省略并在 manifest 中写入 `null`，生产调用必须传入非空冻结 CycleID。
- `--days`：显式指定滚动时长；未显式传入时不参与默认窗口计算。
- `--daily-limit`：每个 daily 榜读取数量，默认 50。
- `--weekly-limit`：每个 weekly 榜读取数量，默认 100。
- `--min-upvotes`：来源 upvotes 下限，默认 0；通常不需要提高，因为 daily 榜已经经过社区选入。
- `--dry-run`：打印筛选结果，不创建或修改输出目录。
- `--verbose`：输出详细请求与过滤日志。

## 输出契约

```text
working_tmp/
└── huggingface/
    ├── crawl_manifest.json
    └── <PAPER_ID_SAFE>/
        └── record.md
```

`crawl_manifest.json` 至少记录：

- 时间窗口和共识文件；
- daily 日期和 weekly 周次；
- 各请求的 URL、状态、读取数量和错误；
- 合并前命中数、按 paper ID 去重数、相关性过滤后记录数；
- 本次结果目录清单；
- `cycle_id` 和固定窗口；独立运行的 `cycle_id` 为 `null`，生产调用回读时必须与冻结 CycleID 一致。

每条 `record.md` 至少保存：

```yaml
source: huggingface
category_hint: Paper
content_status: summary_only
source_role: secondary
source_id: "2607.27205"
title: "TurboVLA: ..."
authors: ["..."]
organization: "H-EmbodVis"
published_at: "2026-07-31T20:00:00+00:00"
listed_at: "2026-08-01T00:00:00+00:00"
retrieved_at: "2026-08-01T16:00:00+00:00"
window_since: "2026-07-31T16:00:00+00:00"
window_until: "2026-08-01T16:00:00+00:00"
huggingface_url: "https://huggingface.co/papers/2607.27205"
arxiv_url: "https://arxiv.org/abs/2607.27205"
pdf_url: "https://arxiv.org/pdf/2607.27205"
project_url: null
upvotes: 122
num_comments: 2
daily_dates: ["2026-08-01"]
daily_ranks: [1]
weekly_periods: ["2026-W31"]
weekly_ranks: [8]
matched_query_groups: ["arm_vla"]
matched_keywords: ["VLA", "vision-language-action"]
relevance_reasons: ["direct keyword in title: VLA"]
status: complete
attachments: []
errors: []
```

正文保存原始标题、作者、组织、Hugging Face 提供的原始 summary、来源链接、热度信号、榜单位置和相关性匹配依据。`category_hint: Paper`、`content_status: summary_only` 和 `source_role: secondary` 是来源线索，不是最终机器分类。crawler 不翻译或改写 summary。

## 重复执行与失败

- 目录名基于 Hugging Face paper ID；arXiv paper 使用规范化 arXiv ID，因此与同源版本稳定关联。
- 同一论文同时出现在多个 daily 日期或 weekly 榜时只写一个目录，并合并榜单位置；保留最佳 upvotes、评论数和最完整元数据。
- 重复执行允许覆盖本次命中的同 ID `record.md`，但不清空 `working_tmp` 或删除其他来源文件。
- 单个 daily 日期或 weekly 周榜请求失败时继续其他请求，将状态写入 manifest；至少一个请求成功时允许部分成功。
- 所有请求失败时进程返回失败，不把空结果误报为成功。
- 无相关论文但请求成功属于成功空结果，manifest 明确记录 `filtered_record_count: 0`。
- 不写入 final publication，不修改正式站点内容，不下载 PDF，不调用后续 phase，不触发发布。

## 验收

1. `uv sync --locked` 后可直接运行，不需要额外安装依赖。
2. 固定窗口下 manifest 能显示 daily 和 weekly 的独立请求结果。
3. 每条记录至少来自一个 daily 或 weekly 榜，并且 `listed_at` 位于窗口内。
4. 同一 paper ID 不重复建目录，同时保留 daily/weekly 两类榜单信号。
5. 每条记录有原始 summary、upvotes、来源 URL 和可解释的共识匹配依据。
6. `--dry-run` 不写文件；部分失败和空结果可观察。
7. 不修改 arXiv crawler、SQLite final publication、站点内容或 lifecycle phase。
