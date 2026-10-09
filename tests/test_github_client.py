"""GitHub 客户端测试（阶段 3）。

**全部使用注入的假 transport**，不发起任何真实网络请求。
重点验证：错误必须显式抛出、绝不伪造成空结果；令牌不出现在任何输出中。
"""

from __future__ import annotations

import json

import pytest

from app.search.github_client import GitHubClient, GitHubError


class FakeResponse:
    """模拟 urllib 的 headers 对象。"""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = {k.lower(): v for k, v in data.items()}

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._data.get(name.lower(), default)


def make_transport(routes: dict[str, tuple[int, dict[str, str], object]]):
    """构造一个按 URL 前缀匹配的假 transport。

    ``routes``：``{url 片段: (status, headers, payload)}``；
    payload 为 dict/list 时自动 JSON 序列化，为 bytes 时原样返回。
    """
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str]):
        calls.append(url)
        for fragment, (status, hdrs, payload) in routes.items():
            if fragment in url:
                if isinstance(payload, bytes):
                    body = payload
                else:
                    body = json.dumps(payload).encode("utf-8")
                return status, FakeResponse(hdrs), body
        return 404, FakeResponse({}), b'{"message": "Not Found"}'

    transport.calls = calls  # type: ignore[attr-defined]
    return transport


def test_missing_token_is_recorded_not_invented():
    client = GitHubClient(None)
    assert client.has_token is False
    assert "Authorization" not in client._headers()


def test_token_is_sent_but_never_leaked_in_headers_repr():
    client = GitHubClient("s3cr3t-token")
    headers = client._headers()
    assert headers["Authorization"] == "Bearer s3cr3t-token"
    # 对象 repr 不应包含令牌
    assert "s3cr3t-token" not in repr(client)


def test_search_repositories_parses_real_fields():
    payload = {
        "items": [
            {
                "full_name": "acme/thing",
                "name": "thing",
                "html_url": "https://github.com/acme/thing",
                "description": "a thing",
                "default_branch": "main",
                "stargazers_count": 123,
                "forks_count": 4,
                "open_issues_count": 2,
                "language": "Python",
                "archived": False,
                "pushed_at": "2026-01-02T03:04:05Z",
                "created_at": "2020-01-01T00:00:00Z",
                "updated_at": "2026-01-02T03:04:05Z",
                "topics": ["mcp"],
                "size": 100,
                "owner": {"login": "acme"},
                "license": {"spdx_id": "MIT", "name": "MIT License"},
            }
        ]
    }
    transport = make_transport({"/search/repositories": (200, {}, payload)})
    client = GitHubClient("t", transport=transport)
    repos = client.search_repositories("mcp memory")
    assert len(repos) == 1
    repo = repos[0]
    assert repo.full_name == "acme/thing"
    assert repo.stars == 123
    assert repo.license_spdx == "MIT"
    assert repo.missing_fields == []


def test_search_missing_license_is_recorded_as_missing():
    payload = {
        "items": [
            {
                "full_name": "acme/nolicense",
                "name": "nolicense",
                "html_url": "https://github.com/acme/nolicense",
                "owner": {"login": "acme"},
                "license": None,
            }
        ]
    }
    transport = make_transport({"/search/repositories": (200, {}, payload)})
    client = GitHubClient("t", transport=transport)
    repo = client.search_repositories("x")[0]
    assert repo.license_spdx is None
    assert "license" in repo.missing_fields


def test_rate_limit_raises_explicit_error_with_reset():
    transport = make_transport(
        {
            "/search/repositories": (
                403,
                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"},
                {"message": "API rate limit exceeded"},
            )
        }
    )
    client = GitHubClient("t", transport=transport)
    with pytest.raises(GitHubError) as excinfo:
        client.search_repositories("x")
    assert excinfo.value.kind == "rate_limited"
    assert excinfo.value.rate_limit["reset"] == 1700000000


def test_not_found_raises_explicit_error():
    transport = make_transport({"/repos/": (404, {}, {"message": "Not Found"})})
    client = GitHubClient("t", transport=transport)
    with pytest.raises(GitHubError) as excinfo:
        client.get_repository("acme", "ghost")
    assert excinfo.value.kind == "not_found"


def test_unauthorized_raises_explicit_error():
    transport = make_transport({"/repos/": (401, {}, {"message": "Bad credentials"})})
    client = GitHubClient("t", transport=transport)
    with pytest.raises(GitHubError) as excinfo:
        client.get_repository("acme", "thing")
    assert excinfo.value.kind == "unauthorized"


def test_server_error_raises_explicit_error():
    transport = make_transport({"/repos/": (500, {}, {"message": "boom"})})
    client = GitHubClient("t", transport=transport)
    with pytest.raises(GitHubError) as excinfo:
        client.get_repository("acme", "thing")
    assert excinfo.value.kind == "server"


def test_invalid_json_raises_server_error():
    transport = make_transport({"/repos/": (200, {}, b"<html>not json</html>")})
    client = GitHubClient("t", transport=transport)
    with pytest.raises(GitHubError) as excinfo:
        client.get_repository("acme", "thing")
    assert excinfo.value.kind == "server"


def test_network_failure_raises_network_error():
    def transport(url: str, headers: dict[str, str]):
        raise GitHubError("网络请求失败：URLError", kind="network", url=url)

    client = GitHubClient("t", transport=transport)
    with pytest.raises(GitHubError) as excinfo:
        client.get_repository("acme", "thing")
    assert excinfo.value.kind == "network"


def test_empty_query_rejected_before_network():
    transport = make_transport({})
    client = GitHubClient("t", transport=transport)
    with pytest.raises(GitHubError) as excinfo:
        client.search_repositories("   ")
    assert excinfo.value.kind == "invalid"
    assert transport.calls == []  # type: ignore[attr-defined]


def test_get_latest_commit_returns_lowercase_sha():
    sha = "A" * 40
    transport = make_transport({"/commits": (200, {}, [{"sha": sha}])})
    client = GitHubClient("t", transport=transport)
    assert client.get_latest_commit("acme", "thing") == "a" * 40


def test_get_latest_commit_rejects_short_sha():
    transport = make_transport({"/commits": (200, {}, [{"sha": "abc"}])})
    client = GitHubClient("t", transport=transport)
    with pytest.raises(GitHubError) as excinfo:
        client.get_latest_commit("acme", "thing")
    assert excinfo.value.kind == "server"


def test_get_file_decodes_base64_and_flags_truncation():
    import base64

    content = b"echo hello\n"
    payload = {
        "type": "file",
        "name": "install.sh",
        "size": len(content),
        "content": base64.b64encode(content).decode(),
        "sha": "deadbeef",
    }
    transport = make_transport({"/contents/install.sh": (200, {}, payload)})
    client = GitHubClient("t", transport=transport)
    f = client.get_file("acme", "thing", "install.sh")
    assert f.content == "echo hello\n"
    assert f.truncated is False


def test_get_file_rejects_directory():
    transport = make_transport({"/contents/dir": (200, {}, [{"name": "a"}])})
    client = GitHubClient("t", transport=transport)
    with pytest.raises(GitHubError) as excinfo:
        client.get_file("acme", "thing", "dir")
    assert excinfo.value.kind == "not_found"


def test_error_to_dict_has_no_token():
    err = GitHubError("boom", kind="server", status=500)
    assert "token" not in json.dumps(err.to_dict(), ensure_ascii=False).lower()