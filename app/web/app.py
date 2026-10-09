"""FastAPI 应用装配。

阶段 2 范围：**只读** API + 静态控制台页面 + 基础安全中间件。
阶段 4 范围：在**保持无写接口**的前提下，增加控制台交互所需的只读端点
（远程搜索 / 深度检查 / 安全审查 / 候选对比 / SSE 事件流）。

- 仍然**不提供任何写接口**（安装/卸载等写操作在阶段 5 引入，届时必须带 CSRF 与会话）；
- 所有请求先过 Host / Origin / 体积检查。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.runtime import Runtime
from app.search.github_client import GitHubError
from app.security import SecurityError, sanitize_for_log
from app.services import (
    PluginNotFound,
    SearchUnavailable,
    ValidationError,
    dump_plugin,
)

# 不需要 Origin 校验的"安全方法"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# SSE 心跳间隔（秒）：无事件时定期发注释行，避免中间层判死连接。
_SSE_HEARTBEAT_SECONDS = 15.0
# 单个插件单次审查最多抓取的文件数上限（防止前端传入过大值）
_MAX_REVIEW_FILES_CAP = 40

# 统一安全响应头。前端不使用内联脚本 / 内联样式，因此 CSP 可以收紧到 'self'。
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _json_response(status: int, payload: dict[str, Any]) -> JSONResponse:
    """构造带统一安全头的 JSON 响应。"""
    resp = JSONResponse(status_code=status, content=payload)
    for key, value in _SECURITY_HEADERS.items():
        resp.headers[key] = value
    return resp


def create_app(runtime: Runtime | None = None) -> FastAPI:
    rt = runtime or Runtime.create()

    app = FastAPI(
        title="mcp-manager",
        version=__version__,
        docs_url=None,       # 关闭交互式文档，减少攻击面
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = rt

    # ------------------------------------------------------------------ #
    # 安全中间件
    # ------------------------------------------------------------------ #
    @app.middleware("http")
    async def _security_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        settings = rt.settings

        # 1) Host 头校验：防止 DNS rebinding 指向本机服务
        host_header = request.headers.get("host", "")
        hostname = host_header.rsplit(":", 1)[0].strip("[]").lower()
        if hostname and hostname not in {
            h.lower() for h in settings.allowed_hosts
        }:
            return _json_response(
                400, {"error": "invalid_host", "detail": "Host 头不在允许列表内"}
            )

        # 2) 体积限制
        raw_len = request.headers.get("content-length")
        if raw_len is not None:
            try:
                if int(raw_len) > settings.max_request_bytes:
                    return _json_response(413, {"error": "payload_too_large"})
            except ValueError:
                return _json_response(400, {"error": "invalid_content_length"})

        # 3) 非安全方法必须携带允许的 Origin（CSRF 基线）
        if request.method not in _SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is None or origin not in settings.allowed_origins:
                return _json_response(403, {"error": "csrf_origin_rejected"})

        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers[key] = value
        return response

    # ------------------------------------------------------------------ #
    # 异常映射（不泄露内部细节）
    # ------------------------------------------------------------------ #
    @app.exception_handler(PluginNotFound)
    async def _not_found(_: Request, exc: PluginNotFound) -> JSONResponse:
        return _json_response(
            404, {"error": "plugin_not_found", "detail": str(exc)}
        )

    @app.exception_handler(ValidationError)
    @app.exception_handler(SecurityError)
    async def _bad_request(_: Request, exc: Exception) -> JSONResponse:
        return _json_response(
            400, {"error": "invalid_request", "detail": sanitize_for_log(exc)}
        )

    @app.exception_handler(SearchUnavailable)
    async def _search_unavailable(_: Request, exc: SearchUnavailable) -> JSONResponse:
        # 显式失败：绝不退化为"返回空结果"（空结果会被误读为"没有找到"）。
        return _json_response(
            503, {"error": "search_unavailable", "detail": sanitize_for_log(exc)}
        )

    @app.exception_handler(GitHubError)
    async def _github_error(_: Request, exc: GitHubError) -> JSONResponse:
        # 502：上游（GitHub）失败，如实透出 kind，便于前端区分限流/网络/权限。
        return _json_response(
            502,
            {
                "error": "github_error",
                "kind": getattr(exc, "kind", "error"),
                "detail": sanitize_for_log(exc),
            },
        )

    # ------------------------------------------------------------------ #
    # API
    # ------------------------------------------------------------------ #
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "loopback_only": rt.settings.is_loopback,
            "plugins": rt.plugins.count(),
        }

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        return rt.plugins.stats()

    @app.get("/api/plugins")
    async def list_plugins(
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        from app.models import PluginKind

        parsed_kind = None
        if kind:
            try:
                parsed_kind = PluginKind(kind)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"未知 kind：{kind}")
        items = rt.plugins.list(kind=parsed_kind, limit=limit, offset=offset)
        return {"count": len(items), "items": [dump_plugin(p) for p in items]}

    @app.get("/api/plugins/{plugin_id}")
    async def get_plugin(plugin_id: str) -> dict[str, Any]:
        return dump_plugin(rt.plugins.get(plugin_id))

    @app.get("/api/audit")
    async def audit(limit: int = 50) -> dict[str, Any]:
        rows = rt.plugins.list_audit(limit=limit)
        return {"count": len(rows), "items": [r.__dict__ for r in rows]}

    # ------------------------------------------------------------------ #
    # 阶段 4：发现（只读 GET；不提供任何写接口）
    # ------------------------------------------------------------------ #
    @app.get("/api/query")
    async def preview_query(
        q: str,
        language: str | None = None,
        min_stars: int | None = None,
        pushed_after: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        """把自然语言需求转成 GitHub 检索式（**不发起网络请求**，便于前端预览）。"""
        filters: dict[str, Any] = {}
        if language:
            filters["language"] = language
        if min_stars is not None:
            filters["min_stars"] = min_stars
        if pushed_after:
            filters["pushed_after"] = pushed_after
        if topic:
            filters["topics"] = (topic,)
        parsed = rt.plugins.build_query(q, **filters)
        return {
            "github_query": parsed.github_query,
            "keywords": parsed.keywords,
            "qualifiers": parsed.qualifiers,
            "notes": parsed.notes,
        }

    @app.get("/api/search")
    async def search(
        q: str,
        limit: int = 10,
        language: str | None = None,
        min_stars: int | None = None,
        pushed_after: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        """远程搜索候选仓库并落库为**待审查**候选。

        未配置 GitHub 令牌时返回 503（``search_unavailable``），
        **绝不**返回空列表来假装"没有找到"。
        """
        filters: dict[str, Any] = {}
        if language:
            filters["language"] = language
        if min_stars is not None:
            filters["min_stars"] = min_stars
        if pushed_after:
            filters["pushed_after"] = pushed_after
        if topic:
            filters["topics"] = (topic,)

        if ":" in q and " " not in q.split(":", 1)[0]:
            # 已是检索式：直接透传
            items = rt.plugins.search_remote(q, limit=limit, actor="user")
            expr = q.strip()
        else:
            parsed = rt.plugins.build_query(q, **filters)
            items = rt.plugins.search_remote(parsed.github_query, limit=limit, actor="user")
            expr = parsed.github_query

        return {
            "query": expr,
            "count": len(items),
            "items": [dump_plugin(p) for p in items],
        }

    @app.get("/api/plugins/{plugin_id}/inspect")
    async def inspect_plugin(plugin_id: str) -> dict[str, Any]:
        """深度检查：固定 commit、README/测试/CI/依赖探测、Release 数。"""
        return dump_plugin(rt.plugins.inspect_remote(plugin_id, actor="user"))

    @app.get("/api/plugins/{plugin_id}/review")
    async def review_plugin(plugin_id: str, max_files: int = 20) -> dict[str, Any]:
        """对已固定 commit 的候选做静态安全审查（只读；不执行任何安装脚本）。"""
        capped = max(1, min(int(max_files), _MAX_REVIEW_FILES_CAP))
        return dump_plugin(rt.plugins.review(plugin_id, actor="user", max_files=capped))

    @app.get("/api/compare")
    async def compare(ids: str) -> dict[str, Any]:
        """对比多个候选（逗号分隔 ID）。输出证据缺口，不做"最佳推荐"。"""
        plugin_ids = [p.strip() for p in ids.split(",") if p.strip()]
        if not plugin_ids:
            raise ValidationError("ids 不能为空")
        return rt.plugins.compare(plugin_ids)

    @app.get("/api/tasks")
    async def tasks(limit: int = 50) -> dict[str, Any]:
        """任务/操作历史：直接来自审计日志（阶段 4 尚无独立任务表）。"""
        rows = rt.plugins.list_audit(limit=limit)
        items = [r.__dict__ for r in rows]
        return {"count": len(items), "items": items}

    # ------------------------------------------------------------------ #
    # 阶段 4：SSE 实时事件流（只读 GET）
    # ------------------------------------------------------------------ #
    @app.get("/api/events/stats")
    async def event_stats() -> dict[str, int]:
        return rt.events.stats()

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        """SSE 事件流。

        - 只推送**已脱敏**的审计/进度事件；不推送任何凭据；
        - 并发订阅有上限（超出返回 503）；
        - 定期发送心跳注释行，避免中间层判死连接。
        """
        sub = rt.events.subscribe()
        if sub is None:
            raise HTTPException(status_code=503, detail="订阅数已达上限")
        # 绑定当前事件循环，使跨线程发布也能投递。
        try:
            rt.events.bind_loop(asyncio.get_running_loop())
        except RuntimeError:  # pragma: no cover - 理论上不可达
            pass

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(
                            sub.queue.get(), timeout=_SSE_HEARTBEAT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    payload = json.dumps(event, ensure_ascii=False, default=str)
                    yield f"event: {event.get('type', 'message')}\ndata: {payload}\n\n"
            finally:
                rt.events.unsubscribe(sub)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------------ #
    # 静态页面
    # ------------------------------------------------------------------ #
    web_dir = rt.settings.web_dir

    @app.get("/")
    async def index() -> FileResponse:
        index_path = web_dir / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=500, detail="web/index.html 缺失")
        return FileResponse(index_path, media_type="text/html; charset=utf-8")

    if (web_dir / "assets").is_dir():
        app.mount(
            "/assets", StaticFiles(directory=str(web_dir / "assets")), name="assets"
        )

    return app