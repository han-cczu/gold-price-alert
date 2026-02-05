"""Web API 测试"""

import pytest
from datetime import datetime, timedelta
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
