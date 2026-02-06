# 金价实时监控与智能分析系统

实时监控黄金价格，自动告警通知，AI 智能分析市场动态。

**纯 Web 模式** - 一个服务包含所有功能：Web 界面 + 数据采集 + 告警检测 + AI 分析。

## 功能特性

- 🔄 **自动数据采集** - 后台自动采集金价，支持多数据源
- ⚠️ **智能告警** - 阈值告警、波动告警，自动检测
- 🤖 **AI 分析** - 调用大模型分析价格波动原因
- 📊 **可视化展示** - 走势图、银行金价对比、汇率换算
- 🐳 **Docker 部署** - 一键启动，开箱即用

## 快速开始

### 方式一：本地运行

```bash
# 1. 安装
pip install -e .

# 2. 启动服务
gold-monitor

# 3. 访问 http://localhost:8000
```

### 方式二：Docker 运行

```bash
# 1. 启动
docker-compose up -d

# 2. 查看日志
docker-compose logs -f

# 3. 访问 http://localhost:8000
```

## 配置

复制 `.env.example` 为 `.env` 并编辑：

```bash
# 数据源: mock(模拟), sina(新浪), goldapi(GoldAPI.io)
GOLD_DATA_SOURCE=mock

# 采集间隔（秒）
GOLD_FETCH_INTERVAL=30

# 告警阈值
GOLD_ALERT_THRESHOLD_PERCENT=1.0
GOLD_ALERT_PRICE_UPPER=2500.0
GOLD_ALERT_PRICE_LOWER=1800.0

# 大模型: mock, anthropic, openai
GOLD_LLM_PROVIDER=mock
GOLD_ANTHROPIC_API_KEY=your-key
GOLD_OPENAI_API_KEY=your-key
```

## 命令行参数

```bash
gold-monitor [选项]

选项:
  --host TEXT     监听地址 (默认: 0.0.0.0)
  --port INTEGER  监听端口 (默认: 8000)
  --reload        开发模式，自动重载
  --version       显示版本号
```

## API 接口

| 接口 | 说明 |
|------|------|
| `/` | Web 界面 |
| `/health` | 健康检查（含采集状态） |
| `/api/price/current` | 获取当前金价 |
| `/api/price/history` | 历史价格 |
| `/api/chart/data` | 图表数据 |
| `/api/alerts` | 告警历史 |
| `/api/analysis` | AI 分析 |
| `/api/bank-prices` | 银行金价 |
| `/api/exchange-rate` | 汇率 |
| `/api/convert` | 价格换算 |
| `/docs` | API 文档 |

## 项目结构

```
gold-price-alert/
├── src/gold_monitor/
│   ├── web.py           # Web 服务（含数据采集）
│   ├── cli.py           # 命令行入口
│   ├── config.py        # 配置管理
│   ├── models.py        # 数据库模型
│   ├── alert.py         # 告警模块
│   ├── analyzer.py      # AI 分析
│   └── data_sources/    # 数据源
├── tests/               # 测试
├── docs/                # 文档
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## 技术栈

- **后端**: Python 3.10+, FastAPI, SQLAlchemy
- **前端**: ECharts, 原生 JavaScript
- **数据库**: SQLite
- **大模型**: Claude / OpenAI / Mock

## License

MIT
