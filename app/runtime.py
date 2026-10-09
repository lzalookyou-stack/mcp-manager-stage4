"""运行期容器。

把 ``Settings`` / SQLite 连接 / ``EventBus`` / ``PluginService``（含 GitHub 客户端）
组装成单一对象，供网页与 MCP 两侧共用（**同一个 service 实例语义**）。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field

from app.config import Settings, load_settings
from app.db import open_database
from app.events import EventBus
from app.search.github_client import GitHubClient
from app.services import PluginService


@dataclass
class Runtime:
    settings: Settings
    conn: sqlite3.Connection
    plugins: PluginService
    events: EventBus = field(default_factory=EventBus)

    @classmethod
    def create(
        cls,
        settings: Settings | None = None,
        *,
        github: GitHubClient | None = None,
        events: EventBus | None = None,
    ) -> "Runtime":
        settings = settings or load_settings()
        conn = open_database(settings.db_path)
        if github is None:
            token = os.environ.get("MCPM_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
            github = GitHubClient(token) if token else None
        bus = events if events is not None else EventBus()
        return cls(
            settings=settings,
            conn=conn,
            plugins=PluginService(conn, github=github, events=bus),
            events=bus,
        )

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover - 关闭失败不影响退出
            pass
