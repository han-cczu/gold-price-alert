# 重构前接口契约清单

基线提交：`03c606f`（代码与快照 `5e2fa7f` 相同）。本表从该提交的 OpenAPI 定义提取，记录已声明的接口形状，不代表端点已通过运行验证。

## HTTP 路径与输入

| 方法 | 路径 | 路径或查询参数 | JSON 请求体 |
|------|------|----------------|-------------|
| GET | `/` | — | — |
| GET | `/api/alerts` | limit, alert_type, hours | — |
| GET | `/api/analysis` | — | — |
| GET | `/api/analysis/history` | limit, analysis_type | — |
| GET | `/api/analysis/history/{record_id}` | record_id | — |
| GET | `/api/bank-prices` | — | — |
| GET | `/api/chart/data` | hours | — |
| GET | `/api/collector/config` | — | — |
| POST | `/api/collector/config` | interval, strategy | — |
| POST | `/api/collector/fill-gaps` | hours | — |
| GET | `/api/collector/gaps` | hours | — |
| GET | `/api/collector/stats` | — | — |
| GET | `/api/config` | — | — |
| GET | `/api/convert` | price, from_unit, to_unit, from_currency, to_currency | — |
| POST | `/api/data/backup` | backup_name | — |
| GET | `/api/data/backups` | — | — |
| POST | `/api/data/cleanup` | — | — |
| GET | `/api/data/export` | format, start, end, limit | — |
| GET | `/api/data/stats` | — | — |
| GET | `/api/exchange-rate` | — | — |
| POST | `/api/llm/active` | — | SetActiveRequest |
| DELETE | `/api/llm/config` | — | — |
| GET | `/api/llm/config` | — | — |
| GET | `/api/llm/providers` | — | — |
| POST | `/api/llm/providers` | — | ProviderRequest |
| DELETE | `/api/llm/providers/{provider_id}` | provider_id | — |
| PUT | `/api/llm/providers/{provider_id}` | provider_id | ProviderUpdateRequest |
| POST | `/api/llm/providers/{provider_id}/models` | provider_id | 可选请求体 |
| POST | `/api/llm/providers/{provider_id}/test` | provider_id | 可选请求体 |
| GET | `/api/llm/status` | — | — |
| GET | `/api/notifications/config` | — | — |
| GET | `/api/notifications/config/{channel}` | channel | — |
| PUT | `/api/notifications/config/{channel}` | channel | NotificationConfigRequest |
| GET | `/api/notifications/logs` | limit, channel | — |
| POST | `/api/notifications/test/{channel}` | channel | 可选请求体 |
| GET | `/api/price/current` | — | — |
| GET | `/api/price/history` | hours, limit | — |
| GET | `/api/price/latest` | — | — |
| GET | `/api/security/status` | — | — |
| GET | `/api/smart-analysis` | — | — |
| POST | `/api/smart-analysis/refresh` | — | 可选请求体 |
| GET | `/api/source-quality` | — | — |
| GET | `/api/source-quality/confidence` | — | — |
| GET | `/health` | — | — |
| GET | `/metrics` | — | — |
| GET | `/ws/status` | — | — |

## WebSocket

端点 `/ws`；客户端发送 `{"type":"ping"}`，服务端回复 `pong`。已有服务端消息包括 `price_update`、`alert`、`config_update`。`/ws/status` 返回连接数和端点。

## 兼容重点

- `price_update.data` 保留 price、currency、source、timestamp，新增 `recorded` 表示本次报价是否入库。`false` 报价只更新当前行情，不增加图表入库记录统计。
- `alert.data` 保留 alert_type、price、message、triggered_at。
- 公开价格、告警、分析和设置页面继续使用已有 URL；`/static` 为拆出的 JS/CSS 增加资源入口。
- 管理接口继续使用 X-Admin-Key；脱敏模型密钥提交时保留已保存值。
- 数据库存储无时区 UTC；重构后的公开时间带 UTC Z，历史无时区字符串按 UTC 解释。
- 单位换算、通知配置生效、图表统计和错误响应等有意变化见 refactor-validation.md。

## 2026-09-10 可靠性修复的契约调整

保持以上路径与请求形状，调整以下异常状态和权限：

- `/health` 就绪返回 200，不就绪返回 503；健康要求数据库可用、采集任务运行、最近采样成功且未过期。状态 JSON 的已有字段保留。
- `GOLD_ENABLE_AUTH=true` 时，GET `/api/analysis` 和 POST `/api/smart-analysis/refresh` 需要管理员凭证。GET `/api/smart-analysis` 可公开返回已有缓存，但缓存缺失时需要管理员凭证才能启动模型任务。
- `/api/bank-prices` 增加 `is_fallback`、`is_stale`。没有任何有效采样时，`data=[]`，`base_price_cny`、`london_gold_cny`、`updated_at` 为 null；使用旧缓存时保留成功时的时间，标识回退/过期。
- 新分析成功后写入已有分析历史表，`smart` 共享任务仅记录一次；缓存读取不生成新历史。
- `/api/data/backups` 的 `name` 可包含 `automatic/` 前缀，表示自动快照。手动创建接口及已有备份名称校验保持可用。
- `/api/collector/config` 增加 `dedupe_max_interval_seconds`，表示同价真实采样的最长保存间隔。

修复范围、回归验证及回退点见 [可靠性修复记录](reliability-fixes.md)。
