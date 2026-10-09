"""候选对比（阶段 3）。

把多个 ``Plugin`` 压成可比较的 ``CompareRow`` 列表，并给出**有依据的**差异说明。
纪律：不排序出"最佳"并宣称安全；只呈现可核对的字段差异与证据缺口。
"""

from __future__ import annotations

from app.models import CompareRow, Plugin


def _row(plugin: Plugin) -> CompareRow:
    return CompareRow(
        plugin_id=plugin.id,
        name=plugin.name,
        repository=plugin.repository,
        kind=plugin.kind,
        pinned_ref=plugin.pinned_ref,
        score_total=plugin.score.total(),
        score=plugin.score,
        risk_level=plugin.risk_level,
        review_status=plugin.review_status,
        license_spdx=plugin.license.spdx_id,
        license_source=plugin.license.source,
        stars=plugin.stars,
        fetched_at=plugin.fetched_at,
        missing_fields=list(plugin.missing_fields),
        notes=[],
    )


def compare_plugins(plugins: list[Plugin]) -> dict[str, object]:
    """生成对比结果。

    返回 ``{"rows": [...], "highlights": [...], "notes": [...]}``。
    ``highlights`` 是**陈述性**差异（例如"X 的许可证未知"），不是推荐结论。
    """
    if not plugins:
        return {"rows": [], "highlights": [], "notes": ["没有可对比的候选。"]}

    rows = [_row(p) for p in plugins]
    rows.sort(key=lambda r: r.score_total, reverse=True)

    highlights: list[str] = []
    notes: list[str] = []

    # 安全一票否决者单独点名
    vetoed = [r for r in rows if r.score.vetoed]
    if vetoed:
        highlights.append(
            "以下候选存在严重（critical）安全发现，评分已被一票否决："
            + "、".join(r.name for r in vetoed)
        )

    no_license = [r for r in rows if not r.license_spdx]
    if no_license:
        highlights.append(
            "以下候选未取得明确许可证，使用前需自行确认授权："
            + "、".join(r.name for r in no_license)
        )

    no_ref = [r for r in rows if not r.pinned_ref]
    if no_ref:
        highlights.append(
            "以下候选尚未锁定到固定 commit（安装前必须固定版本）："
            + "、".join(r.name for r in no_ref)
        )

    missing = [r for r in rows if r.missing_fields]
    if missing:
        highlights.append(
            "以下候选存在未获取到的字段（数据不完整，结论需谨慎）："
            + "、".join(f"{r.name}({len(r.missing_fields)} 项)" for r in missing)
        )

    notes.append("排序依据为可解释总分；总分不包含任何「绝对安全」含义。")
    notes.append("对比只呈现已核实字段，缺失字段不会被默认值填充。")
    return {"rows": [r.model_dump() for r in rows], "highlights": highlights, "notes": notes}