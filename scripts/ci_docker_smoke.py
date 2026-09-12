"""Check the running Mock application from inside the isolated CI container."""

import math
import time

import httpx


JS_MODULES = (
    "dashboard",
    "api",
    "chart",
    "market",
    "analysis",
    "settings",
    "realtime",
    "render",
    "state",
)

VENDOR_ASSETS = ("echarts.min.js", "marked.umd.js", "purify.min.js")


def checked_get(client, path):
    response = client.get(path)
    if response.status_code != 200:
        raise RuntimeError(f"{path}: expected HTTP 200, got {response.status_code}")
    return response


def wait_until_ready(client):
    deadline = time.monotonic() + 60
    last_error = "server has not responded"
    while time.monotonic() < deadline:
        try:
            health = checked_get(client, "/health").json()
            if (
                health.get("status") == "healthy"
                and health.get("database") == "connected"
                and health.get("collector_running") is True
                and health.get("data_source") == "mock"
                and health.get("data_source_healthy") is True
                and health.get("last_price") is not None
            ):
                print("PASS /health: database connected, Mock collector running")
                return
            last_error = str(health)
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise RuntimeError(
        f"Mock application did not become ready within 60s: {last_error}"
    )


def main():
    with httpx.Client(
        base_url="http://127.0.0.1:8000", timeout=2, trust_env=False
    ) as client:
        wait_until_ready(client)

        price = checked_get(client, "/api/price/current").json()
        value = price.get("price")
        if (
            not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            or price.get("currency") != "USD"
            or price.get("source") != "mock"
        ):
            raise RuntimeError(f"Unexpected Mock price response: {price}")
        print("PASS /api/price/current: positive finite Mock USD quote")

        page = checked_get(client, "/")
        if (
            not page.headers.get("content-type", "").startswith("text/html")
            or "<html" not in page.text.lower()
            or "/static/js/dashboard.js" not in page.text
            or "/static/css/dashboard.css" not in page.text
        ):
            raise RuntimeError("Homepage must be HTML referencing the dashboard assets")
        print("PASS /: dashboard HTML and asset references")

        assets = [(f"/static/js/{name}.js", "javascript") for name in JS_MODULES]
        assets.extend(
            (f"/static/vendor/{name}", "javascript") for name in VENDOR_ASSETS
        )
        assets.append(("/static/css/dashboard.css", "text/css"))
        for path, media_type in assets:
            response = checked_get(client, path)
            if not response.content or media_type not in response.headers.get(
                "content-type", ""
            ):
                raise RuntimeError(f"{path}: empty asset or incorrect content type")
            print(f"PASS {path}")
    print(
        "Docker smoke test passed: health, price, HTML, 9 JS modules, "
        "3 vendored libraries and 1 CSS file"
    )


if __name__ == "__main__":
    main()
