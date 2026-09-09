---
name: release-candidate-record
description: 将 refine-owned schema v3 assembly 通过正式 validator 冻结到 SQLite，持久化最终 publication 与最终展示资产；不访问 final publication、不部署站点、不改变语义选择。
---

# Freeze final publication into SQLite

这是历史 skill 名称下的现行兼容入口；权威 sink 已是 SQLite，不存在 Grist Records 或人工阶段。输入只能是当前 owner 下唯一 assembly：

`working_tmp/refine_candidates/runs/<REFINE_RUN_ID>/assemblies/<ASSEMBLY_GENERATION_ID>/publication_set.json`

执行：

```sh
uv run --locked python -m colab_daily.publication freeze --owner <WORKSPACE_OWNER> --assembly <RELATIVE_PUBLICATION_SET_PATH>
```

命令回读 SQLite lifecycle/rating/refine ownership，验证三轨隔离、quota、rank/score scale、taxonomy、分类专用正文、证据、图片 acquisition audit 与成功/drop 闭包。它将 transient paths 映射为最终 publication 字段，把验证通过的显示图片复制到 content-addressed durable assets；不把 inventory/rating/refine manifests、下载正文、base64 或目录快照写进 DB。

相同完整输入重试为 no-op；同周期已冻结后改变 publication、validation 或图片 bytes 必须失败。空 Paper 在上游已满足合法条件时可冻结。不要直接调用 `storage freeze-publication` 拼造 validation booleans；只有 publication validator 可以制造成功 checks。

本 skill 不 deploy、不 notify、不清理 working_tmp，也不重新评分、精读、分类、排序或补位。
