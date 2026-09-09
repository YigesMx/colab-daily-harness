---
name: phase-release
description: 执行 Colab Daily 正式 PHASE_release：冻结上海日报周期，动态抓取全部来源，隔离完成 Paper/News/Policy 三轨评分与精读，冻结 SQLite 正式 publication，部署 VitePress，确认后进入 Released；可选发送 Released 通知。
---

# PHASE_release production workflow

本 skill 是正式无人值守入口。不存在 Grist、候选预览或人工闸门。语义判断由独立 agent context 完成；Python CLI 只负责周期所有权、检查点、确定性校验、SQLite 持久化、渲染和 delivery。

## 固定边界

- 从项目根运行；配置由 `colab_daily.config.Config` 解析，所有相对路径锚定项目根。
- `working_tmp/` 是唯一瞬态 workspace。source/PDF/图片/inventory/rating/refine/assembly 文件只在这里；失败完整保留。
- SQLite 只存有界控制/来源/delivery facts，以及最终 publication 和最终资产；禁止写入临时快照、正文采集包、base64、完整中间 manifest。
- 新周期始终为 `PHASE_release`。只有正式站点本地 build、精确非强制 push 和完整 public artifact 回读确认后，deployment CLI 才切换为 `Released`。
- 通知仅允许在 `Released` 后执行；失败或 unknown 不撤销 release、不重复部署。unknown POST 禁止自动重发。

## 1. 冻结周期与 owner

scheduled 在任何业务副作用前只读一次时钟；manual 必须显式日期；retry 不读时钟。创建有界 owner metadata JSON 后执行：

```sh
uv run --locked python -m colab_daily.lifecycle begin --trigger scheduled --owner <OWNER> --context state/input/owner-metadata.json --output working_tmp/input/cycle.json
# manual: add --display-date YYYY-MM-DD
# retry:
uv run --locked python -m colab_daily.lifecycle status --owner <OWNER> --output working_tmp/input/lifecycle.json
```

SQLite 冻结 `daily-<DisplayDate>`、DisplayDate 和前一完整 Asia/Shanghai 自然日半开窗口。一个非 closed workspace claim 全局唯一；不按年龄/PID 抢占，不创建替代 DB/workspace。

## 2. 动态 source discovery

按目录名排序枚举 `.agents/skills/sources/*/SKILL.md`，逐个读取并用同一 owner、CycleID、since/until、`working_tmp` 根执行。不得维护静态来源列表。每个 source 必须产生可核验状态；失败不阻断其他来源，但不得把未知 arXiv 状态伪装为空。

arXiv 使用 SQLite source cursor/events；旧 JSON 仅是迁移档案。其他 crawler 已接入 lifecycle source adapter。source records 与失败材料留在 `working_tmp`。

## 3. inventory 与三轨 rating

调用 active `inventory_sources.py` 和 `coordinate_three_tracks.py`，始终传 `--owner`。实际参数以 `--help` 为准：

```sh
uv run --locked python .agents/skills/rating-filter-organize/scripts/inventory_sources.py --help
uv run --locked python .agents/skills/rating-filter-organize/scripts/coordinate_three_tracks.py --help
```

共享阶段只能 inventory、canonicalize、单值 Category、冻结 SQLite prior identity、互斥 routing；不得语义评分。canonical object 与 routing candidate 必须原样携带 inventory 的不可变身份投影 `canonical_url`、`normalized_arxiv_id` 和完整 `source_identities`，不得由精读后的展示链接反推或替换。创建三个独立、干净 agent context：paper/news/policy。三路分数域不可比较或归一化。

固定配额：Paper target/max 10；Policy target/max 3；News primary 5 并按自身顺序保留最多 10 个 qualified reserve；最终 Paper≤10、Policy≤3、News+Policy≤10、总计≤20。Policy 缺口只能由 News reserve 自身顺序补。Paper 不跨轨补位。

## 4. 三个独立 refine context

必须恰好运行 paper/news/policy 三个独立语义 context；空输入也输出 terminal-success empty manifest。启动每个 context 前，由 workspace owner 鉴权并把独立 context ID 写成该 `refine:<track>` run 的 owner：

```sh
uv run --locked python -m colab_daily.lifecycle claim-run --owner <WORKSPACE_OWNER> --run <REFINE_RUN_ID> --kind refine:<track> --run-owner <CONTEXT_ID> --context <CONTEXT_METADATA_PATH>
```

其中 context metadata 的 `context_id` 必须等于 `<CONTEXT_ID>`。agent 写完 generation 后仍由 workspace owner 鉴权完成绑定：

```sh
uv run --locked python -m colab_daily.publication complete-refine --owner <WORKSPACE_OWNER> --run <REFINE_RUN_ID> --manifest <RELATIVE_MANIFEST_PATH>
```

Paper 主动读取尽可能完整的 TeX/PDF/HTML/作者材料/项目页/仓库并完成规定图片链；News/Policy 优先官方材料，受限时保守成稿并明确未知。不得用规则脚本替代语义评分、全文理解、证据判断或写作。三路 terminal 后，refine shared assembly 生成唯一 schema v3 publication set、taxonomy 和 manifest。

## 5. 冻结正式 publication

assembly 文件保持在 `working_tmp/refine_candidates/runs/.../assemblies/.../publication_set.json`。用唯一 deterministic validator 映射最终内容和有效图片到 SQLite：

```sh
uv run --locked python -m colab_daily.publication freeze --owner <WORKSPACE_OWNER> --assembly <RELATIVE_PUBLICATION_SET_PATH>
```

该命令验证 grouped selection、三 context SQLite 身份、quota/rank/score scale、成功/drop 对账、证据 materialization、分类专用 Markdown sections、taxonomy、图片真实性/来源/ownership；然后只持久化最终 mapped publication 与 durable assets。相同输入重试 no-op；已冻结后变更失败。

## 6. 正式 VitePress deployment

维护/预检可离线执行：

```sh
uv run --locked python -m colab_daily.publication preflight
uv run --locked python -m colab_daily.publication sync-template
```

生产唯一发布命令：

```sh
uv run --locked python -m colab_daily.publication deploy --cycle <CYCLE_ID>
```

它从 SQLite export 渲染，不读取已删除的中间文件；只管理固定模板文件和本 DisplayDate 的 Markdown/图片/manifest；拒绝 symlink、路径穿越、日期碰撞、未拥有修改或 staged 文件。使用专用 deploy key、禁用 SSH agent/个人凭据，创建单一确定 commit，非强制 push；unknown push 重试复用同一 commit。只有精确 remote SHA 以及全部 public artifacts 与本地 commit-bound manifest 一致时进入 `Released`。

## 7. 可选通知与 cleanup

notification 不属于 release gate：

```sh
uv run --locked python -m colab_daily.publication notify --cycle <CYCLE_ID>
```

SQLite 在 POST 前保存固定 attempt intent；仅业务响应明确接受才 confirmed。连接中断、超时或含糊响应记为 unknown，后续调用停止且不重复 POST。通知失败不重新 deploy、不改变 Released。

无论是否启用通知，只要 cycle 已 Released 且最终 publication/assets 已验证，可清理 owner 的整个 workspace：

```sh
uv run --locked python -m colab_daily.lifecycle cleanup --owner <WORKSPACE_OWNER> --cycle <CYCLE_ID>
```

cleanup 只删当前 claim 的 `working_tmp`，可从 cleaning 状态恢复；绝不删 `state/`、部署 checkout 或其他周期。通知 unknown receipt 在 SQLite 中，不需要保留 workspace。

## 完成报告

仅报告冻结周期/窗口、source 状态与数量、inventory/canonical/prior/routing 数量、三轨 routed/qualified/selected/refined、三 context/run/generation/assembly ID、frozen hash 的短标识、deployment 状态、Released、notification 状态和 cleanup。不得回显正文、分数明细、secret、private URL/ID 或认证输出。
