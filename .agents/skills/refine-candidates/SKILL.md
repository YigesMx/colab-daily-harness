---
name: refine-candidates
description: 读取 rating 冻结的 Paper、News、Policy 分组 selection，在严格隔离的三个生产 context 中先物化并审计原始全文/官方证据，再按各自共识生成完整候选文章。生产必须尽力为每条 selected 生成 publication item；信息受限时保守成稿并标注证据限制，只有经过多路径获取仍无法确认基本身份/事件或无法形成可读内容时才记录 dropped。随后由 refine-owned shared assembly 统一中文关键词、压紧最终 Category-local rank 并组装唯一 schema v3 publication_set。只要任务要求精读三路 rating 入入对象、生成分类专用候选文章、恢复精读失败或准备 SQLite/VitePress 的冻结输入，就使用本 skill；不得跨 track 读取、改分、替换或补位，也不得写 sink。
---

# Refine Candidates

本 skill 只丰富 rating 已冻结的 grouped selection，不重新选择候选，不推进 lifecycle phase。生产 refine 有且仅有三个逻辑 context：`paper`、`news`、`policy`。三者可以并行，但不得合并，也不得读取 sibling 的候选正文、评分、判断、顺序或中间产物。任一 context 输入为空时也必须生成 terminal-success empty manifest；Paper 空不阻断 News、Policy 和后续 assembly。

本 skill 禁止计算、保存或比较内容 hash。幂等与完整性只使用 CycleID、rating/refine run ID、CandidateID、Category、track、rank/score/scale、受管路径、文件大小/格式和下游回读。

## 输入

```yaml
cycle_id: daily-2026-08-17
display_date: 2026-08-17
timezone: Asia/Shanghai
selection_limit: 20
rating_current_path: working_tmp/rating_filter_organize/current.json
grouped_selection_path: working_tmp/rating_filter_organize/runs/<RATING_RUN_ID>/grouped_selection.json
output_root: working_tmp/refine_candidates
network_timeout_seconds: 30
max_download_bytes: 104857600
```

Grouped selection 必须来自稳定 rating current，`schema_version=3`、`quota_contract=three-track-v3`，总数≤20，Paper≤10，Policy≤3，News≤10-Policy。三组 shape 精确为 `{Paper:{candidates:[]},News:{candidates:[]},Policy:{candidates:[]}}`。Paper 条目 `rating_track=paper/score_scale=paper-v2`；News 为 `news/news-v3`；Policy 为 `policy/policy-v3`。三组 rank 均从 1 连续；三套分数不可比较，禁止 global/final/combined 字段。

## 证据获取

- Paper：主动多路径读取 TeX、PDF、HTML、作者 manuscript/补充材料、项目页、官方源码和 crawler 附件；只保存 URL 不读内容不算完成。无法确认的细节写“原文未报告/当前材料未确认”。
- News：官方 RSS/Atom、官网公告、摘要或附件能确认主体、时间、事件、官方 URL 即可保守成稿；401/403、反爬或 summary-only 不是 dropped 理由。
- Policy：优先正式 HTML/PDF、法规文本、公告和程序记录；全文受限时基于已确认事实保守成稿，并明确约束力、范围、条款、时间线和后续程序未知项。

每篇 Paper 必须完整执行图片检索，优先 Figure 1/TeX/PDF/HTML/项目页/官方仓库；News、Policy 也应检查官方页面与附件，无合适图时所有阶段 terminal 且非空 `nullReason` 后允许 null。禁止生成式备用图、logo、头像、广告图或整页截图。

## 路径与 generation

```text
working_tmp/refine_candidates/
├── current.json
└── runs/<REFINE_RUN_ID>/
    ├── input_set.json
    ├── recovery.json
    ├── contexts/{paper,news,policy}/
    │   ├── input_manifest.json
    │   ├── consensus.md
    │   └── generations/<TRACK_GENERATION_ID>/
    │       ├── generation_manifest.json
    │       ├── results.json
    │       ├── successful_set.json
    │       ├── dropped_set.json
    │       └── candidates/<CANDIDATE_ID>/
    └── assemblies/<ASSEMBLY_GENERATION_ID>/
        ├── keyword_taxonomy.json
        ├── candidates/<CANDIDATE_ID>/
        ├── publication_set.json
        └── manifest.json
```

每个 track generation immutable；重试只创建新 generation，复用已验证成功项，不重复已确认 dropped，不改成功正文。每个 context 必须证明 `input = successful_set ∪ dropped_set` 且交集为空。dropped 只允许在多路径获取后仍无法确认基本身份/事件、内容为空或无法形成可读文章；不得因摘要短、缺图、页面反爬或写作成本高丢弃。

## Category-specific 正文

- Paper H2 依次：`研究问题与贡献`、`方法与系统`、`实验设置与数据`、`结果、限制与结论`、`来源链接`。
- News H2 依次：`事件概述`、`已确认事实与证据`、`影响与后续观察`、`来源链接`。
- Policy H2 依次：`政策行动`、`适用范围与约束力`、`关键条款`、`时间线`、`影响与待观察事项`、`来源链接`。

正文必须完整、独立发布、不包含本地路径/占位符/内部评分过程。结论可追溯到证据；未确认处明确标注。

## Shared assembly

恰好三个 context 全部 terminal-complete 并覆盖输入后，refine-owned assembly 才可执行：

1. 从全文、标题与技术证据汇总暂存关键词，语义近似归并，建立全局规范中文 taxonomy。
2. 目标 10-15 个，15 为硬上限；少于 10 必须在 taxonomy 和 assembly manifest 均保存非空 `under_target_reason`。每篇映射 2-5 个最终标签。
3. 只消费三路 successful sets；按原 Category 和组内顺序保留成功项，压缩最终 category-local `group_rank`，原样保留 `group_score`、`score_scale`、`rating_track`。
4. 在规范 assembly 路径生成唯一 `schema_version: 3` publication_set.json，shape 仍为固定三组，只含成功项。
5. assembly manifest 对账 `grouped selection = publication set ∪ dropped set` 且交集为空。
6. taxonomy、三路输出和 publication set 完整验证后，原子更新唯一 `current.json`。

Pointer 必须显式包含 `refine_run_id`、`paper_generation_id`、`news_generation_id`、`policy_generation_id`、`assembly_generation_id`、规范 publication/taxonomy/manifest path。不得存在 rating-owned、phase-owned 或 post_refine alternative。

## 验收

1. 只消费 rating current 指向的 immutable v3 grouped selection。
2. 恰好三个隔离 context，均覆盖输入；空输入 terminal-success。
3. 每篇 selected 有 `success | dropped`，成功文符合对应 section 合同和证据/图片审计。
4. taxonomy 与每篇关键词来自同一最终映射。
5. publication set schema/shape/quota/rank/score/track 正确，三路 success/dropped 对账穷尽互斥。
6. 不写 final publication/VitePress/Feishu，不推进 Phase，不重新评分、分类或补位。
