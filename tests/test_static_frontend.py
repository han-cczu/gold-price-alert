"""Frontend entry-point integrity and executable JavaScript regression tests."""

from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "gold_monitor" / "static"


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.local_assets = []
        self.inline_handlers = []
        self.module_entries = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.inline_handlers.extend(key for key in attributes if key.startswith("on"))
        for key in ("src", "href"):
            value = attributes.get(key, "")
            if value.startswith("/static/"):
                self.local_assets.append(value.removeprefix("/static/"))
        if tag == "script" and attributes.get("type") == "module":
            self.module_entries.append(attributes.get("src"))


def test_html_assets_exist_and_events_use_module_handlers():
    parser = AssetParser()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    assert parser.module_entries == ["/static/js/dashboard.js"]
    assert not parser.inline_handlers
    assert parser.local_assets
    for asset in parser.local_assets:
        assert (STATIC / asset).is_file(), asset


def test_javascript_behavior_regressions():
    node = shutil.which("node")
    if not node:
        pytest.skip("JavaScript behavior checks require Node.js")
    scripts = sorted((ROOT / "tests" / "frontend").glob("*.test.mjs"))
    result = subprocess.run(
        [node, "--test", *map(str, scripts)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_browser_modules_parse():
    node = shutil.which("node")
    if not node:
        pytest.skip("JavaScript syntax checks require Node.js")
    for module in sorted((STATIC / "js").glob("*.js")):
        result = subprocess.run(
            [node, "--check", str(module)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, module.name + result.stderr
