"""Container readiness check; HTTP success alone is insufficient."""

import httpx


def check_health(client: httpx.Client) -> None:
    response = client.get("http://127.0.0.1:8000/health")
    response.raise_for_status()
    health = response.json()
    if not (
        health.get("status") == "healthy"
        and health.get("database") == "connected"
        and health.get("collector_running") is True
        and health.get("data_source_healthy") is True
    ):
        raise RuntimeError("Application is not ready")


def main() -> None:
    with httpx.Client(timeout=5, trust_env=False) as client:
        check_health(client)


if __name__ == "__main__":
    main()
