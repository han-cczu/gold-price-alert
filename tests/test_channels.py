"""Delivery adapters must only report success the receiving service confirmed."""

import smtplib

import aiohttp
import pytest

from gold_monitor.alerts.types import Alert, AlertType
from gold_monitor.notifications.channels import EmailNotification, WebhookNotification
from gold_monitor.time_utils import utcnow


class FakeResponse:
    def __init__(self, status, body=None, invalid_json=False):
        self.status = status
        self._body = body
        self._invalid_json = invalid_json

    async def json(self, content_type=None):
        if self._invalid_json:
            raise ValueError("not json")
        return self._body


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json):
        self.posts.append((url, json))
        return _ResponseContext(self.response)


class _ResponseContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc):
        return False


def alert():
    return Alert(AlertType.VOLATILITY, 2000.0, "test", utcnow())


@pytest.mark.parametrize("kind", ["dingtalk", "wechat"])
@pytest.mark.parametrize(
    "response, delivered",
    [
        (FakeResponse(200, {"errcode": 0, "errmsg": "ok"}), True),
        (
            FakeResponse(200, {"errcode": 310000, "errmsg": "keywords not in content"}),
            False,
        ),
        (FakeResponse(200, {"errmsg": "ok"}), False),
        (FakeResponse(200, "ok", invalid_json=True), False),
        (FakeResponse(500, {"errcode": 0}), False),
    ],
)
async def test_robot_webhooks_require_errcode_zero(
    monkeypatch, kind, response, delivered
):
    session = FakeSession(response)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda timeout=None: session)
    channel = WebhookNotification("https://example.invalid/robot", kind)
    assert await channel.send(alert()) is delivered
    assert session.posts[0][0] == "https://example.invalid/robot"
    assert session.posts[0][1]["msgtype"] == "text"


@pytest.mark.parametrize(
    "response, delivered",
    [
        (FakeResponse(200, {"errcode": 1}), True),
        (FakeResponse(204), True),
        (FakeResponse(404), False),
    ],
)
async def test_generic_webhooks_use_the_http_status_only(
    monkeypatch, response, delivered
):
    session = FakeSession(response)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda timeout=None: session)
    channel = WebhookNotification("https://example.invalid/hook", "generic")
    assert await channel.send(alert()) is delivered
    assert session.posts[0][1]["alert_type"] == "volatility"


class FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.starttls_called = False
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, username, password):
        pass

    def sendmail(self, sender, recipients, message):
        self.sent.append((sender, list(recipients)))


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.mark.parametrize(
    "port, expected_class, starttls",
    [(587, FakeSMTP, True), (25, FakeSMTP, True), (465, FakeSMTPSSL, False)],
)
async def test_email_uses_implicit_tls_on_465_and_starttls_elsewhere(
    monkeypatch, port, expected_class, starttls
):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    channel = EmailNotification(
        "smtp.example.invalid", port, "u", "p", ["a@example.invalid"]
    )
    assert await channel.send(alert()) is True
    assert len(FakeSMTP.instances) == 1
    connection = FakeSMTP.instances[0]
    assert type(connection) is expected_class
    assert connection.port == port
    assert connection.starttls_called is starttls
    assert connection.sent == [("u", ["a@example.invalid"])]
