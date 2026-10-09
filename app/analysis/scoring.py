"""可解释质量评分（阶段 3）。

权重（见 docs/architecture.md）：
维护状态 20% / 代码质量与测试 25% / 安全 30% / 兼容性 15% / 社区与许可证 10%。

纪律：
- 每个维度都必须给出**理由**（``Evidence``），不允许黑箱总分。
- Star 数**仅作辅助信号**，只对"社区"维度有轻微影响，不参与主体判断。
- **严重安全风险不可被抵消**：存在 critical 发现时 ``vetoed=True``，总分直接归零。
- 证据不足时给**保守的低分**并标注【未检查】，绝不用默认高分填充。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.models import (
    Evidence,
    EvidenceLevel,
    RepoMetrics,
    RiskLevel,
    ScoreBreakdown,
    SecurityReport,
)

# 单个维度内各加分项的权重（每个维度和为 1.0）
_MAINTENANCE_WEIGHTS = {"recency": 0.6, "not_archived": 0.4}
_QUALITY_WEIGHTS = {"readme": 0.3, "tests": 0.4, "ci": 0.2, "deps": 0.1}
_COMPAT_WEIGHTS = {"language_known": 0.4, "has_license": 0.3, "recent_release": 0.3}
_COMMUNITY_WEIGHTS = {"license_quality": 0.6, "stars": 0.4}

_STALE_DAYS = 365 * 2       # 超过 2 年未推送视为维护停滞
_RECENT_DAYS = 365          # 1 年内有推送视为活跃
_STRONG_STARS = 500
_OK_STARS = 50


def _days_since(moment: datetime | None, *, now: datetime) -> float | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment).total_seconds() / 86400.0


def _maintenance(metrics: RepoMetrics, now: datetime) -> tuple[float, list[Evidence]]:
    evidence: list[Evidence] = []
    score = 0.0

    age = _days_since(metrics.pushed_at, now=now)
    if age is None:
        recency = 0.0
        evidence.append(
            Evidence(
                statement="无法获取最近推送时间，按最保守处理给 0 分",
                level=EvidenceLevel.UNCHECKED,
                source="GitHub API: repos.pushed_at",
            )
        )
    elif age <= _RECENT_DAYS:
        recency = 100.0
        evidence.append(
            Evidence(
                statement=f"最近 {int(age)} 天内有推送，维护活跃",
                level=EvidenceLevel.VERIFIED,
                source="GitHub API: repos.pushed_at",
            )
        )
    elif age >= _STALE_DAYS:
        recency = 0.0
        evidence.append(
            Evidence(
                statement=f"已 {int(age)} 天无推送（超过 2 年），维护停滞",
                level=EvidenceLevel.VERIFIED,
                source="GitHub API: repos.pushed_at",
            )
        )
    else:
        recency = round(100.0 * (1 - (age - _RECENT_DAYS) / (_STALE_DAYS - _RECENT_DAYS)), 2)
        evidence.append(
            Evidence(
                statement=f"{int(age)} 天前有推送，维护状态中等",
                level=EvidenceLevel.VERIFIED,
                source="GitHub API: repos.pushed_at",
            )
        )
    score += recency * _MAINTENANCE_WEIGHTS["recency"]

    if metrics.archived:
        evidence.append(
            Evidence(
                statement="仓库已归档（archived），不再接受维护",
                level=EvidenceLevel.VERIFIED,
                source="GitHub API: repos.archived",
            )
        )
    else:
        score += 100.0 * _MAINTENANCE_WEIGHTS["not_archived"]
        evidence.append(
            Evidence(
                statement="仓库未归档",
                level=EvidenceLevel.VERIFIED,
                source="GitHub API: repos.archived",
            )
        )
    return round(score, 2), evidence


def _quality(metrics: RepoMetrics) -> tuple[float, list[Evidence]]:
    evidence: list[Evidence] = []
    score = 0.0

    if metrics.has_readme:
        score += 100.0 * _QUALITY_WEIGHTS["readme"]
    evidence.append(
        Evidence(
            statement=("仓库包含 README" if metrics.has_readme else "未检测到 README"),
            level=EvidenceLevel.VERIFIED if metrics.has_readme else EvidenceLevel.INFERRED,
            source="GitHub API: contents(README*)",
        )
    )

    if metrics.has_tests:
        score += 100.0 * _QUALITY_WEIGHTS["tests"]
    evidence.append(
        Evidence(
            statement=("检测到测试目录/文件" if metrics.has_tests else "未检测到测试"),
            level=EvidenceLevel.VERIFIED if metrics.has_tests else EvidenceLevel.INFERRED,
            source="GitHub API: contents(tests/ test_* *_test*)",
        )
    )

    if metrics.has_ci:
        score += 100.0 * _QUALITY_WEIGHTS["ci"]
    evidence.append(
        Evidence(
            statement=("检测到 CI 配置" if metrics.has_ci else "未检测到 CI 配置"),
            level=EvidenceLevel.VERIFIED if metrics.has_ci else EvidenceLevel.INFERRED,
            source="GitHub API: contents(.github/workflows)",
        )
    )

    if metrics.has_dependency_manifest:
        score += 100.0 * _QUALITY_WEIGHTS["deps"]
    evidence.append(
        Evidence(
            statement=(
                "存在依赖清单（便于审计供应链）"
                if metrics.has_dependency_manifest
                else "未检测到依赖清单"
            ),
            level=EvidenceLevel.VERIFIED if metrics.has_dependency_manifest else EvidenceLevel.INFERRED,
            source="GitHub API: contents(requirements.txt pyproject.toml package.json 等)",
        )
    )
    return round(score, 2), evidence


def _security(report: SecurityReport | None) -> tuple[float, list[Evidence], bool]:
    """安全维度。

    - **未审查**：不给分（0），证据等级【未检查】，并且**不**参与否决。
    - **已审查**：按最高风险等级扣分；critical 直接 0 分并触发一票否决。
    """
    if report is None:
        return 0.0, [
            Evidence(
                statement="尚未进行安全审查，安全维度按 0 分处理（保守）",
                level=EvidenceLevel.UNCHECKED,
                source="SecurityReport: 缺失",
            )
        ], False

    if report.vetoed:
        return 0.0, [
            Evidence(
                statement=(
                    f"存在 {sum(1 for f in report.findings if f.severity is RiskLevel.CRITICAL)} "
                    "条严重（critical）风险发现，安全一票否决，总分归零"
                ),
                level=EvidenceLevel.VERIFIED,
                source="SecurityReport.findings",
            )
        ], True

    penalty = {
        RiskLevel.NONE: 0.0,
        RiskLevel.LOW: 15.0,
        RiskLevel.MEDIUM: 40.0,
        RiskLevel.HIGH: 70.0,
    }[report.risk_level]
    score = max(0.0, 100.0 - penalty)
    evidence = [
        Evidence(
            statement=(
                f"审查覆盖 {len(report.scanned_files)} 个文件，最高风险等级为 "
                f"{report.risk_level.value}，共 {len(report.findings)} 条发现"
            ),
            level=EvidenceLevel.VERIFIED,
            source="SecurityReport",
        ),
        Evidence(
            statement=SecurityReport.DISCLAIMER,
            level=EvidenceLevel.VERIFIED,
            source="SecurityReport.DISCLAIMER",
        ),
    ]
    return round(score, 2), evidence, False


def _compatibility(metrics: RepoMetrics) -> tuple[float, list[Evidence]]:
    evidence: list[Evidence] = []
    score = 0.0

    if metrics.language:
        score += 100.0 * _COMPAT_WEIGHTS["language_known"]
        evidence.append(
            Evidence(
                statement=f"主要语言为 {metrics.language}",
                level=EvidenceLevel.VERIFIED,
                source="GitHub API: repos.language",
            )
        )
    else:
        evidence.append(
            Evidence(
                statement="语言未知，兼容性无法判断",
                level=EvidenceLevel.UNCHECKED,
                source="GitHub API: repos.language",
            )
        )

    if metrics.license_spdx:
        score += 100.0 * _COMPAT_WEIGHTS["has_license"]
        evidence.append(
            Evidence(
                statement=f"许可证：{metrics.license_spdx}（来源：{metrics.license_source.value}）",
                level=EvidenceLevel.VERIFIED,
                source="GitHub API: repos.license.spdx_id",
            )
        )
    else:
        evidence.append(
            Evidence(
                statement="未获取到明确许可证，兼容性存在法律不确定性",
                level=EvidenceLevel.UNCHECKED,
                source="GitHub API: repos.license",
            )
        )

    if metrics.releases_count > 0:
        score += 100.0 * _COMPAT_WEIGHTS["recent_release"]
        evidence.append(
            Evidence(
                statement=f"存在 {metrics.releases_count} 个 Release，可锁定版本",
                level=EvidenceLevel.VERIFIED,
                source="GitHub API: repos.releases",
            )
        )
    else:
        evidence.append(
            Evidence(
                statement="未发布 Release，只能锁定 commit",
                level=EvidenceLevel.VERIFIED,
                source="GitHub API: repos.releases",
            )
        )
    return round(score, 2), evidence


def _community(metrics: RepoMetrics) -> tuple[float, list[Evidence]]:
    evidence: list[Evidence] = []
    score = 0.0

    permissive = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense"}
    if metrics.license_spdx in permissive:
        score += 100.0 * _COMMUNITY_WEIGHTS["license_quality"]
        evidence.append(
            Evidence(
                statement=f"宽松许可证（{metrics.license_spdx}），二次分发友好",
                level=EvidenceLevel.VERIFIED,
                source="GitHub API: repos.license.spdx_id",
            )
        )
    elif metrics.license_spdx:
        score += 50.0 * _COMMUNITY_WEIGHTS["license_quality"]
        evidence.append(
            Evidence(
                statement=f"非宽松许可证（{metrics.license_spdx}），需评估使用场景",
                level=EvidenceLevel.VERIFIED,
                source="GitHub API: repos.license.spdx_id",
            )
        )
    else:
        evidence.append(
            Evidence(
                statement="许可证不明，社区/法律维度不给分",
                level=EvidenceLevel.UNCHECKED,
                source="GitHub API: repos.license",
            )
        )

    # Star 仅作辅助：即便满分，本项也只占社区维度的 40%
    if metrics.stars >= _STRONG_STARS:
        star_score = 100.0
    elif metrics.stars >= _OK_STARS:
        star_score = 60.0
    else:
        star_score = 20.0
    score += star_score * _COMMUNITY_WEIGHTS["stars"]
    evidence.append(
        Evidence(
            statement=f"Star 数 {metrics.stars}（**仅作辅助信号**，权重受限）",
            level=EvidenceLevel.VERIFIED,
            source="GitHub API: repos.stargazers_count",
        )
    )
    return round(score, 2), evidence


def score_metrics(
    metrics: RepoMetrics,
    *,
    security_report: SecurityReport | None = None,
    now: datetime | None = None,
) -> ScoreBreakdown:
    """根据仓库指标与安全报告产出可解释评分。"""
    now = now or datetime.now(timezone.utc)

    maintenance, m_ev = _maintenance(metrics, now)
    quality, q_ev = _quality(metrics)
    security, s_ev, vetoed = _security(security_report)
    compatibility, c_ev = _compatibility(metrics)
    community, k_ev = _community(metrics)

    return ScoreBreakdown(
        maintenance=maintenance,
        quality=quality,
        security=security,
        compatibility=compatibility,
        community=community,
        vetoed=vetoed,
        reasons=[*m_ev, *q_ev, *s_ev, *c_ev, *k_ev],
    )