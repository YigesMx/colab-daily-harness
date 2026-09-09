---
name: arxiv-crawler
description: 通过 `arxiv-announcement-state` 固定脚本抓取 arXiv 官方 announcement RSS/Atom 中本期关注分类的新公告，跨分类按规范化 arXiv ID 去重，用 API id_list 补全元数据，并在 `working_tmp/arxiv/` 生成 source records；不要再用 submittedDate 直接定义日报新增。
---

# arXiv Crawler

## 职责

本 source skill 负责 arXiv 公告抓取和原始记录整理。公告发现、跨分类归并、状态持久化、API 元数据补全和原子提交由 `.agents/skills/arxiv-announcement-state/` 的固定脚本完成。它不负责跨来源语义归并、最终评分、存储发布写入、refine、站点或 phase。

运行前读取 `consensus.md`。如果共识不存在或没有分类/关键词配置，停止并报告。

## 运行

干净 session 先执行：

```bash
uv sync --locked
```

先通过 `python -m colab_daily.lifecycle begin` 冻结 CycleID、上海日期/窗口和全局 owner，再调用：

```bash
uv run --locked python .agents/skills/arxiv-announcement-state/scripts/sync_arxiv_announcements.py \
  --cycle-id daily-2026-01-02 --owner example-owner --consensus consensus.md
```

显式日期/窗口可省略并从 SQLite 恢复；若提供，必须与冻结字段一致。
`--output-dir` 只能是该项目的 `working_tmp`；`--state-root` 已移除。
离线测试使用独立临时项目/SQLite 与 mock feed/API，不调用生产抓取；参见公告 skill 测试。

脚本固定抓取 consensus 中的关注分类 Atom feed，用 `new`/`cross` 作为新公告候选，将 `replace`/`replace-cross` 仅保存为 state/manifest 事件。RSS 分类只做召回，`matched_query_groups` 只作提示，不能替代后续 rating。随后以 `id_list` 批量补全权威标题、作者、摘要、分类、published/updated 和 PDF URL。API `submittedDate` 不参与日报新增判定。

## 合法空结果与周末证明

成功空结果必须与抓取失败区分。manifest 显式保存每个官方 feed 的 success/HTTP/解析状态、窗口、公告事件计数、pending 数、emitted 数和 state 提交结果。只有全部关注 feed 同步/解析成功（或官方可确认无公告）、无待补 API 的 pending 事件、emitted=0，且冻结 DisplayDate 的前一上海自然日属于周末/arXiv 无公告日时，下游 Paper track 才能把空 arXiv 结果视为合法空。任何 feed 失败、解析失败、pending>0、state 未知或计数不一致都保持 failed/partial 并触发诊断重试，不得推断为空。

## 时间与状态

调用方冻结的 `[since, until)` 用于公告时间审计。SQLite source=`arxiv` 是唯一
cursor/events 权威；旧 JSON 只作保留的迁移归档。默认 retention_days=30、下限14，
本适配器保留未提及事件，不自动删除历史。API 失败仅持久化 pending 身份；
成功先将临时记录/manifest 写入并 fsync，保存最小 stage intent，再验证文件后
在同一 CAS 事务保存 cursor、emitted 身份变化和 path/hash/count receipt，最后
原子显现输出。临时正文、二进制和文件快照不得进 SQLite。已提交重试复用完整
本地材料；缺失则失败，不从 DB 重建。CAS 冲突/未提交残缺需显式
`--retry-uncommitted`，先证明无提交并归档本地尝试后再抓取。

RSS feed 的 `published`/Atom `published` 是公告时间；API 返回的 `published` 是提交处理时间。source record 必须同时保存 `announcement_at` 和 API 时间字段，不能混用。

## 输出

```text
working_tmp/arxiv/
├── crawl_manifest.json
└── <arxiv_id_safe>/record.md
```

每条 record 至少包含：`source_id`、`version`、`announce_type`、`announce_types`、`announcement_at`、`title`、`authors`、`published_at`、`updated_at`、`retrieved_at`、`window_since`、`window_until`、`primary_category`、`categories`、`source_categories`、`abstract_url`、`pdf_url`、`category_hint: Paper`、`content_status: substantial`、`source_role: primary`、状态、附件和错误。`category_hint` 仅说明 arXiv 是论文来源，不能替代 rating 在 canonicalization 后作出的最终 `category`。

manifest 至少包含 feed 状态、公告总数、`new_or_cross_count`、`replacement_count`、当前窗口数、恢复 pending 数、emitted 数、记录目录、record_sha256、source_revision 和 source_operation。成功空结果是合法状态；所有 feed 失败或 API 补全失败必须显式失败/部分状态。

## 验收

1. 不使用 API `submittedDate` 发现日报新增。
2. RSS 能发现此前提交但本期才公告的论文。
3. 同一论文跨关注分类只有一个 source record，保留全部来源分类和公告类型。
4. `new/cross` 与 replacement 分离。
5. state 与可恢复来源批次原子提交；失败不丢失恢复依据，已 emitted 身份不回退。
6. 不执行语义评分、正式发布或通知；恢复字节校验不替代语义质量判断。
