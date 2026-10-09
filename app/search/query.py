"""自然语言需求 → GitHub 检索式（阶段 3）。

**定位说明**：本模块是**功能性文本转换**，用于把用户的需求描述变成 GitHub
搜索限定符。它**不是安全控制**，其输出也不会被用于放行/拦截任何操作——
安全边界一律由 ``app.security`` 的结构性校验与阶段 5 的用户授权机制承担。
"""

from __future__ import annotations

import re

from app.models import SearchQuery

# 中英文停用词（仅用于关键词提取，不涉及任何安全判断）
_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "to", "with", "without", "in", "on",
    "is", "are", "be", "that", "this", "it", "as", "by", "from", "at", "my", "i",
    "want", "need", "looking", "look", "some", "any", "please", "help", "find",
    "me", "project", "projects", "repo", "repository", "repositories", "tool",
    "tools", "library", "package", "support", "supports", "supporting", "based",
    "一个", "项目", "需要", "想要", "帮我", "找", "支持", "的", "和", "与", "或",
    "可以", "能够", "要求", "功能", "工具", "库", "框架", "尽量", "最好", "轻量",
}

# 常见需求词 → GitHub 检索关键词的同义扩展（提升召回）
_SYNONYMS = {
    "长期记忆": ["memory"],
    "记忆": ["memory"],
    "mcp": ["mcp"],
    "模型上下文协议": ["mcp"],
    "agent": ["agent"],
    "代理": ["agent"],
    "插件": ["plugin"],
    "轻量": ["lightweight"],
    "依赖少": ["zero-dependencies"],
    "本地": ["local"],
    "离线": ["offline"],
    "向量": ["vector"],
    "检索": ["search"],
    "搜索": ["search"],
    "知识库": ["knowledge-base"],
    "爬虫": ["crawler"],
    "自动化": ["automation"],
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]*|[\u4e00-\u9fff]{2,}")


def extract_keywords(requirement: str, *, max_keywords: int = 8) -> list[str]:
    """从需求文本抽取检索关键词（保序去重）。"""
    if not requirement or not requirement.strip():
        return []

    lowered = requirement.lower()
    keywords: list[str] = []
    seen: set[str] = set()

    def _add(word: str) -> None:
        key = word.lower()
        if key in seen or key in _STOPWORDS or len(key) < 2:
            return
        seen.add(key)
        keywords.append(word)

    # 1) 先做同义扩展（中文需求词 → 英文检索词）
    for phrase, expansions in _SYNONYMS.items():
        if phrase in lowered:
            for exp in expansions:
                _add(exp)

    # 2) 再抽取字面 token
    for token in _TOKEN_RE.findall(requirement):
        _add(token)

    return keywords[:max_keywords]


def build_search_query(
    requirement: str,
    *,
    language: str | None = None,
    min_stars: int | None = None,
    pushed_after: str | None = None,
    topics: tuple[str, ...] = (),
    in_name: bool = False,
) -> SearchQuery:
    """把需求转成 GitHub 检索式。

    ``pushed_after`` 形如 ``2024-01-01``（GitHub 的 ``pushed:>`` 限定符）。
    """
    keywords = extract_keywords(requirement)
    qualifiers: dict[str, str] = {}
    notes: list[str] = []

    if language:
        qualifiers["language"] = language
    if min_stars is not None:
        if min_stars < 0:
            raise ValueError("min_stars 不能为负")
        qualifiers["stars"] = f">={int(min_stars)}"
    if pushed_after:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", pushed_after):
            raise ValueError(f"pushed_after 需为 YYYY-MM-DD，实际 {pushed_after!r}")
        qualifiers["pushed"] = f">={pushed_after}"
    for topic in topics:
        qualifiers.setdefault("topic", "")
        qualifiers["topic"] = (qualifiers["topic"] + "," + topic).strip(",")

    parts: list[str] = []
    if keywords:
        if in_name:
            parts.append(" ".join(f"in:name {k}" for k in keywords[:3]))
        else:
            parts.append(" ".join(keywords))
    else:
        notes.append("未能从需求中提取关键词，检索式仅含限定符。")

    for key, value in qualifiers.items():
        parts.append(f"{key}:{value}")

    github_query = " ".join(p for p in parts if p).strip()
    if not github_query:
        raise ValueError("检索式为空：请提供需求描述或至少一个筛选条件。")

    notes.append("检索式由本地规则生成，未使用任何在线模型；可在界面上手工改写后重试。")
    return SearchQuery(
        raw_requirement=requirement,
        github_query=github_query,
        keywords=keywords,
        qualifiers=qualifiers,
        notes=notes,
    )