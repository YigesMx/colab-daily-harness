---
name: ai-official-policy-source-crawler
description: 抓取国务院、工业和信息化部、AI Policy Daily、美国 Federal Register、欧盟 Digital Strategy、英国 GOV.UK/DSIT 和中国国家标准平台的公开 AI 政策动态，在冻结的 [since,until) 窗口内生成 working_tmp/ai_official_policy_source/ 原始政策记录。只要任务要求官方 AI 政策、中外人工智能政策追踪、AI 标准公告或 AI Act/监管程序，就使用本 skill。
compatibility: 需要 uv、项目根目录 .venv 中的 Python 3.10、feedparser、requests 和网络访问；不需要账号、token、验证码或浏览器自动化。
---

# AI Official Policy Source Crawler

## 唯一职责

从七个公开来源发现与人工智能、具身智能、大模型、机器人、智能体、政策、治理、监管、标准、产业支持和政府行动明确相关的条目，按调用方提供的冻结 `[since, until)` 半开窗口过滤，并保存原始标题、摘要或正文以及可审计的来源元数据。本 skill 不负责共识筛选、跨来源去重、评分、精读、final publication 写入、候选整理或发布。

## 来源与公开端点

来源清单在同目录 `sources.json`，脚本只使用以下无认证端点：

- State Council：`https://sousuo.www.gov.cn/search-gov/data` 的公开 GET 查询，使用 `t`、`q`、`searchfield`、`timetype`、`mintime`、`maxtime`、`sort`、`sortType`、`p`、`n`，再只跟随 `www.gov.cn` 的详情页。结果读取 `searchVO.catMap.*.listVO`；详情页正文优先读取 `pages_content`/`UCAP-CONTENT`。
- MIIT：使用已测试的 `https://www.miit.gov.cn/search-front-server/api/structure/list-category?websiteid=...&searchid=51` 和 `https://www.miit.gov.cn/search-front-server/api/search/info` GET API。脚本先读取返回的 category `iid`，再将该动态 ID 放入搜索查询；不依赖 page/build/unit、jsearch 或其他旧 gateway endpoint。RRSdy 的少量 Atom URL 只作可选发现；404 标记为 `stale_404`，过旧 feed 标记为 `success_stale`，不把旧内容伪装成空结果。动态 API 返回条目后只跟随 `www.miit.gov.cn` 详情页。
- AI Policy Daily：`https://aipolicydaily.org/archive/daily/feed.xml`，以及每条日期对应的 `/archive/daily/YYYY-MM-DD/index.md` 和 `index.txt`。RSS 发现日报，日期索引提供正文；按 Markdown 标题拆分 `Policy Tracker`、`Global & Geopolitics` 和涉及政策的故事，其他章节只在标题或正文明确属于政策议题时保留。每条拆分故事使用独立的 `#story-NNNN` URL，并写入 `url_fragment_identity: true`，使下游 inventory 将同一日报页面中的故事保留为不同 canonical identity。`NNNN` 是解析器在该稳定 issue 中产生的 1-based story position；标题不参与唯一身份。
- U.S. Federal Register：`https://www.federalregister.gov/api/v1/documents.json` 的免 key JSON API，按 `conditions[term]=artificial intelligence`、官方 `publication_date` 和冻结窗口对应上海日期查询；读取标题、摘要、机构、文件类型、document number 和 `html_url`。仅在标题/摘要满足 AI 与政策双相关性时保存，避免只在全文偶然提到 AI 的普通行政文件进入下游。
- EU Digital Strategy：`https://digital-strategy.ec.europa.eu/en/rss.xml`。RSS 只作为官方发现和摘要证据，按 feed 发布时间过滤，保存 AI Act、AI Office、AI Gigafactories、GenAI/Apply AI 等明确 AI 政策或治理相关条目；无关数字市场/平台条目不得只因来源宽泛而进入。
- UK GOV.UK / DSIT：`policy-papers-and-consultations.atom?keywords=%22artificial+intelligence%22&organisations[]=department-for-science-innovation-and-technology`。Atom 按条目时间过滤并保存 DSIT 官方 policy paper、consultation、call for evidence 等明确 AI 相关条目。`updated` 只标记为 `feed_published` 日期依据；refine 如需首次发布日，应继续读取官方详情页。
- China National Standards：`https://std.samr.gov.cn/noc/search/nocGBPage?searchText=人工智能` 结构化 JSON，读取国家标准公告的公告号、日期、标题和平台 row ID；记录 URL 使用官方 listing page `https://std.samr.gov.cn/noc/nocGB` 加上稳定 `noticeCode/recordId` 查询参数，`discovery_url` 保留可复放 API。公告本身是 primary 正式政策证据，不因缺少详情页而 dropped。

不访问任何 tokenized 机器之心 RSS，也不通过验证码、登录、代理轮换、浏览器或其他方式绕过限制。

## 时间与请求边界

- `--since` 和 `--until` 必须同时提供并带时区；编排层传入同一冻结窗口。独立运行未提供窗口时使用前一个完整 `Asia/Shanghai` 自然日。
- 所有候选按明确发布日期过滤：`since <= published_at < until`。缺少日期的条目不进入窗口记录，只在报告中计数并说明。
- 每个来源默认最多 2 个发现关键词、每个关键词 1 页、每个日期最多 1 个详情请求；Federal Register、EU、GOV.UK 和 SAMR 是单次官方 API/feed 请求，不逐条详情抓取。默认候选上限 40，每源和总体请求总量有硬上限。请求顺序按来源串行，间隔由配置控制为 0，不并发突发访问。
- HTTP 403/429、robots 或 CAPTCHA 页面是 `blocked`，不是成功空结果；网络或解析错误是 `failed`；部分详情失败是 `partial`。动态/API 成功但没有窗口条目是 `success_empty`。RSS 成功但 feed/内容明显早于窗口是 `success_stale`。Federal Register 和 SAMR 日期查询成功但没有窗口条目是 `success_empty`；EU/GOV.UK feed 最新条目早于窗口时是 `success_stale`。成功且有记录是 `success`。

## 运行

```bash
uv run python .agents/skills/sources/ai-official-policy-source-crawler/scripts/crawl_ai_official_policy.py \
  --since 2026-07-31T16:00:00+00:00 \
  --until 2026-08-01T16:00:00+00:00 \
  --cycle-id daily-2026-08-01 \
  --output-dir /tmp/colab-daily-policy
```

参数：

- `--sources`：默认同目录 `sources.json`。
- `--since`、`--until`：冻结的带时区半开窗口；只传一个会失败。
- `--cycle-id`：可选稳定周期 ID；独立运行可省略并在 manifest 中写入 `null`，生产调用必须传入非空冻结 CycleID。
- `--output-dir`：默认仓库 `working_tmp`，固定写入其下 `ai_official_policy_source/`。
- `--max-records`：最终记录上限，默认 40。
- `--dry-run`：只打印状态和计数，不创建文件。
- `--verbose`：输出不含凭据的调试日志。

## 输出

```text
working_tmp/
└── ai_official_policy_source/
    ├── crawl_manifest.json
    └── <SOURCE_NAME>--<STABLE_ID>/record.md
```

每条 `record.md` 使用 YAML front matter，至少包含 `source: ai_official_policy_source`、`source_name`、`publisher`、`source_id`、`title`、`published_at`、`listed_at`、`date_basis`、`retrieved_at`、冻结窗口、`url`、`url_fragment_identity`、`discovery_url`、`homepage_url`、`source_category_hint: Policy`、`source_role`（`primary`/`secondary`）、`content_scope`（`full`/`substantial`/`summary_only`）、`content`、`summary`、`section`、`status`、`attachments` 和 `errors`。`url_fragment_identity` 仅对 URL fragment 确实定位独立子文档的记录为 `true`；当前只有 AI Policy Daily 拆分出的 `#story-*` 故事满足该条件，国务院和工信部普通详情页必须为 `false`。正文保持来源原文，不翻译、不总结。

国务院、工信部、Federal Register、EU、GOV.UK 和国家标准平台是 `primary`；AI Policy Daily 是 `secondary`，但其 `Policy Tracker` 作为政策栏目保留。每条记录的 `source_category_hint` 固定为 `Policy`。Federal Register/EU/GOV.UK/SAMR record 可为 `summary_only`，后续 refine 必须继续读取官方详情或正文；不得把 source summary 当作完整精读。

Manifest 显式包含 `cycle_id`（独立运行未传 `--cycle-id` 时为 `null`），并保存固定窗口、配置路径、请求次数、最终记录数、每来源 `success/success_empty/success_stale/partial/failed/blocked/not_yet_published` 状态、解析/窗口/详情计数和错误。生产调用回读时必须验证 CycleID 与冻结输入一致。运行不会清空其他 `working_tmp` 内容，不写生产数据。

## 幂等、失败与验收

- 稳定目录由来源名和规范 URL 的 SHA-256 前缀构造，重复运行覆盖同一目录，不以标题作身份。AI Policy Daily 的稳定 issue URL 和解析器 story position 共同构成 `#story-NNNN` fragment，并参与来源 ID 和目录 ID；相同 issue bytes 的重试产生相同 ID，同一期标题重复或标题 slug 冲突也不会覆盖。`url_fragment_identity: true` 明确要求下游 canonicalization 保留该 fragment；其他来源不得仅因普通页面锚点将该字段设为 `true`。
- AI Policy Daily 的 `not_yet_published` 表示 RSS 已成功且目标日期尚未发布；不得将其改写为 `success_empty`。若日报页面可读但 RSS 未列出，则仍需保守标记 `not_yet_published`。
- 单源失败不阻断其他源；全部源均失败或 blocked 时进程返回非零。失败现场保留 manifest 和已写记录，便于用同一窗口重试。新国际/标准来源更新频率天然低频，`success_empty` 是合法状态；连续多日 `success_stale` 应在 source manifest/receipt 中可观察，不得伪装成成功。
- 最小验收是：七类来源都有明确状态；请求数量受硬上限约束；条目严格在窗口内；AI/政策双相关性、正文深度元数据准确；Policy 分类和 primary/secondary 角色完整；不触碰 tokenized RSS、共识、其他 skill、final publication、正式站点或生产数据。
