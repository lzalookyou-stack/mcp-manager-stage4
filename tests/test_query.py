"""检索式转换测试（阶段 3）。"""

from __future__ import annotations

import pytest

from app.search.query import build_search_query, extract_keywords


def test_extract_keywords_expands_chinese_requirement():
    keywords = extract_keywords("需要轻量、支持长期记忆、依赖少的 MCP 项目")
    assert "memory" in keywords
    assert "mcp" in keywords
    assert "lightweight" in keywords


def test_extract_keywords_dedups_and_drops_stopwords():
    keywords = extract_keywords("mcp mcp MCP the and 项目")
    assert keywords.count("mcp") == 1
    assert "the" not in keywords
    assert "and" not in keywords


def test_empty_requirement_yields_no_keywords():
    assert extract_keywords("") == []
    assert extract_keywords("   ") == []


def test_build_query_contains_qualifiers():
    q = build_search_query(
        "轻量 MCP 记忆项目",
        language="Python",
        min_stars=50,
        pushed_after="2024-01-01",
        topics=("mcp",),
    )
    assert "language:Python" in q.github_query
    assert "stars:>=50" in q.github_query
    assert "pushed:>=2024-01-01" in q.github_query
    assert "topic:mcp" in q.github_query
    assert q.raw_requirement == "轻量 MCP 记忆项目"


def test_build_query_rejects_bad_date():
    with pytest.raises(ValueError):
        build_search_query("x", pushed_after="2024/01/01")


def test_build_query_rejects_negative_stars():
    with pytest.raises(ValueError):
        build_search_query("x", min_stars=-1)


def test_build_query_rejects_empty_everything():
    with pytest.raises(ValueError):
        build_search_query("   ")


def test_build_query_rejects_single_char_requirement():
    """单字符/无有效关键词时必须显式报错，绝不返回空检索式（否则会被误读为"没有结果"）。"""
    with pytest.raises(ValueError):
        build_search_query("x")
    # 纯符号/停用词同样视为无有效关键词
    with pytest.raises(ValueError):
        build_search_query("!!!")


def test_build_query_notes_disclose_no_online_model():
    q = build_search_query("memory mcp")
    assert any("未使用任何在线模型" in n for n in q.notes)