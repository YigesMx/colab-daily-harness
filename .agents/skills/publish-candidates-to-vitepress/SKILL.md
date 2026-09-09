---
name: publish-candidates-to-vitepress
description: 将 SQLite 已冻结的正式 publication 确定性渲染到 VitePress checkout，完成本地 build、受限 commit、非强制 push 和完整 public artifact 回读；确认后进入 Released。
---

# Formal VitePress publication

这是正式站点 sink，不是候选预览。输入只有 SQLite `cycle_id`；不得从 working_tmp assembly 或历史 Grist 重建。

离线只读/模板操作：

```sh
uv run --locked python -m colab_daily.publication preflight
uv run --locked python -m colab_daily.publication sync-template
```

正式有副作用命令：

```sh
uv run --locked python -m colab_daily.publication deploy --cycle <CYCLE_ID>
```

`deploy` 在任何 checkout 写入前保存 SQLite deployment intent，渲染固定 template allowlist 和该 DisplayDate 的正式 Markdown、图片、manifest；拒绝 symlink、path traversal、现有日期碰撞、未拥有修改与 staged path。build 包含站点内容、链接、资产和 loopback HTTP 验证。

Git 必须使用配置的 0600 专用 key；禁用 SSH agent、全局 Git、交互认证、个人 token 和 force push。首次只创建一个确定 commit；push 结果未知时先回读 remote SHA并复用同一 commit，不创建第二 commit。只有 exact remote commit、commit-bound artifact manifest 和每个公开 artifact bytes 全部回读一致时 SQLite 才确认 delivery 并切 `Released`。

远端/公开验证失败或 unknown 保持 `PHASE_release`，保留同一 intent 供重试。不得把 push 返回成功单独视为 release，不得清理 working_tmp，不得发送通知。
