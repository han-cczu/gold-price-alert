"""FastAPI Web 服务"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .config import settings
from .models import Database
from .collector import DataCollector, create_data_source
from .alert import AlertMonitor, ConsoleNotification
from .analyzer import GoldAnalyzer


app = FastAPI(
    title="金价实时监控系统",
    description="实时获取金价、告警通知、AI 分析",
    version="0.1.0"
)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局实例
db = Database(settings.database_url)
db.create_tables()


# ============ 数据模型 ============

class PriceResponse(BaseModel):
    price: float
    currency: str = "USD"
    source: str
    timestamp: datetime


class PriceHistoryResponse(BaseModel):
    data: list[PriceResponse]
    count: int
    stats: dict


class ChartDataResponse(BaseModel):
    timestamps: list[str]
    prices: list[float]
    current_price: float
    price_change: float
    price_change_percent: float
    high: float
    low: float


class AlertResponse(BaseModel):
    id: int
    alert_type: str
    price: float
    message: str
    triggered_at: datetime


class AnalysisResponse(BaseModel):
    summary: str
    possible_reasons: list[str]
    market_sentiment: str
    recommendation: str
    generated_at: datetime


class HealthResponse(BaseModel):
    status: str
    database: str
    data_source: str
    data_source_healthy: bool
    last_price: Optional[float]
    last_update: Optional[datetime]
    uptime_seconds: Optional[float]


class ExchangeRateResponse(BaseModel):
    usd_cny: float
    updated_at: datetime


class BankPriceResponse(BaseModel):
    bank_name: str
    bank_code: str
    buy_price: float
    sell_price: float
    timestamp: datetime
    product_name: str


class BankPricesResponse(BaseModel):
    data: list[BankPriceResponse]
    base_price_cny: float
    london_gold_cny: float
    updated_at: datetime


# ============ API 路由 ============

@app.get("/", response_class=HTMLResponse)
async def root():
    """首页 - 金价走势图仪表盘"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>金价实时监控系统</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
                background: #f5f5f5;
                color: #333;
                min-height: 100vh;
            }
            .container { max-width: 1200px; margin: 0 auto; padding: 20px; }

            /* 标题区域 */
            .header {
                text-align: center;
                padding: 30px 0;
            }
            .header h1 {
                font-size: 32px;
                color: #1a1a2e;
                margin-bottom: 10px;
            }
            .header .subtitle {
                color: #666;
                font-size: 14px;
            }

            /* 日期时间显示 */
            .datetime-bar {
                background: #fff;
                border-radius: 8px;
                padding: 15px 30px;
                text-align: center;
                margin-bottom: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .datetime-bar .date { color: #333; font-size: 16px; }
            .datetime-bar .time { color: #f5a623; font-size: 16px; margin-left: 20px; }

            /* 分析切换按钮 */
            .analysis-tabs {
                display: flex;
                justify-content: center;
                gap: 10px;
                margin-bottom: 20px;
            }
            .tab-btn {
                padding: 10px 25px;
                border: none;
                border-radius: 20px;
                cursor: pointer;
                font-size: 14px;
                transition: all 0.3s;
            }
            .tab-btn.active {
                background: #f5a623;
                color: #fff;
            }
            .tab-btn:not(.active) {
                background: #fff;
                color: #666;
            }
            .tab-btn:hover:not(.active) {
                background: #f0f0f0;
            }

            /* AI 分析卡片 */
            .analysis-card {
                background: #fff;
                border-radius: 8px;
                padding: 25px 30px;
                margin-bottom: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .analysis-row {
                display: flex;
                margin-bottom: 15px;
            }
            .analysis-label {
                width: 100px;
                color: #333;
                font-weight: 500;
            }
            .analysis-value {
                flex: 1;
                color: #666;
            }
            .analysis-btn {
                display: block;
                width: 150px;
                margin: 20px auto 0;
                padding: 12px 30px;
                background: #f5a623;
                color: #fff;
                border: none;
                border-radius: 25px;
                cursor: pointer;
                font-size: 14px;
                transition: background 0.3s;
            }
            .analysis-btn:hover {
                background: #e09000;
            }

            /* 价格显示区域 */
            .price-section {
                background: #fff;
                border-radius: 8px;
                padding: 30px;
                margin-bottom: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .price-header {
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                margin-bottom: 20px;
                flex-wrap: wrap;
                gap: 20px;
            }
            .price-main {
                display: flex;
                align-items: baseline;
                gap: 15px;
            }
            .price-value {
                font-size: 42px;
                font-weight: bold;
                color: #1a1a2e;
            }
            .price-change {
                font-size: 18px;
                font-weight: 500;
            }
            .price-change.up { color: #00c853; }
            .price-change.down { color: #ff5252; }

            /* 切换按钮组 */
            .switch-group {
                display: flex;
                gap: 5px;
                background: #f5f5f5;
                border-radius: 6px;
                padding: 4px;
            }
            .switch-btn {
                padding: 8px 16px;
                border: none;
                background: transparent;
                border-radius: 4px;
                cursor: pointer;
                font-size: 13px;
                color: #666;
                transition: all 0.2s;
            }
            .switch-btn.active {
                background: #fff;
                color: #333;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            }
            .switch-btn:hover:not(.active) {
                color: #333;
            }

            /* 图表控制栏 */
            .chart-controls {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 15px;
                flex-wrap: wrap;
                gap: 15px;
            }

            /* 时间周期选择 */
            .period-group {
                display: flex;
                gap: 5px;
            }
            .period-btn {
                padding: 6px 14px;
                border: 1px solid #ddd;
                background: #fff;
                border-radius: 4px;
                cursor: pointer;
                font-size: 13px;
                color: #666;
                transition: all 0.2s;
            }
            .period-btn.active {
                background: #1a1a2e;
                color: #fff;
                border-color: #1a1a2e;
            }
            .period-btn:hover:not(.active) {
                border-color: #999;
            }

            /* 图表容器 */
            #chart {
                width: 100%;
                height: 400px;
            }

            /* 统计信息 */
            .stats-row {
                display: flex;
                justify-content: space-around;
                padding-top: 20px;
                border-top: 1px solid #eee;
                margin-top: 20px;
            }
            .stat-item {
                text-align: center;
            }
            .stat-label {
                font-size: 12px;
                color: #999;
                margin-bottom: 5px;
            }
            .stat-value {
                font-size: 18px;
                font-weight: 500;
                color: #333;
            }
            .stat-value.high { color: #00c853; }
            .stat-value.low { color: #ff5252; }

            /* 历史记录表格 */
            .history-section {
                background: #fff;
                border-radius: 8px;
                padding: 25px 30px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .history-section h2 {
                font-size: 18px;
                margin-bottom: 20px;
                color: #333;
            }
            table {
                width: 100%;
                border-collapse: collapse;
            }
            th, td {
                padding: 12px 15px;
                text-align: left;
                border-bottom: 1px solid #eee;
            }
            th {
                color: #999;
                font-weight: 500;
                font-size: 13px;
            }
            td {
                color: #333;
                font-size: 14px;
            }

            /* 加载动画 */
            .loading {
                color: #999;
                font-style: italic;
            }

            /* 银行金价对比 */
            .bank-section {
                background: #fff;
                border-radius: 8px;
                padding: 25px 30px;
                margin-bottom: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .bank-section h2 {
                font-size: 18px;
                margin-bottom: 20px;
                color: #333;
            }
            .section-subtitle {
                font-size: 13px;
                color: #999;
                font-weight: normal;
            }
            .bank-cards {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
                gap: 15px;
            }
            .bank-card {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                border-radius: 12px;
                padding: 20px;
                color: #fff;
                transition: transform 0.2s;
            }
            .bank-card:hover {
                transform: translateY(-3px);
            }
            .bank-card .bank-name {
                font-size: 14px;
                opacity: 0.9;
                margin-bottom: 12px;
            }
            .bank-card .price-row {
                display: flex;
                justify-content: space-between;
                margin-bottom: 8px;
            }
            .bank-card .price-label {
                font-size: 12px;
                opacity: 0.8;
            }
            .bank-card .price-value {
                font-size: 16px;
                font-weight: bold;
            }
            .bank-card .spread {
                font-size: 11px;
                opacity: 0.7;
                text-align: right;
                margin-top: 8px;
            }
            .london-gold-info {
                margin-top: 15px;
                padding-top: 15px;
                border-top: 1px solid #eee;
                color: #666;
                font-size: 14px;
            }
            #london-gold-price {
                color: #f5a623;
                font-weight: bold;
            }

            /* 汇率换算工具 */
            .converter-section {
                background: #fff;
                border-radius: 8px;
                padding: 25px 30px;
                margin-bottom: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .converter-section h2 {
                font-size: 18px;
                margin-bottom: 20px;
                color: #333;
            }
            .converter-row {
                display: flex;
                align-items: center;
                gap: 20px;
                flex-wrap: wrap;
            }
            .converter-input-group {
                display: flex;
                gap: 8px;
                flex: 1;
                min-width: 280px;
            }
            .converter-input-group input {
                flex: 1;
                padding: 12px 15px;
                border: 1px solid #ddd;
                border-radius: 6px;
                font-size: 16px;
            }
            .converter-input-group select {
                padding: 12px 10px;
                border: 1px solid #ddd;
                border-radius: 6px;
                background: #fff;
                font-size: 14px;
                cursor: pointer;
            }
            .converter-arrow {
                font-size: 24px;
                color: #999;
            }
            .converter-info {
                margin-top: 15px;
                color: #999;
                font-size: 13px;
            }
            #current-rate {
                color: #f5a623;
                font-weight: bold;
            }

            /* 告警历史 */
            .alerts-section {
                background: #fff;
                border-radius: 8px;
                padding: 25px 30px;
                margin-bottom: 20px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.05);
            }
            .alerts-section h2 {
                font-size: 18px;
                margin-bottom: 20px;
                color: #333;
            }
            .alert-item {
                display: flex;
                align-items: center;
                padding: 12px 15px;
                border-radius: 8px;
                margin-bottom: 10px;
                background: #f8f9fa;
            }
            .alert-item.threshold_upper { border-left: 4px solid #ff5252; }
            .alert-item.threshold_lower { border-left: 4px solid #ffc107; }
            .alert-item.volatility { border-left: 4px solid #2196f3; }
            .alert-icon {
                font-size: 20px;
                margin-right: 15px;
            }
            .alert-content {
                flex: 1;
            }
            .alert-message {
                font-size: 14px;
                color: #333;
            }
            .alert-time {
                font-size: 12px;
                color: #999;
                margin-top: 4px;
            }
            .alert-price {
                font-size: 16px;
                font-weight: bold;
                color: #333;
            }
            .no-alerts {
                color: #999;
                text-align: center;
                padding: 20px;
            }

            /* 响应式 */
            @media (max-width: 768px) {
                .price-value { font-size: 32px; }
                .chart-controls { flex-direction: column; align-items: stretch; }
                .period-group { justify-content: center; flex-wrap: wrap; }
                #chart { height: 300px; }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>黄金价格走势图</h1>
                <p class="subtitle">实时监控金价走势，AI 智能分析市场动态</p>
            </div>

            <div class="datetime-bar">
                <span class="date" id="current-date">加载中...</span>
                <span class="time" id="current-time">--:--:--</span>
            </div>

            <div class="analysis-tabs">
                <button class="tab-btn active" onclick="switchAnalysis('daily')">日线分析</button>
                <button class="tab-btn" onclick="switchAnalysis('minute')">分钟线分析</button>
            </div>

            <div class="analysis-card">
                <div class="analysis-row">
                    <span class="analysis-label">操作建议：</span>
                    <span class="analysis-value" id="ai-recommendation">加载中...</span>
                </div>
                <div class="analysis-row">
                    <span class="analysis-label">结论：</span>
                    <span class="analysis-value" id="ai-conclusion">加载中...</span>
                </div>
                <button class="analysis-btn" onclick="showDetailedAnalysis()">查看详细分析</button>
            </div>

            <div class="price-section">
                <div class="price-header">
                    <div class="price-main">
                        <span class="price-value" id="current-price">$0.00</span>
                        <span class="price-change" id="price-change">+0.00%</span>
                    </div>
                    <div style="display: flex; gap: 15px; flex-wrap: wrap;">
                        <div class="switch-group" id="currency-switch">
                            <button class="switch-btn active" data-currency="USD">USD</button>
                            <button class="switch-btn" data-currency="CNY">CNY</button>
                        </div>
                        <div class="switch-group" id="unit-switch">
                            <button class="switch-btn" data-unit="KG">KG</button>
                            <button class="switch-btn" data-unit="G">G</button>
                            <button class="switch-btn active" data-unit="OZ">OZ</button>
                        </div>
                    </div>
                </div>

                <div class="chart-controls">
                    <div></div>
                    <div class="period-group">
                        <button class="period-btn active" data-period="1D">1D</button>
                        <button class="period-btn" data-period="7D">7D</button>
                        <button class="period-btn" data-period="1M">1M</button>
                        <button class="period-btn" data-period="6M">6M</button>
                        <button class="period-btn" data-period="1Y">1Y</button>
                        <button class="period-btn" data-period="5Y">5Y</button>
                    </div>
                </div>

                <div id="chart"></div>

                <div class="stats-row">
                    <div class="stat-item">
                        <div class="stat-label">最高价</div>
                        <div class="stat-value high" id="stat-high">$0.00</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">最低价</div>
                        <div class="stat-value low" id="stat-low">$0.00</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">平均价</div>
                        <div class="stat-value" id="stat-avg">$0.00</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-label">数据点</div>
                        <div class="stat-value" id="stat-count">0</div>
                    </div>
                </div>
            </div>

            <!-- 银行金价对比 -->
            <div class="bank-section">
                <h2>银行金价对比 <span class="section-subtitle">(Au99.99 人民币/克)</span></h2>
                <div class="bank-cards" id="bank-cards">
                    <div class="loading">加载中...</div>
                </div>
                <div class="london-gold-info">
                    <span>伦敦金基准价: </span>
                    <span id="london-gold-price">¥0.00/克</span>
                </div>
            </div>

            <!-- 汇率换算工具 -->
            <div class="converter-section">
                <h2>金价换算工具</h2>
                <div class="converter-row">
                    <div class="converter-input-group">
                        <input type="number" id="convert-input" value="2050" step="0.01" placeholder="输入金额">
                        <select id="convert-from-unit">
                            <option value="oz">盎司 (oz)</option>
                            <option value="g">克 (g)</option>
                            <option value="kg">公斤 (kg)</option>
                        </select>
                        <select id="convert-from-currency">
                            <option value="USD">美元 (USD)</option>
                            <option value="CNY">人民币 (CNY)</option>
                        </select>
                    </div>
                    <div class="converter-arrow">→</div>
                    <div class="converter-input-group">
                        <input type="text" id="convert-output" readonly placeholder="结果">
                        <select id="convert-to-unit">
                            <option value="oz">盎司 (oz)</option>
                            <option value="g" selected>克 (g)</option>
                            <option value="kg">公斤 (kg)</option>
                        </select>
                        <select id="convert-to-currency">
                            <option value="USD">美元 (USD)</option>
                            <option value="CNY" selected>人民币 (CNY)</option>
                        </select>
                    </div>
                </div>
                <div class="converter-info">
                    当前汇率: 1 USD = <span id="current-rate">7.20</span> CNY
                </div>
            </div>

            <!-- 告警历史 -->
            <div class="alerts-section">
                <h2>最近告警记录</h2>
                <div id="alerts-container">
                    <div class="loading">加载中...</div>
                </div>
            </div>

            <div class="history-section">
                <h2>最近价格记录</h2>
                <table>
                    <thead>
                        <tr>
                            <th>时间</th>
                            <th>价格 (USD)</th>
                            <th>来源</th>
                        </tr>
                    </thead>
                    <tbody id="history-table">
                        <tr><td colspan="3" class="loading">加载中...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 详细分析弹窗 -->
        <div id="analysis-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:1000;">
            <div style="position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); background:#fff; border-radius:12px; padding:30px; max-width:600px; width:90%; max-height:80vh; overflow-y:auto;">
                <h2 style="margin-bottom:20px; color:#333;">AI 分析报告</h2>
                <div id="modal-content"></div>
                <button onclick="closeModal()" style="margin-top:20px; padding:10px 30px; background:#f5a623; color:#fff; border:none; border-radius:20px; cursor:pointer;">关闭</button>
            </div>
        </div>

        <script>
            // 全局状态
            let chart = null;
            let currentCurrency = 'USD';
            let currentUnit = 'OZ';
            let currentPeriod = '1D';
            let chartData = { timestamps: [], prices: [] };
            let analysisData = null;

            // 单位换算系数
            const unitConversions = {
                'OZ': 1,
                'G': 31.1035,  // 1 盎司 = 31.1035 克
                'KG': 31103.5  // 1 盎司 = 0.0311035 公斤
            };

            // 汇率（从 API 获取）
            let exchangeRates = {
                'USD': 1,
                'CNY': 7.2
            };

            // 获取实时汇率
            async function fetchExchangeRate() {
                try {
                    const resp = await fetch('/api/exchange-rate');
                    if (resp.ok) {
                        const data = await resp.json();
                        exchangeRates.CNY = data.usd_cny;
                    }
                } catch (e) {
                    console.warn('获取汇率失败，使用默认值');
                }
            }

            // 初始化图表
            function initChart() {
                chart = echarts.init(document.getElementById('chart'));
                window.addEventListener('resize', () => chart.resize());
            }

            // 更新图表
            function updateChart(data) {
                const prices = data.prices.map(p => convertPrice(p));
                const currencySymbol = currentCurrency === 'USD' ? '$' : '¥';
                const unitLabel = currentUnit === 'OZ' ? '盎司' : (currentUnit === 'G' ? '克' : '公斤');

                const option = {
                    tooltip: {
                        trigger: 'axis',
                        formatter: function(params) {
                            const time = params[0].axisValue;
                            const price = params[0].data;
                            return `${time}<br/>价格: ${currencySymbol}${price.toFixed(2)}/${unitLabel}`;
                        },
                        backgroundColor: 'rgba(255,255,255,0.95)',
                        borderColor: '#eee',
                        borderWidth: 1,
                        textStyle: { color: '#333' }
                    },
                    grid: {
                        left: '3%',
                        right: '4%',
                        bottom: '3%',
                        top: '5%',
                        containLabel: true
                    },
                    xAxis: {
                        type: 'category',
                        data: data.timestamps,
                        boundaryGap: false,
                        axisLine: { lineStyle: { color: '#ddd' } },
                        axisLabel: { color: '#999', fontSize: 11 },
                        axisTick: { show: false }
                    },
                    yAxis: {
                        type: 'value',
                        axisLine: { show: false },
                        axisLabel: {
                            color: '#999',
                            fontSize: 11,
                            formatter: val => currencySymbol + val.toFixed(2)
                        },
                        splitLine: { lineStyle: { color: '#f0f0f0', type: 'dashed' } }
                    },
                    series: [{
                        name: '金价',
                        type: 'line',
                        data: prices,
                        smooth: true,
                        symbol: 'none',
                        lineStyle: {
                            color: '#4ecdc4',
                            width: 2
                        },
                        areaStyle: {
                            color: {
                                type: 'linear',
                                x: 0, y: 0, x2: 0, y2: 1,
                                colorStops: [
                                    { offset: 0, color: 'rgba(78, 205, 196, 0.4)' },
                                    { offset: 1, color: 'rgba(78, 205, 196, 0.05)' }
                                ]
                            }
                        },
                        markLine: {
                            silent: true,
                            symbol: 'none',
                            data: [{
                                yAxis: prices[prices.length - 1],
                                lineStyle: { color: '#4ecdc4', type: 'dashed', width: 1 },
                                label: {
                                    position: 'end',
                                    formatter: currencySymbol + prices[prices.length - 1]?.toFixed(2),
                                    color: '#4ecdc4',
                                    backgroundColor: 'rgba(78, 205, 196, 0.1)',
                                    padding: [4, 8],
                                    borderRadius: 4
                                }
                            }]
                        }
                    }]
                };

                chart.setOption(option);
            }

            // 价格换算
            function convertPrice(priceUSD) {
                const rate = exchangeRates[currentCurrency];
                const unitFactor = unitConversions[currentUnit];
                return (priceUSD * rate) / unitFactor;
            }

            // 获取图表数据
            async function fetchChartData() {
                const periodHours = {
                    '1D': 24,
                    '7D': 168,
                    '1M': 720,
                    '6M': 4320,
                    '1Y': 8760,
                    '5Y': 43800
                };

                const hours = periodHours[currentPeriod] || 24;
                const resp = await fetch(`/api/chart/data?hours=${hours}`);
                const data = await resp.json();
                chartData = data;

                updateChart(data);
                updateStats(data);
                updatePriceDisplay(data);
            }

            // 更新价格显示
            function updatePriceDisplay(data) {
                const currencySymbol = currentCurrency === 'USD' ? '$' : '¥';
                const price = convertPrice(data.current_price);
                const changePercent = data.price_change_percent;

                document.getElementById('current-price').textContent =
                    currencySymbol + price.toFixed(2);

                const changeEl = document.getElementById('price-change');
                const sign = changePercent >= 0 ? '+' : '';
                changeEl.textContent = sign + changePercent.toFixed(2) + '%';
                changeEl.className = 'price-change ' + (changePercent >= 0 ? 'up' : 'down');
            }

            // 更新统计信息
            function updateStats(data) {
                const currencySymbol = currentCurrency === 'USD' ? '$' : '¥';
                const high = convertPrice(data.high);
                const low = convertPrice(data.low);
                const prices = data.prices.map(p => convertPrice(p));
                const avg = prices.reduce((a, b) => a + b, 0) / prices.length || 0;

                document.getElementById('stat-high').textContent = currencySymbol + high.toFixed(2);
                document.getElementById('stat-low').textContent = currencySymbol + low.toFixed(2);
                document.getElementById('stat-avg').textContent = currencySymbol + avg.toFixed(2);
                document.getElementById('stat-count').textContent = data.prices.length;
            }

            // 获取历史记录
            async function fetchHistory() {
                const resp = await fetch('/api/price/history?limit=10');
                const data = await resp.json();

                const tbody = document.getElementById('history-table');
                if (data.data.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="3">暂无数据</td></tr>';
                    return;
                }

                tbody.innerHTML = data.data.map(p => `
                    <tr>
                        <td>${new Date(p.timestamp).toLocaleString('zh-CN')}</td>
                        <td>$${p.price.toFixed(2)}</td>
                        <td>${p.source}</td>
                    </tr>
                `).join('');
            }

            // 获取 AI 分析
            async function fetchAnalysis() {
                try {
                    const resp = await fetch('/api/analysis');
                    if (!resp.ok) {
                        document.getElementById('ai-recommendation').textContent = '数据不足，请先采集数据';
                        document.getElementById('ai-conclusion').textContent = '运行 monitor 命令采集数据后可查看分析';
                        return;
                    }
                    analysisData = await resp.json();

                    document.getElementById('ai-recommendation').textContent = analysisData.recommendation || '暂无建议';
                    document.getElementById('ai-conclusion').textContent = analysisData.summary || '暂无结论';
                } catch (e) {
                    document.getElementById('ai-recommendation').textContent = '获取分析失败';
                    document.getElementById('ai-conclusion').textContent = e.message;
                }
            }

            // 切换分析模式
            function switchAnalysis(mode) {
                document.querySelectorAll('.analysis-tabs .tab-btn').forEach(btn => {
                    btn.classList.remove('active');
                });
                event.target.classList.add('active');
                // 可以根据 mode 加载不同的分析
                fetchAnalysis();
            }

            // 显示详细分析
            function showDetailedAnalysis() {
                const modal = document.getElementById('analysis-modal');
                const content = document.getElementById('modal-content');

                if (analysisData) {
                    content.innerHTML = `
                        <p><strong>市场情绪：</strong> ${analysisData.market_sentiment}</p>
                        <p style="margin-top:15px;"><strong>可能原因：</strong></p>
                        <ul style="margin-left:20px; margin-top:10px;">
                            ${analysisData.possible_reasons.map(r => `<li style="margin-bottom:8px;">${r}</li>`).join('')}
                        </ul>
                        <p style="margin-top:15px;"><strong>操作建议：</strong> ${analysisData.recommendation}</p>
                        <p style="margin-top:15px; color:#999; font-size:12px;">
                            生成时间: ${new Date(analysisData.generated_at).toLocaleString('zh-CN')}
                        </p>
                    `;
                } else {
                    content.innerHTML = '<p>暂无分析数据</p>';
                }

                modal.style.display = 'block';
            }

            // 关闭弹窗
            function closeModal() {
                document.getElementById('analysis-modal').style.display = 'none';
            }

            // 更新日期时间
            function updateDateTime() {
                const now = new Date();
                const year = now.getFullYear();
                const month = String(now.getMonth() + 1).padStart(2, '0');
                const day = String(now.getDate()).padStart(2, '0');
                document.getElementById('current-date').textContent = `${year}年${month}月${day}日`;
                document.getElementById('current-time').textContent =
                    now.toLocaleTimeString('zh-CN', { hour12: false });
            }

            // 绑定事件
            function bindEvents() {
                // 币种切换
                document.querySelectorAll('#currency-switch .switch-btn').forEach(btn => {
                    btn.addEventListener('click', () => {
                        document.querySelectorAll('#currency-switch .switch-btn').forEach(b => b.classList.remove('active'));
                        btn.classList.add('active');
                        currentCurrency = btn.dataset.currency;
                        if (chartData.prices.length > 0) {
                            updateChart(chartData);
                            updateStats(chartData);
                            updatePriceDisplay(chartData);
                        }
                    });
                });

                // 单位切换
                document.querySelectorAll('#unit-switch .switch-btn').forEach(btn => {
                    btn.addEventListener('click', () => {
                        document.querySelectorAll('#unit-switch .switch-btn').forEach(b => b.classList.remove('active'));
                        btn.classList.add('active');
                        currentUnit = btn.dataset.unit;
                        if (chartData.prices.length > 0) {
                            updateChart(chartData);
                            updateStats(chartData);
                            updatePriceDisplay(chartData);
                        }
                    });
                });

                // 时间周期切换
                document.querySelectorAll('.period-btn').forEach(btn => {
                    btn.addEventListener('click', () => {
                        document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
                        btn.classList.add('active');
                        currentPeriod = btn.dataset.period;
                        fetchChartData();
                    });
                });

                // 点击弹窗外部关闭
                document.getElementById('analysis-modal').addEventListener('click', (e) => {
                    if (e.target.id === 'analysis-modal') closeModal();
                });

                // 换算工具事件
                const convertInputs = ['convert-input', 'convert-from-unit', 'convert-from-currency', 'convert-to-unit', 'convert-to-currency'];
                convertInputs.forEach(id => {
                    document.getElementById(id).addEventListener('change', doConvert);
                    if (id === 'convert-input') {
                        document.getElementById(id).addEventListener('input', doConvert);
                    }
                });
            }

            // 获取银行金价
            async function fetchBankPrices() {
                try {
                    const resp = await fetch('/api/bank-prices');
                    if (!resp.ok) return;
                    const data = await resp.json();

                    const container = document.getElementById('bank-cards');
                    const colors = [
                        'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
                        'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)',
                        'linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)',
                        'linear-gradient(135deg, #43e97b 0%, #38f9d7 100%)',
                        'linear-gradient(135deg, #fa709a 0%, #fee140 100%)',
                        'linear-gradient(135deg, #a8edea 0%, #fed6e3 100%)'
                    ];

                    container.innerHTML = data.data.map((bank, i) => `
                        <div class="bank-card" style="background: ${colors[i % colors.length]}">
                            <div class="bank-name">${bank.bank_name}</div>
                            <div class="price-row">
                                <span class="price-label">买入价</span>
                                <span class="price-value">¥${bank.buy_price.toFixed(2)}</span>
                            </div>
                            <div class="price-row">
                                <span class="price-label">卖出价</span>
                                <span class="price-value">¥${bank.sell_price.toFixed(2)}</span>
                            </div>
                            <div class="spread">价差: ¥${(bank.sell_price - bank.buy_price).toFixed(2)}</div>
                        </div>
                    `).join('');

                    document.getElementById('london-gold-price').textContent = `¥${data.london_gold_cny.toFixed(2)}/克`;
                } catch (e) {
                    console.warn('获取银行金价失败', e);
                }
            }

            // 获取告警历史
            async function fetchAlerts() {
                try {
                    const resp = await fetch('/api/alerts?limit=5');
                    if (!resp.ok) return;
                    const data = await resp.json();

                    const container = document.getElementById('alerts-container');

                    if (data.length === 0) {
                        container.innerHTML = '<div class="no-alerts">暂无告警记录</div>';
                        return;
                    }

                    const icons = {
                        'threshold_upper': '🔴',
                        'threshold_lower': '🟡',
                        'volatility': '⚡'
                    };

                    container.innerHTML = data.map(alert => `
                        <div class="alert-item ${alert.alert_type}">
                            <span class="alert-icon">${icons[alert.alert_type] || '⚠️'}</span>
                            <div class="alert-content">
                                <div class="alert-message">${alert.message}</div>
                                <div class="alert-time">${new Date(alert.triggered_at).toLocaleString('zh-CN')}</div>
                            </div>
                            <div class="alert-price">$${alert.price.toFixed(2)}</div>
                        </div>
                    `).join('');
                } catch (e) {
                    console.warn('获取告警历史失败', e);
                }
            }

            // 执行换算
            async function doConvert() {
                const price = parseFloat(document.getElementById('convert-input').value) || 0;
                const fromUnit = document.getElementById('convert-from-unit').value;
                const toUnit = document.getElementById('convert-to-unit').value;
                const fromCurrency = document.getElementById('convert-from-currency').value;
                const toCurrency = document.getElementById('convert-to-currency').value;

                try {
                    const resp = await fetch(`/api/convert?price=${price}&from_unit=${fromUnit}&to_unit=${toUnit}&from_currency=${fromCurrency}&to_currency=${toCurrency}`);
                    if (resp.ok) {
                        const data = await resp.json();
                        const symbol = toCurrency === 'USD' ? '$' : '¥';
                        document.getElementById('convert-output').value = `${symbol}${data.converted.price.toFixed(2)}`;
                        document.getElementById('current-rate').textContent = data.exchange_rate.toFixed(4);
                    }
                } catch (e) {
                    console.warn('换算失败', e);
                }
            }

            // 初始化
            document.addEventListener('DOMContentLoaded', async () => {
                initChart();
                bindEvents();
                updateDateTime();
                setInterval(updateDateTime, 1000);

                await Promise.all([
                    fetchExchangeRate(),
                    fetchChartData(),
                    fetchHistory(),
                    fetchAnalysis(),
                    fetchBankPrices(),
                    fetchAlerts()
                ]);

                // 初始换算
                doConvert();

                // 自动刷新
                setInterval(fetchChartData, 30000);
                setInterval(fetchHistory, 30000);
                setInterval(fetchExchangeRate, 1800000);  // 30分钟更新汇率
                setInterval(fetchBankPrices, 60000);      // 1分钟更新银行金价
                setInterval(fetchAlerts, 30000);          // 30秒更新告警
            });
        </script>
    </body>
    </html>
    """


# 应用启动时间
_app_start_time = datetime.utcnow()


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """健康检查"""
    last_record = db.get_latest_price()

    # 检查数据源健康状态
    data_source_healthy = False
    try:
        source = create_data_source()
        data_source_healthy = await source.health_check()
    except Exception:
        data_source_healthy = False

    # 检查数据库连接
    db_status = "connected"
    try:
        from sqlalchemy import text
        with db.get_session() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        db_status = "disconnected"

    # 计算运行时间
    uptime = (datetime.utcnow() - _app_start_time).total_seconds()

    # 判断整体健康状态
    status = "healthy" if db_status == "connected" else "unhealthy"

    return HealthResponse(
        status=status,
        database=db_status,
        data_source=settings.data_source,
        data_source_healthy=data_source_healthy,
        last_price=last_record.price if last_record else None,
        last_update=last_record.timestamp if last_record else None,
        uptime_seconds=uptime
    )


@app.get("/api/chart/data", response_model=ChartDataResponse)
async def get_chart_data(
    hours: int = Query(default=24, ge=1, le=43800, description="查询小时数")
):
    """获取图表数据"""
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=hours)

    records = db.get_prices_in_range(start_time, end_time)

    if not records:
        # 如果没有数据，尝试获取最新价格
        try:
            source = create_data_source()
            price_data = await source.fetch_price()
            db.save_price(price_data.price, price_data.source)
            records = [db.get_latest_price()]
        except Exception:
            return ChartDataResponse(
                timestamps=[],
                prices=[],
                current_price=0,
                price_change=0,
                price_change_percent=0,
                high=0,
                low=0
            )

    # 根据时间范围调整时间格式
    if hours <= 24:
        time_format = "%H:%M"
    elif hours <= 168:
        time_format = "%m-%d %H:%M"
    else:
        time_format = "%Y-%m-%d"

    timestamps = [r.timestamp.strftime(time_format) for r in records]
    prices = [r.price for r in records]

    current_price = prices[-1] if prices else 0
    first_price = prices[0] if prices else 0
    price_change = current_price - first_price
    price_change_percent = (price_change / first_price * 100) if first_price > 0 else 0

    return ChartDataResponse(
        timestamps=timestamps,
        prices=prices,
        current_price=current_price,
        price_change=price_change,
        price_change_percent=price_change_percent,
        high=max(prices) if prices else 0,
        low=min(prices) if prices else 0
    )


@app.get("/api/price/current", response_model=PriceResponse)
async def get_current_price():
    """获取当前金价"""
    source = create_data_source()
    price_data = await source.fetch_price()

    # 保存到数据库
    db.save_price(price_data.price, price_data.source)

    return PriceResponse(
        price=price_data.price,
        source=price_data.source,
        timestamp=price_data.timestamp
    )


@app.get("/api/price/latest", response_model=PriceResponse)
async def get_latest_price():
    """获取最新存储的金价（不请求数据源）"""
    record = db.get_latest_price()
    if not record:
        raise HTTPException(status_code=404, detail="没有价格数据")

    return PriceResponse(
        price=record.price,
        source=record.source,
        timestamp=record.timestamp
    )


@app.get("/api/price/history", response_model=PriceHistoryResponse)
async def get_price_history(
    hours: int = Query(default=24, ge=1, le=168, description="查询小时数"),
    limit: int = Query(default=100, ge=1, le=1000, description="最大记录数")
):
    """获取历史价格"""
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=hours)

    records = db.get_prices_in_range(start_time, end_time)
    if limit and len(records) > limit:
        records = records[-limit:]

    prices = [r.price for r in records] if records else []
    stats = {
        "max": max(prices) if prices else 0,
        "min": min(prices) if prices else 0,
        "avg": sum(prices) / len(prices) if prices else 0,
        "count": len(prices)
    }

    return PriceHistoryResponse(
        data=[
            PriceResponse(
                price=r.price,
                source=r.source,
                timestamp=r.timestamp
            ) for r in records
        ],
        count=len(records),
        stats=stats
    )


@app.get("/api/alerts", response_model=list[AlertResponse])
async def get_alerts(
    limit: int = Query(default=50, ge=1, le=200, description="最大记录数"),
    alert_type: Optional[str] = Query(default=None, description="告警类型过滤: threshold_upper, threshold_lower, volatility"),
    hours: Optional[int] = Query(default=None, ge=1, le=720, description="时间范围（小时）")
):
    """获取告警历史（支持按类型和时间过滤）"""
    from .models import AlertRecord
    with db.get_session() as session:
        query = session.query(AlertRecord)

        # 按类型过滤
        if alert_type:
            query = query.filter(AlertRecord.alert_type == alert_type)

        # 按时间范围过滤
        if hours:
            start_time = datetime.utcnow() - timedelta(hours=hours)
            query = query.filter(AlertRecord.triggered_at >= start_time)

        records = query.order_by(
            AlertRecord.triggered_at.desc()
        ).limit(limit).all()

        return [
            AlertResponse(
                id=r.id,
                alert_type=r.alert_type,
                price=r.price,
                message=r.message,
                triggered_at=r.triggered_at
            ) for r in records
        ]


@app.get("/api/analysis", response_model=AnalysisResponse)
async def run_analysis():
    """运行 AI 分析"""
    records = db.get_recent_prices(limit=20)
    if len(records) < 2:
        raise HTTPException(status_code=400, detail="数据不足，无法分析")

    current_price = records[0].price
    oldest_price = records[-1].price
    price_change = current_price - oldest_price

    recent_prices = [(r.timestamp, r.price) for r in reversed(records)]

    analyzer = GoldAnalyzer()
    report = await analyzer.analyze_volatility(
        current_price=current_price,
        price_change=price_change,
        recent_prices=recent_prices,
        time_window_minutes=settings.alert_volatility_window
    )

    return AnalysisResponse(
        summary=report.summary,
        possible_reasons=report.possible_reasons,
        market_sentiment=report.market_sentiment,
        recommendation=report.recommendation,
        generated_at=report.generated_at
    )


@app.get("/api/config")
async def get_config():
    """获取当前配置（脱敏）"""
    return {
        "data_source": settings.data_source,
        "fetch_interval": settings.fetch_interval,
        "alert_threshold_percent": settings.alert_threshold_percent,
        "alert_price_upper": settings.alert_price_upper,
        "alert_price_lower": settings.alert_price_lower,
        "llm_provider": settings.llm_provider
    }


# 汇率缓存（简单内存缓存，避免频繁请求）
_exchange_rate_cache = {"usd_cny": 7.2, "updated_at": None}


@app.get("/api/exchange-rate", response_model=ExchangeRateResponse)
async def get_exchange_rate():
    """获取 USD/CNY 汇率"""
    import httpx
    from datetime import timedelta

    # 检查缓存是否有效（30分钟）
    if (_exchange_rate_cache["updated_at"] and
            datetime.utcnow() - _exchange_rate_cache["updated_at"] < timedelta(minutes=30)):
        return ExchangeRateResponse(
            usd_cny=_exchange_rate_cache["usd_cny"],
            updated_at=_exchange_rate_cache["updated_at"]
        )

    # 尝试从免费 API 获取汇率
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # 使用央行汇率接口或其他免费接口
            resp = await client.get(
                "https://api.exchangerate-api.com/v4/latest/USD"
            )
            if resp.status_code == 200:
                data = resp.json()
                rate = data.get("rates", {}).get("CNY", 7.2)
                _exchange_rate_cache["usd_cny"] = rate
                _exchange_rate_cache["updated_at"] = datetime.utcnow()
    except Exception:
        # 获取失败使用缓存或默认值
        if _exchange_rate_cache["updated_at"] is None:
            _exchange_rate_cache["updated_at"] = datetime.utcnow()

    return ExchangeRateResponse(
        usd_cny=_exchange_rate_cache["usd_cny"],
        updated_at=_exchange_rate_cache["updated_at"]
    )


@app.get("/api/bank-prices", response_model=BankPricesResponse)
async def get_bank_prices():
    """获取各银行金价"""
    from .data_sources.bank import get_bank_source

    bank_source = get_bank_source()
    bank_prices = await bank_source.fetch_all_bank_prices()
    base_price = await bank_source.fetch_base_price_cny()

    # 计算伦敦金人民币价格（作为基准）
    london_gold_cny = base_price

    return BankPricesResponse(
        data=[
            BankPriceResponse(
                bank_name=p.bank_name,
                bank_code=p.bank_code,
                buy_price=p.buy_price,
                sell_price=p.sell_price,
                timestamp=p.timestamp,
                product_name=p.product_name
            ) for p in bank_prices
        ],
        base_price_cny=base_price,
        london_gold_cny=london_gold_cny,
        updated_at=datetime.utcnow()
    )


@app.get("/api/convert")
async def convert_gold_price(
    price: float = Query(..., description="价格"),
    from_unit: str = Query(default="oz", description="原单位: oz, g, kg"),
    to_unit: str = Query(default="g", description="目标单位: oz, g, kg"),
    from_currency: str = Query(default="USD", description="原币种: USD, CNY"),
    to_currency: str = Query(default="CNY", description="目标币种: USD, CNY")
):
    """金价单位和币种换算"""
    # 单位换算系数（相对于盎司）
    unit_factors = {
        "oz": 1.0,
        "g": 31.1035,
        "kg": 0.0311035
    }

    # 获取汇率
    usd_cny = _exchange_rate_cache.get("usd_cny", 7.2)
    currency_rates = {
        ("USD", "CNY"): usd_cny,
        ("CNY", "USD"): 1 / usd_cny,
        ("USD", "USD"): 1.0,
        ("CNY", "CNY"): 1.0
    }

    # 先转换单位
    from_factor = unit_factors.get(from_unit, 1.0)
    to_factor = unit_factors.get(to_unit, 1.0)
    converted_price = price * from_factor / to_factor

    # 再转换币种
    rate = currency_rates.get((from_currency, to_currency), 1.0)
    converted_price *= rate

    return {
        "original": {
            "price": price,
            "unit": from_unit,
            "currency": from_currency
        },
        "converted": {
            "price": round(converted_price, 2),
            "unit": to_unit,
            "currency": to_currency
        },
        "exchange_rate": usd_cny
    }


def run_server(host: str = "0.0.0.0", port: int = 8000):
    """启动 Web 服务"""
    import uvicorn
    uvicorn.run(app, host=host, port=port)
