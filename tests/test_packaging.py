"""打包与容器配置测试"""

from pathlib import Path


def test_dockerfile_installs_project_dependencies_from_pyproject():
    """Dockerfile 不应维护一份会漂移的依赖清单"""
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "pip install --no-cache-dir ." in dockerfile
    assert "pip install --no-cache-dir \\" not in dockerfile


def test_production_env_example_requires_admin_authentication():
    """生产环境模板必须显式开启管理接口鉴权。"""
    env_example = Path(".env.production.example").read_text(encoding="utf-8")

    assert "GOLD_ENABLE_AUTH=true" in env_example
    assert "GOLD_ADMIN_API_KEY=" in env_example
    assert "GOLD_SECRET_KEY=" in env_example


def test_docker_compose_passes_security_and_runtime_config():
    """Compose 部署必须把安全与运行时配置传入容器。"""
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    required_env = [
        "GOLD_ENABLE_AUTH=${GOLD_ENABLE_AUTH:-true}",
        "GOLD_ADMIN_API_KEY=${GOLD_ADMIN_API_KEY:-}",
        "GOLD_SECRET_KEY=${GOLD_SECRET_KEY:-}",
        "GOLD_ENCRYPT_API_KEYS=${GOLD_ENCRYPT_API_KEYS:-true}",
        "GOLD_CORS_ALLOW_ORIGINS=${GOLD_CORS_ALLOW_ORIGINS:-}",
        "GOLD_TAVILY_API_KEY=${GOLD_TAVILY_API_KEY:-}",
        "GOLD_LLM_CONFIG_PATH=${GOLD_LLM_CONFIG_PATH:-/app/data/llm_config.json}",
    ]

    for env_line in required_env:
        assert env_line in compose


def test_ci_quality_gates_block_failures():
    """CI 的 lint/type/security gate 不应被 continue-on-error 或 || true 放行。"""
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "continue-on-error: true" not in ci
    assert "|| true" not in ci
    assert "ruff format . --check" in ci
