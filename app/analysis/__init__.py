"""分析子包（阶段 3）：评分、安全审查、候选对比。"""

from __future__ import annotations

from app.analysis.compare import compare_plugins
from app.analysis.scoring import score_metrics
from app.analysis.security_review import ScannedFile, review_files

__all__ = ["score_metrics", "review_files", "ScannedFile", "compare_plugins"]