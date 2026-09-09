---
name: rating-filter-organize
description: 无损清点并归并当期全来源对象，共享冻结精确 Category 与上一周期身份排除，随后按冻结 Category 启动隔离的 Paper、News、Policy 三个生产 rating，并生成不可跨 track 比分的固定三组候选。只要任务涉及整理抓取结果、跨来源或跨周期去重、机器分类、三 track 评分、筛选日报候选或生成 grouped rating artifact，就使用本 skill。
---

# Rating Filter Organize

本 skill 拥有 production rating 的完整边界。共享阶段只做 quality-neutral source inventory、跨来源 canonicalization、一次性精确 Category assignment、上一周期身份排除和 production routing freeze；质量判断只能由 `paper`、`news`、`policy` 三个相互隔离的生产 child runs 完成。共享协调器不得提前 triage、打分、比较或选择，child 不得重新 canonicalize、分类或读取 sibling track。

本 skill 的 agent 语义阶段不以内容 hash 代替判断。Python runtime 仅用 bounded path/tree/artifact receipts、CycleID、SQLite Publish/run identity、CandidateID 和稳定来源身份完成幂等、归属与恢复。

## 输入与职责

调用方提供 `owner`、`cycle_id`、SQLite `publish_id`、冻结 `selection_limit=20` 和 `working_tmp/` 中本周期全部 source crawler 产物。production schema v3 的 `selection_limit` 固定为 `20`，不接受更小上限；新 production artifact 必须声明 `schema_version: 3` 与 `quota_contract: three-track-v3`。

共享协调器职责：

1. 动态清点全部 source 状态和合规 records；`success`、成功空、`partial`、`skipped`、`failed` 可观察，有效 partial records 不丢失。
2. 无损 canonicalize 全部可消费 records；每条 source record 恰好属于一个 canonical object。
3. 依据对象形态、原始内容和主要事实为每个 object 精确赋值一次 `category: Paper | News | Policy`。
4. 通过 SQLite `prior_identity` 冻结上一 eligible 周期全部候选身份，不按旧 `Selected` 或 `Category` 过滤；仅历史 `Migrated` snapshot 可超过 20 行。
5. 将未排除对象穷尽互斥路由到 `paper`、`news`、`policy` 三路。
6. 三路均可独立 materialize；全部 terminal output 完成后，仅在数量层执行 News reserve 对 Policy 缺口的 fallback，再组装固定三组 artifact。

共享 routing 禁止 `admission`、`decision`、`qualified`、`selected`、`score`、`group_rank` 等质量字段。本 skill 不直接写 final publication、不运行 crawler、不 refine、不 deploy、不推进 phase、不清理 `working_tmp/`。SQLite 只接收 run ownership、stage checkpoints、prior snapshot 与有界 artifact receipts；完整 inventory/rating 文档留在 workspace。

## 受管布局

```text
working_tmp/rating_filter_organize/
├── current.json
├── activation_index.json
├── publishing.json
└── runs/<RUN_ID>/
    ├── manifest.json
    ├── partition_manifest.json
    ├── production_partition_report.json
    ├── grouped_selection.json
    ├── shared/
    │   ├── source_inventory.json
    │   ├── canonical_objects.json
    │   ├── canonical_records/
    │   ├── source_records/
    │   ├── prior_cycle_identity.json
    │   └── routing.json
    └── tracks/{paper,news,policy}/
        ├── contracts/{consensus.md,rating_skill.md,agent_prompt.md}
        ├── input.json
        ├── terminal_output.json
        ├── context.json
        ├── triage.json
        ├── scoring.json
        ├── comparison.json
        ├── cutoff.json
        ├── evidence/
        └── output.json
```

Immutable run 内完整写入并验证后，才以 `publishing.json` journal 和排他 activation lock 原子切换 `activation_index.json`/`current.json`。失败保留现场；成功后的普通重试复用同一 generation。

## 共享验收

1. inventory 覆盖动态发现全集，record path/owner/window/cycle 与 source receipts 一致。
2. canonicalization 与 Category assignment 一一、无损、无质量判断；source hint 仅审计。
3. prior identity 冻结上一完成周期全部非空 CandidateID，confirmed same 排除，疑似保留并审计。
4. routing 输入并集等于三路输出并集，三路交集为空；空 Paper route 在满足 arXiv 周末/同步证明时合法，不阻断 News、Policy。
5. Paper track 只读取 Paper；News 只读取 News；Policy 只读取 Policy。各 track contract/evidence 全部为 track-private immutable snapshot。

## 三轨配额与隔离

三路固定语义：

- Paper：`paper_capacity=10`，target=10，`score_scale=paper-v2`。
- News：`news_capacity=10`，primary target=5，`score_scale=news-v3`；必须输出按 News 分数域排序的 qualified reserve list。
- Policy：`policy_capacity=3`，target=3，`score_scale=policy-v3`；严格按 `policy_consensus.md`，把明确 AI/具身智能/机器人/大模型/智能体相关的正式政策行动作为 materiality 加分，并允许满足证据合同的强相关 55-59 分对象进入边界合格列表。

三个 child 均独立完成 triage、评分、同 track 比较、cutoff 和输出，不读取 sibling 内容、候选 ID、分数、顺序或判断。最终协调器只读取各路有序 ID 数量：Policy 取前最多 3；News 取自身前 `10 - selected_policy_count`；Paper 取前最多 10。这个 fallback 是数量级操作，不比较 News 与 Policy 分数。Paper 永不补 News、Policy 缺口；不得为凑数加入未过 cutoff 的对象。

每个 child terminal output 必须声明 `schema_version: 3` 与 `quota_contract: three-track-v3`，并始终包含顶层 `under_target_reason`：ordered qualified 数低于本 track target 时为非空有界原因，达到 target 时必须为 null。该原因同时冻结到 cutoff、terminal manifest 与 grouped selection coordinator，只能解释合法短缺，不得作为抓取失败或跳过验收的理由。

Paper 为空合法条件：arXiv announcement source 为 `success` 或可解释的成功空，manifest/feed/pending/emitted 证明无公告，其他 Paper 来源状态明确，且冻结 DisplayDate 的前一上海自然日为周末/arXiv 无公告日。若 arXiv feed/API 失败、pending 不一致、状态未知或无法证明空结果，Paper track 保持 non-terminal 并重试诊断，不得把抓取失败当作合法空。

## 最终 Artifact

唯一最终 rating artifact 是 immutable `grouped_selection.json`：

```yaml
schema_version: 3
quota_contract: three-track-v3
production_input: true
production_artifact: true
cycle_id: "<CycleID>"
rating_run_id: "<RUN_ID>"
selection_limit: 20
score_domains_comparable: false
track_consensus:
  paper: {id: paper-v2, path: "<snapshot>"}
  news: {id: news-v3, path: "<snapshot>"}
  policy: {id: policy-v3, path: "<snapshot>"}
groups:
  Paper: {candidates: []}
  News: {candidates: []}
  Policy: {candidates: []}
```

固定且仅允许 `Paper`、`News`、`Policy` 三组，空组保留。每条 CandidateID 全局唯一，`category` 等于所在组，只允许 category-local `group_rank` 和本 track `group_score`。Paper rank 1-3 可由下游派生 presentation emphasis；News、Policy 永不 emphasis。禁止 All/global/final/combined rank 或 score。

manifest 保存 source/status/count、canonical/prior/routing identity、三路 input/output/context/contract/evidence identity、assessed/qualified/selected/final_selected、fallback 数量和验证结果；不得把 sibling 内容、candidate IDs、score、reason 或 order 写进另一 track input/context/provenance。

## 完成验收

1. 所有动态来源和可消费 records 完整进入共享 inventory。
2. source records、canonical objects、prior identity 和三路 routing 全部对账。
3. 三个 child 使用独立 clean context 并覆盖全部 assigned candidates。
4. News/Paper/Policy outputs 不超过各自 isolated capacity；三路均可为空，但 Paper 空必须满足上述周末/同步证明。
5. grouped selection 通过 `validate_category_quota.py` 和 `validate_grouped_candidates.py` 的 v3 等价校验：总数≤20、Paper≤10、Policy≤3、News≤10-Policy、News+Policy≤10。
6. immutable run、manifest、activation index 和 current 指向同一 generation，`publishing.json` 不存在。
7. 未写 final publication、VitePress、refine、phase 或其他 sink；只有有界 SQLite runtime checkpoints。
