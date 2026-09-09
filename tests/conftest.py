"""测试进程级环境隔离。"""

import os
from pathlib import Path


_TEST_TMP = Path(".tmp_pytest")
_TEST_TMP.mkdir(exist_ok=True)

os.environ["GOLD_DATA_SOURCE"] = "mock"
os.environ["GOLD_LLM_PROVIDER"] = "mock"
os.environ["GOLD_DATABASE_URL"] = "sqlite:///test_web.db"
os.environ["GOLD_LLM_CONFIG_PATH"] = str(_TEST_TMP / "llm_config.json")
