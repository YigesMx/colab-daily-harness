---
name: crawl-from-all-sources
description: 发现并运行仓库 `.agents/skills/sources/` 下的全部 source skills，正式 PHASE_release 使用冻结的前一个 `Asia/Shanghai` 自然日半开窗口并把结果写入 `working_tmp/`。
---

# Crawl From All Sources

本 skill 是供当前 agent loop 使用的编排提示词，不包含脚本，也不维护来源清单。

在 `PHASE_release` 中运行时，接收统一的 lifecycle context：`DisplayDate`、冻结的 `since`、`until`、`working_tmp` 输出根、CycleID 和 workspace owner token。上下文只用于边界校验和进度汇总，不能改变来源特有规则。arXiv source 必须通过 `arxiv-announcement-state` adapter 使用 SQLite source cursor/events；其他 source 使用 lifecycle source adapter。并行 source skills 不直接写 final publication；SQLite 只保存有界 request/tree/count receipts，完整 source documents 留在 workspace。

1. 确定一个带时区的半开时间窗口 `[since, until)`。日报 prepare 必须由调用方提供已冻结窗口，并验证它等于 `DisplayDate` 前一个 `Asia/Shanghai` 完整自然日：例如 `DisplayDate = 2026-08-02` 时为 `[2026-08-01T00:00:00+08:00, 2026-08-02T00:00:00+08:00)`。独立运行若只提供 `DisplayDate`，按同一规则计算并冻结；若窗口和展示日期都未提供，以启动时的上海本地日期作为 DisplayDate，只计算一次前一自然日窗口并明确报告。不得使用滚动 72 小时、session 当前 UTC 时刻或上一次成功 publish 时间作为生产边界，也不能让 child skills 各自计算默认窗口。
2. 枚举 `.agents/skills/sources/*/SKILL.md`，按父目录名排序。每个匹配文件都是本次必须处理的 source skill；不要使用记忆中的静态列表，也不要递归运行更深层目录或没有 `SKILL.md` 的目录。
3. 逐个读取发现的 `SKILL.md`，遵循其中声明的前置条件、运行方式、输出契约、幂等和失败行为。来源特有参数只能来自对应 source skill 或用户的明确要求，不在这里推断或复制。
4. 可并行运行彼此独立的 source skills，但每个 skill 的内部顺序约束优先。PHASE 生产调用必须把同一非空冻结 `CycleID` 以 `--cycle-id` 传给所有支持该参数的 source，同时传入同一 `since`、`until` 和仓库 `working_tmp/` 输出根目录；每个 manifest 必须回读并验证 `cycle_id` 与窗口一致。独立 source 调试运行可省略参数，此时 manifest 的 `cycle_id` 必须为 `null`。
5. 单个来源失败不应阻止其他来源完成，也不阻断调用方把其他来源的有效记录交给后续 rating。不要自动重试会消耗配额或被 source skill 禁止重试的请求；需要跳过来源时，只有用户明确要求或对应 source skill 契约允许时才跳过，并保留可观察结果。arXiv 脚本自身负责有限 RSS/API 重试和 state pending，不由 agent 手工维护 ID 队列。
6. 全部来源结束后，检查每个已发现 skill 都有明确结果：成功、成功空结果、部分成功、跳过或失败。验证其 manifest 和 `record.md` 等产物符合各自契约；不要在本步骤总结、去重、评分、refine、写 final publication、deploy、通知、清理 `working_tmp/` 或调用后续 phase。
7. 向调用方报告实际发现的 source skill 名称、DisplayDate、统一时间窗口、每源状态与记录数，以及所有错误或跳过原因。成功空结果是有效完成但必须明确标记；只要任一来源为部分成功、失败或跳过，就不要把全源抓取描述为完整成功。

返回的每个 source 结果必须包含 `skill`、稳定 `source_name`、稳定 `source_directory`、`status`、有界 `error`、非空有界 `detail`、`record_count`、`error_count`、CycleID、冻结 `window_since/window_until`、manifest 路径或 null，以及 manifest 声明的精确本次 `record_paths`。有 manifest 的来源必须回读验证 manifest 显式包含且匹配 `cycle_id/window_since/window_until/status`；每条 record front matter 必须显式包含与冻结 instant 相等的 `window_since/window_until`，并按其 source record 契约包含非空 `status` 或 `crawl_status`，但 source record 契约未要求 CycleID 时不新增这一要求。manifest 创建前失败/跳过的 source 也必须有 entry，manifest path 为 null 且 records 为空。

调用方必须在 rating 前把本次动态发现全集以 schema v2 原子写入唯一项目内非 symlink receipt `working_tmp/.phase_prepare_candidates/source-discovery-<CYCLE_ID>.json`，不得使用替代文件名、其他目录或 symlink。顶层字段至少精确包含 `schema_version: 2`、`cycle_id`、统一 `window_since/window_until`、`discovered_count`、`completed_count`、`record_count`、`status_counts` 和 `sources`；每个 source 精确包含上一段列出的全部字段。terminal receipt 的 `completed_count` 必须等于 `discovered_count`，skill 集合必须等于本次动态枚举全集，各状态计数、总记录数、逐源记录数、error/error_count 和 manifest/record 集合必须可复算且一致。同一 CycleID receipt 不存在时原子创建，已存在时只允许精确 JSON 相同复用，不直接覆盖。使用以下 PhaseDetail 模板，由调用 phase 写入：

- 开始：`正在并行抓取 {source_count} 个信息源。`
- 运行中：`已完成 {completed}/{source_count} 个信息源抓取，当前共生成 {record_count} 条来源记录。`
- 完成：`已完成全部信息源调度，共获得 {record_count} 条来源记录。`
- 不完整完成：`已完成全部信息源调度，共获得 {record_count} 条来源记录；其中 {partial_count} 个部分成功、{failed_count} 个失败、{skipped_count} 个跳过。`

错误 detail 不包含 token、带凭据 URL、完整 traceback 或抓取正文。

重复执行同一窗口时复用各 source skill 的稳定身份和幂等行为，不扩大或改变时间窗口。若生产 sink 前发现 source manifest 缺少 `cycle_id`（`missing_cycle_id`），这是预-sink failure；修复脚本后必须复用既有 owner、context、Publish 和冻结窗口，仅重跑受影响 source 并继续，不要求人工补写 manifest。新增、删除或重命名 `.agents/skills/sources/*/SKILL.md` 后，下一次运行应自动反映目录现状，无需修改本 skill。
