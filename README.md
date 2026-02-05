# 金价实时监控与智能分析系统

实时监控黄金价格，自动告警通知，AI 智能分析市场动态。

## 功能特性

- 🔄 **实时数据采集** - 支持多数据源（GoldAPI.io / 新浪财经 / Mock）
- ⚠️ **智能告警** - 阈值告警、波动告警，支持多渠道通知
- 🤖 **AI 分析** - 调用大模型分析价格波动原因
- 📊 **可视化展示** - 精美的 Web 界面，支持 K 线图和走势图
- 💻 **CLI 工具** - 命令行实时监控

## 快速开始

### 1. 安装

```bash
# 克隆项目
git clone <repo-url>
cd gold-price-alert

# 安装依赖
pip install -e .
```

### 2. 配置

复制环境变量示例文件并编辑：

```bash
cp .env.example .env
```

主要配置项：

```bash
# 数据源: mock(模拟), sina(新浪财经), goldapi(GoldAPI.io)
GOLD_DATA_SOURCE=mock

# 采集间隔（秒）
GOLD_FETCH_INTERVAL=30

# 告警阈值
GOLD_ALERT_THRESHOLD_PERCENT=1.0
GOLD_ALERT_PRICE_UPPER=2500.0
GOLD_ALERT_PRICE_LOWER=1800.0

# 大模型配置（可选）
GOLD_LLM_PROVIDER=mock
GOLD_ANTHROPIC_API_KEY=your-key
GOLD_OPENAI_API_KEY=your-key
```

### 3. 运行

#### CLI 实时监控

```bash
gold-monitor monitor
```

#### Web 服务

```bash
gold-monitor web --port 8000
```

访问 http://localhost:8000 查看 Web 界面。

## CLI 命令

```bash
# 启动实时监控
gold-monitor monitor

# 获取当前金价
gold-monitor fetch

# 查看历史数据
gold-monitor history --hours 24

# 查看告警记录
gold-monitor alerts --limit 20

# 运行 AI 分析
gold-monitor analyze

# 启动 Web 服务
gold-monitor web --host 0.0.0.0 --port 8000
```

## API 接口

| 接口 | 方法 | 描述 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/api/price/current` | GET | 获取当前金价 |
| `/api/price/latest` | GET | 获取最新存储的金价 |
| `/api/price/history` | GET | 获取历史价格 |
| `/api/chart/data` | GET | 获取图表数据 |
| `/api/alerts` | GET | 获取告警历史 |
| `/api/analysis` | GET | 运行 AI 分析 |
| `/api/config` | GET | 获取配置信息 |

## 项目结构

```
gold-price-alert/
├── src/gold_monitor/
│   ├── __init__.py      # 包入口
│   ├── config.py        # 配置管理
│   ├── models.py        # 数据库模型
│   ├── collector.py     # 数据采集服务
│   ├── alert.py         # 告警模块
│   ├── analyzer.py      # AI 分析模块
│   ├── cli.py           # CLI 命令行
│   ├── web.py           # Web 服务
│   └── data_sources/    # 数据源适配器
│       ├── base.py      # 基类
│       ├── mock.py      # 模拟数据源
│       ├── sina.py      # 新浪财经
│       ├── goldapi.py   # GoldAPI.io
│       └── fallback.py  # 故障自动切换
├── tests/               # 测试用例
├── docs/                # 文档
├── pyproject.toml       # 项目配置
├── .env.example         # 环境变量示例
└── README.md            # 说明文档
```

## 技术栈

- **后端**: Python 3.10+, FastAPI, SQLAlchemy, APScheduler
- **前端**: ECharts, 原生 JavaScript
- **数据库**: SQLite (开发) / PostgreSQL (生产)
- **大模型**: Anthropic Claude / OpenAI GPT

## 告警通知渠道

支持多种通知方式：

1. **控制台** - 默认开启，终端实时显示
2. **邮件** - 配置 SMTP 服务器
3. **Webhook** - 支持钉钉、企业微信
4. **Telegram** - 配置 Bot Token 和 Chat ID

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest tests/
```

## License

MIT License
