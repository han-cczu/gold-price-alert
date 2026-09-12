# 2026-09-12 加固与修复记录

基线：`1a2be18`（合并 PR #3 后的 `main`）。本轮针对只读审查列出的问题逐项修复，不改变数据库表结构，只新增索引。所有测试使用隔离临时数据库与替身，不访问真实行情、模型或通知服务。

## 修复清单

| 问题 | 修复 | 验证 |
| --- | --- | --- |
| `/api/price/current` 匿名可调且每次触发上游抓取、入库、告警评估 | `PriceService` 复用采集器最近报价；报价过期时最多每个采集间隔刷新一次，并合并并发刷新；从未取得报价且抓取失败才 503 | `tests/test_price_service.py`；`tests/test_web.py` 中 10 次匿名请求只产生 1 次抓取 |
| 保留期清理启动后 24 小时才首次运行 | 清理循环先执行再休眠，失败只记录日志不终止 | `tests/test_runtime.py` 启动后过期样本被删除 |
| `gold_prices` 缺少时间索引，且 `create_all` 不会给旧表补索引 | 新增 `timestamp`、`(currency, timestamp)`、告警/通知/分析记录时间索引；`create_tables` 逐个 `checkfirst` 补建 | `tests/test_storage.py` 旧结构数据库启动后出现全部索引且幂等 |
| 全窗口图表聚合在大表上独占单线程数据库数秒 | 采样查询改为 GROUP BY 求每桶极值后再取代表行，不再对每桶全部观测按价格排序；所有 `/api/` 路径纳入每客户端限流 | 采样语义测试全部保留；新增并列极值确定性测试；限流 429 测试 |
| DeepSeek 兜底模型 `deepseek-chat` 已下线 | 兜底改为 `deepseek-v4-flash`（`OpenAIProvider` 与 `resolve_model`） | `tests/test_analyzer.py` |
| 每次重启都重新付费生成智能分析，内存缓存重启即丢 | 启动时 `restore_cache` 从分析历史恢复 24 小时内、同一供应商和模型的结果；不匹配或过期才调用模型 | `tests/test_analysis_reliability.py`、`tests/test_runtime.py` |
| 兼容地址规范化把 `/v4` 等地址改成 `.../v4/v1` | 只有路径没有 `/v<数字>` 版本段时才补 `/v1`；前端预览同步 | `tests/test_analyzer.py` 参数化用例 |
| 钉钉/企业微信 Webhook 把 HTTP 200 当成功 | 解析响应体，`errcode` 必须为 0；generic 类型接受任意 2xx | `tests/test_channels.py` |
| Compose 自带 Nginx 时所有访客共用一个限流身份 | 网络固定子网 `172.28.0.0/24`，Nginx 固定 `172.28.0.10`，`GOLD_TRUSTED_PROXY_IPS` 默认加入该地址，生产模板同步 | `tests/test_packaging.py` 既有检查；人工核对 compose |
| 启用鉴权后匿名访客的 AI 卡片显示鉴权错误 | 页面先读公开的 `/api/security/status`；无管理员凭证时用公开的 `/api/config` 摘要标注卡片，不再调用管理员接口 | `tests/frontend/sections.test.mjs` |
| 全局 `ValueError` 兜底为 400 并回显内部文本 | 新增 `errors.InvalidInput`（400）与 `errors.ConfigurationError`（503）；其他 `ValueError` 记录日志并返回通用 500 | `tests/test_web.py` |
| 前端库依赖公共 CDN，`marked` 未锁版本 | ECharts 5.4.3、marked 18.0.12、DOMPurify 3.2.6 自托管于 `static/vendor/`，下载哈希与 jsDelivr 元数据核对一致；随 wheel 打包 | `tests/test_static_frontend.py`；Docker 冒烟脚本新增 3 个资源检查 |
| breakout/pullback 告警无图标与样式 | `market.js` 与 `dashboard.css` 补齐 | 目视 |
| 死代码与未接线指标 | 删除 `web.run_server`、各模块全局单例 getter、遗留清理调度；接通 `record_fetch`、`record_notification`、`record_analysis`、`record_db_operation` | `tests/test_observability.py` |
| 每日分析用本地时间 | 改为 UTC 零点，与存储约定一致 | 代码审阅 |
| 邮件仅支持 STARTTLS | 465 端口使用隐式 TLS | `tests/test_channels.py` |
| CI/构建整理 | `codecov-action@v5`；compose 去掉过时 `version`；Dockerfile `AS` 大写并移除无用 gcc | CI 通过为准 |
| 个人工具配置被跟踪 | `.claude/settings.local.json`、`.cursor/` 移出索引并加入 `.gitignore`（文件保留在本地） | `git status` |

## 有意调整的契约

- `/api/price/current` 返回最近成功报价而不是每次实时抓取；限流覆盖全部 `/api/` 路径。
- `/api/config` 新增 `llm_provider_name`、`llm_enabled`。
- 输入校验 400、配置不可用 503、其他内部错误 500。
- 版本号升至 0.3.0。

详见 [API 契约](api-contract.md) 末节。

## 性能基准

临时 SQLite，合成数据，30 秒间隔连续写入；仅供数量级参考。

| 场景 | 修复前 | 修复后 |
| --- | --- | --- |
| 60 万行，最新价查询 | 135 ms | 4 ms |
| 60 万行，24 小时历史 | 59 ms | 1 ms |
| 60 万行，5 年全窗口图表 | 3.4 s | 1.3 s |
| 8.6 万行（30 天保留期），全窗口图表 | 约 0.5 s | 0.2 s |

全窗口聚合仍随行数线性增长；保留期清理正常运行后，表规模上限约为 30 天的样本数。

## 回退

不改表结构。回退代码后新索引保留且无害；`static/vendor/` 与 `errors.py` 随代码一起回退即可。已保存的分析历史、备份和模型配置格式不变。

## 检查命令

```powershell
python -m pytest tests/ -q -p no:cacheprovider --basetemp=.tmp_pytest/hardening-run
python -m ruff check src/ tests/ scripts/
python -m ruff format . --check
python -m mypy src/ --ignore-missing-imports
```
