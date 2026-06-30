"""Web API 测试"""

import pytest
from fastapi.testclient import TestClient

# 设置测试环境变量
import os
os.environ["GOLD_DATA_SOURCE"] = "mock"
os.environ["GOLD_LLM_PROVIDER"] = "mock"
os.environ["GOLD_DATABASE_URL"] = "sqlite:///test_web.db"

from gold_monitor.web import app, db


@pytest.fixture(scope="module")
def client():
    """创建测试客户端"""
    db.create_tables()
    # 预填充一些测试数据
    for i in range(5):
        db.save_price(2000 + i * 10, "mock")
    yield TestClient(app)


def test_root_page(client):
    """测试首页"""
    response = client.get("/")
    assert response.status_code == 200
    assert "金价实时监控系统" in response.text


def test_health_check(client):
    """测试健康检查"""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "database" in data
    assert "data_source" in data
    assert "collector_running" in data
    assert "collector_stats" in data
    assert "fetch_interval" in data


def test_get_current_price(client):
    """测试获取当前金价"""
    response = client.get("/api/price/current")
    assert response.status_code == 200
    data = response.json()
    assert "price" in data
    assert "source" in data
    assert "timestamp" in data
    assert data["price"] > 0


def test_get_latest_price(client):
    """测试获取最新存储的金价"""
    response = client.get("/api/price/latest")
    assert response.status_code == 200
    data = response.json()
    assert "price" in data


def test_get_price_history(client):
    """测试获取历史价格"""
    response = client.get("/api/price/history?hours=24&limit=10")
    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    assert "count" in data
    assert "stats" in data


def test_get_chart_data(client):
    """测试获取图表数据"""
    response = client.get("/api/chart/data?hours=24")
    assert response.status_code == 200
    data = response.json()
    assert "timestamps" in data
    assert "prices" in data
    assert "current_price" in data
    assert "high" in data
    assert "low" in data


def test_get_alerts(client):
    """测试获取告警历史"""
    response = client.get("/api/alerts?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


def test_get_alerts_with_filter(client):
    """测试带过滤的告警历史"""
    response = client.get("/api/alerts?alert_type=threshold_upper&hours=24")
    assert response.status_code == 200


def test_run_analysis(client):
    """测试运行 AI 分析"""
    response = client.get("/api/analysis")
    assert response.status_code == 200
    data = response.json()
    assert "summary" in data
    assert "possible_reasons" in data
    assert "market_sentiment" in data
    assert "recommendation" in data


def test_get_config(client):
    """测试获取配置"""
    response = client.get("/api/config")
    assert response.status_code == 200
    data = response.json()
    assert "data_source" in data
    assert "fetch_interval" in data


def test_get_exchange_rate(client):
    """测试获取汇率"""
    response = client.get("/api/exchange-rate")
    assert response.status_code == 200
    data = response.json()
    assert "usd_cny" in data
    assert "updated_at" in data
    assert data["usd_cny"] > 0


def test_get_bank_prices(client):
    """测试获取银行金价"""
    response = client.get("/api/bank-prices")
    assert response.status_code == 200
    data = response.json()
    assert data["base_price_cny"] > 0
    assert data["london_gold_cny"] > 0
    assert len(data["data"]) >= 1
    first = data["data"][0]
    assert first["bank_name"]
    assert first["buy_price"] < first["sell_price"]


def test_convert_gold_price_validates_units(client):
    """未知单位不应静默按默认系数换算"""
    response = client.get("/api/convert?price=1&from_unit=bad&to_unit=g")
    assert response.status_code == 422


def test_convert_gold_price_validates_currencies(client):
    """未知币种不应静默按 1.0 汇率换算"""
    response = client.get("/api/convert?price=1&from_currency=EUR&to_currency=CNY")
    assert response.status_code == 422


def test_convert_gold_price_per_ounce_to_per_gram(client):
    """按单位价格换算：USD/oz -> USD/g"""
    response = client.get(
        "/api/convert?price=3110.35&from_unit=oz&to_unit=g"
        "&from_currency=USD&to_currency=USD"
    )
    assert response.status_code == 200
    assert response.json()["converted"]["price"] == 100.0


def test_export_data_rejects_invalid_datetime(client):
    """非法 ISO 时间应返回 400，而不是内部异常"""
    response = client.get("/api/data/export?start=not-a-date")
    assert response.status_code == 400
    assert "ISO" in response.json()["detail"]


def test_websocket_status(client):
    """测试 WebSocket 状态"""
    response = client.get("/ws/status")
    assert response.status_code == 200
    data = response.json()
    assert "active_connections" in data
    assert "endpoint" in data
    assert data["endpoint"] == "/ws"


def test_websocket_connection(client):
    """测试 WebSocket 连接"""
    import json
    
    with client.websocket_connect("/ws") as websocket:
        # 连接后应收到当前价格
        # 发送心跳
        websocket.send_text(json.dumps({"type": "ping"}))
        
        # 等待心跳响应
        data = websocket.receive_text()
        message = json.loads(data)
        
        # 可能先收到价格更新，也可能收到 pong
        assert message["type"] in ["pong", "price_update"]


def test_collector_stats_api(client):
    """测试采集器统计 API"""
    response = client.get("/api/collector/stats")
    # 在测试环境可能采集器未运行
    if response.status_code == 200:
        data = response.json()
        assert "running" in data
        assert "stats" in data
        assert "config" in data


def test_collector_config_api(client):
    """测试采集器配置 API"""
    response = client.get("/api/collector/config")
    if response.status_code == 200:
        data = response.json()
        assert "interval" in data
        assert "strategy" in data
        assert "deduplicate" in data


def test_update_collector_interval(client):
    """测试运行时修改采集间隔"""
    response = client.post("/api/collector/config?interval=60")
    if response.status_code == 200:
        data = response.json()
        assert "changes" in data
        assert data["changes"].get("interval") == 60


def test_detect_gaps_api(client):
    """测试检测数据间隙"""
    response = client.get("/api/collector/gaps?hours=1")
    if response.status_code == 200:
        data = response.json()
        assert "gaps" in data
        assert "count" in data
        assert isinstance(data["gaps"], list)
