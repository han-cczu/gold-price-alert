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
