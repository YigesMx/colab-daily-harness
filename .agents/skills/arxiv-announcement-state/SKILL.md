---
name: arxiv-announcement-state
description: 从官方公告 RSS/Atom 发现 arXiv 来源记录，使用项目 SQLite source cursor/events 与可恢复批次；不以 submittedDate 代替公告，不做语义评分或正式发布。
---

# arXiv Announcement State — SQLite adapter

## 输入与所有权

先通过 `python -m colab_daily.lifecycle begin` 冻结上海日报日期/窗口和
唯一 `working_tmp/` owner。调用本脚本必须给出同一个 owner 与 CycleID。
不接受凭临时 JSON 或旧 owner 文件推断所有权，不按时间抢占。

```bash
uv run --locked python .agents/skills/arxiv-announcement-state/scripts/sync_arxiv_announcements.py \
  --owner example-owner --cycle-id daily-2026-01-02 --consensus consensus.md
```

可选 `--project-root` 指定项目根；相对路径从项目根解析，不从 cwd 解析。
`--display-date`、`--since`、`--until` 可省略并从 SQLite 恢复；显式窗口必须
成对提供且等于被冻结的前一个完整上海自然日。`--output-dir` 只能指向项目
`working_tmp`。旧 `--state-root` 不再支持，原始 source JSON 仅保留作迁移归档。

## 公告与语义边界

动态从 consensus 读取关注分类和关键词组。官方分类 RSS 负责召回，不在
来源层按简单关键词丢掉分类内论文；关键词只作为 matched_query_groups 审计提示。
对 new/cross 按规范化 ID 归并，再通过 API id_list 补全权威元数据。replacement
只进入事件/manifest，不进入新论文记录。保留请求间隔和完整记录约定。
方向判断、现实对象去重、三轨评分和精读仍由各自独立 agent contexts 完成。

## SQLite 是唯一状态权威

- source=`arxiv`；`source_cursors` 保存有界标量 cursor、revision，新增
  `source_events` 仅保存 seen/pending/emitted 身份、时间、分类等控制字段；
  不保存候选正文/summary/title。未提及事件和已迁移历史字段保留，不复制进
  新操作请求。retention_days 默认 30、不得小于 14，目前不执行历史删除。
- `source_operations` 在同一个 CAS 事务中保存身份增量、cursor 和有界 receipt
  （operation/input identity、临时路径、hash、count），绝不存 manifest/record
  全文、二进制、base64、文件快照或全状态 bundle。临时材料仅在 working_tmp。
  SQLite 是 owner/stage/source 状态权威，但不是临时文档备份。
  任何 API token/header 不得进入状态或诊断。
- API 失败把尚未 emitted 的当前事件持久化为 pending，不发布成功 manifest；
  之后即使 RSS 不再列出该事件，也可以补全并在同一事务中转为 emitted。
- 先在 `.arxiv-batch-<digest>` 写入并 fsync 记录、manifest、identity update
  和 receipt，验证文件后持久化最小 stage intent，再次验证/fsync 本地文件，
  然后 CAS cursor/events/receipt，最后原子显现 `working_tmp/arxiv/`。
- 提交结果未知或提交后显现中断：先查询 SQLite operation，复用已验证的本地
  材料，不重复联网/消费事件。已提交材料缺失、损坏或含符号链接直接失败，
  不假装可以从 DB 重建。
- CAS 冲突不显现本批次、不覆盖其他 cursor。缺失/不完整的未提交材料或 stale
  CAS 必须显式 `--retry-uncommitted`；先证明无已提交 operation，再将原本地
  尝试保留到 `.arxiv-attempts/<revision>`，读取当前 cursor 重新抓取。此参数
  不能绕过已提交批次，也不能改变 owner/日期/窗口。
- 相同批次身份重复调用复用原值；改日期/窗口/分类/关键词组/retention 参数失败关闭。
  已出现但与已提交批次不一致的文件保留并报错，不盲目覆盖。

## 输出与合法空结果

`working_tmp/arxiv/crawl_manifest.json` 与 `<arxiv_id>/record.md` 是临时批次材料，
不是 SQLite 可再生投影。manifest 包含冻结窗口、feed 状态、new/replacement/pending/emitted
数量、record_sha256、source_revision、source_operation 和 state_backend=sqlite。

成功空结果不等于抓取失败。只有关注 feed 同步/解析成功（或可验证无公告）、没有
待补 API 的 pending、emitted=0，并且冻结日期前一上海自然日满足周末/无公告证据时，
下游 Paper track 才能采用合法空原因。partial/failed 或未知状态不得冒充成功空结果。

上一周期候选身份另由 SQLite `prior_identity` 冻结全量历史候选；arXiv ingestion
ledger 不替代跨来源、跨周期的语义去重。正式发布、站点构建、通知不属于本 skill。
清理必须通过 lifecycle 的 SQLite owner/Released gate；不能删除 state 或其他周期。

## 离线测试

```bash
uv run --offline --locked python -m unittest discover -s .agents/skills/arxiv-announcement-state/tests -v
```

覆盖窗口/ID、SQL 控制权威、pending、CAS 冲突和受控重试、fsync/intent 前后
断点、未知提交结果无重复消费、已提交材料缺失/损坏/symlink、owner/跨周期
拒绝，以及 sentinel 正文/摘要不进入 SQLite。集成遵守 `colab_daily/RUNTIME.md`；
此轻量元数据边界取代旧的整批文档数据库重放设计。
