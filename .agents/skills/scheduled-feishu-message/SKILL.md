---
name: scheduled-feishu-message
description: 可选发送 SQLite 已 Released 正式日报的飞书通知；POST 前记录 delivery intent，未知结果阻断自动重发，通知不影响发布状态。
---

# Optional post-Release notification

通知不是 workflow phase，也不是 release gate。只允许在 SQLite cycle 已 `Released` 后运行：

```sh
uv run --locked python -m colab_daily.publication notify --cycle <CYCLE_ID>
```

配置只从进程环境或项目根私有 `.env` 读取：`FEISHU_WEBHOOK_URL` 与正式 `COLAB_SITE_URL`。不得把 webhook 放进 CLI、prompt、SQLite、日志或 receipt。消息从 SQLite 最终 publication 渲染，包含日期、正式报告 URL、三组数量和每组最多三个标题；不读取 working_tmp。

SQLite 在 POST 前创建固定 `released-report-v1` intent，绑定 frozen publication hash 与分组数量。明确 HTTP 200 且业务整数 code 0 才 confirmed。超时、连接断开、不可解析或未明确接受的响应一律记为 `unknown`；普通重试必须停止，不再次 POST。已 confirmed 调用直接复用。

通知失败/unknown 不撤销 Released、不触发第二次 build/commit/push，也不阻止 owner-authorized cleanup。外部 scheduler 如需 09:00 Asia/Shanghai 触发，应调用此 CLI；本项目维护不注册或修改 scheduler。
