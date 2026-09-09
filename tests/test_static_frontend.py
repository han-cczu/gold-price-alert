"""静态前端安全约束测试"""

from pathlib import Path


def test_frontend_sanitizes_markdown_before_inserting_html():
    """LLM/Markdown 内容进入 innerHTML 前必须经过 sanitizer。"""
    html = Path("src/gold_monitor/static/index.html").read_text(encoding="utf-8")

    assert "DOMPurify" in html
    assert "DOMPurify.sanitize(marked.parse(text))" in html


def test_frontend_escapes_provider_fields_before_inner_html():
    """用户可配置的 provider 字段不能直接拼接进 innerHTML。"""
    html = Path("src/gold_monitor/static/index.html").read_text(encoding="utf-8")

    assert "const safeName = escapeHtml(p.name);" in html
    assert "const safeBaseUrl = escapeHtml(p.base_url || '(无 URL)');" in html
    assert "${safeName}" in html
    assert "${safeBaseUrl}" in html


def test_frontend_status_messages_use_text_content():
    """设置页状态消息默认应作为文本写入，避免错误详情注入 HTML。"""
    html = Path("src/gold_monitor/static/index.html").read_text(encoding="utf-8")

    assert "status.textContent = message;" in html
