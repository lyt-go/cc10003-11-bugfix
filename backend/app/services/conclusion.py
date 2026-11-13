"""实验↔分析共享的结论领域规则：门禁、版本与冻结口径都收在这一处。

实验方案与分析方案必须共享核心指标与结论版本，因此显著性结论不挂在任何
一侧的私有字段上，而是落在独立的 conclusion 表里，两侧都从这里读写：
- 显著性阈值冻结为 0.05，复现配置哈希冻结为 ab_eq_11_v5，结项时写进结项记录；
- 指标换版后，基于旧口径版本的结论立即失效，旧记录保留可追溯、不参与门禁；
- 结项记录（abtest_closure）只增不改，历史结项永远能追溯到当时的阈值。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE_CONCLUSION = "conclusion"
MODULE_CLOSURE = "abtest_closure"

FROZEN_THRESHOLD = 0.05
CONFIG_HASH = "ab_eq_11_v5"

CONCLUSION_ACTIVE = "有效"
CONCLUSION_ADOPTED = "已采纳"
CONCLUSION_STALE = "已失效"
CONCLUSION_REPLACED = "已取代"

VERDICTS = ("显著", "不显著")


@dataclass
class GateResult:
    """结项门禁评估结果：ok=False 时 gaps 逐项列出缺口。"""

    ok: bool
    gaps: list[str] = field(default_factory=list)
    metric: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    conclusion: dict[str, Any] | None = None


def to_int(value: Any) -> int:
    """把「15000」「15,000」、15000 这类写法统一成整数；解析不了返回 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = re.sub(r"[^0-9]", "", str(value or ""))
    return int(text) if text else 0


def parse_split(text: Any) -> list[float]:
    """解析分流比例：「50/50」「50:50」「40%,60%」都接受，返回各组百分比。"""
    parts = [part for part in re.split(r"[/:,，、\-\s]+", str(text or "").strip()) if part]
    ratios: list[float] = []
    for part in parts:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)%?", part)
        if not match:
            return []
        ratios.append(float(match.group(1)))
    if len(ratios) < 2 or any(ratio <= 0 for ratio in ratios):
        return []
    if abs(sum(ratios) - 100.0) > 1e-6:
        return []
    return ratios


def find_metric(metric_code: Any) -> dict[str, Any] | None:
    code = str(metric_code or "").strip()
    return store.find_by("metric", "指标编号", code) if code else None


def find_analysis_plan(plan_code: Any) -> dict[str, Any] | None:
    code = str(plan_code or "").strip()
    return store.find_by("analysis", "方案编号", code) if code else None


def conclusions_for(plan_code: str) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(MODULE_CONCLUSION)
        if row.get("分析方案编号") == plan_code
    ]


def active_conclusion(plan_code: str) -> dict[str, Any] | None:
    """分析方案当前的显著性结论，全系统只有这一份参与门禁与展示。

    「有效」优先；已被结项采纳（已采纳）的结论仍是该方案的当前版本，
    复核入口与实验列表读到的都是它，不会出现两版或残留。
    """
    current = [
        row for row in conclusions_for(plan_code)
        if row.get("状态") in (CONCLUSION_ACTIVE, CONCLUSION_ADOPTED)
    ]
    for row in current:
        if row.get("状态") == CONCLUSION_ACTIVE:
            return row
    return current[0] if current else None


def next_conclusion_version(plan_code: str) -> str:
    return f"v{len(conclusions_for(plan_code)) + 1}"


def invalidate_for_metric(metric_code: str, current_version: str) -> list[dict[str, Any]]:
    """指标换版：基于旧口径版本的有效结论全部标记失效，返回失效清单。"""
    invalidated = []
    for row in store.rows(MODULE_CONCLUSION):
        if (
            row.get("核心指标") == metric_code
            and row.get("状态") == CONCLUSION_ACTIVE
            and row.get("指标版本") != current_version
        ):
            row["状态"] = CONCLUSION_STALE
            invalidated.append(row)
    return invalidated


def evaluate_close_gate(experiment: dict[str, Any]) -> GateResult:
    """结项门禁：核心指标、分流比例、分析结论、样本量逐项校验。

    不通过时实验保持「实验中」，gaps 逐项说明缺口（含样本差多少）。
    """
    gaps: list[str] = []
    result = GateResult(ok=False, gaps=gaps)

    metric_code = str(experiment.get("核心指标") or "").strip()
    if not metric_code:
        gaps.append("核心指标未绑定")
        metric = None
    else:
        metric = find_metric(metric_code)
        if metric is None:
            gaps.append(f"核心指标 {metric_code} 不存在")
        elif metric.get("status") != "已发布":
            gaps.append(f"核心指标 {metric_code} 未发布（当前：{metric.get('status')}）")
    result.metric = metric

    if not parse_split(experiment.get("分流比例")):
        gaps.append(f"分流比例不完整：{experiment.get('分流比例') or '未配置'}（各组之和须为 100）")

    plan_code = str(experiment.get("绑定分析方案") or "").strip()
    if not plan_code:
        gaps.append("未绑定分析方案")
        plan = None
    else:
        plan = find_analysis_plan(plan_code)
        if plan is None:
            gaps.append(f"分析方案 {plan_code} 不存在")
        elif plan.get("status") != "已启用":
            gaps.append(f"分析方案 {plan_code} 未启用（当前：{plan.get('status')}）")
    result.plan = plan

    conclusion = active_conclusion(plan_code) if plan else None
    if plan and metric is not None:
        current_version = str(metric.get("口径版本") or "")
        stale = invalidate_for_metric(metric_code, current_version)
        if stale:
            conclusion = active_conclusion(plan_code)
        elif conclusion is None:
            # 换版动作已经把这些结论置为失效，这里要认出换版而不是笼统报缺结论
            stale = [
                row for row in conclusions_for(plan_code)
                if row.get("核心指标") == metric_code
                and row.get("状态") == CONCLUSION_STALE
                and row.get("指标版本") != current_version
            ]
        if stale:
            versions = "、".join(str(row.get("结论版本")) for row in stale)
            gaps.append(
                f"核心指标已换版（当前口径 {metric.get('口径版本')}），"
                f"结论版本 {versions} 基于旧口径，已失效"
            )
    if plan and conclusion is None and not any("已换版" in gap for gap in gaps):
        gaps.append(f"分析方案 {plan_code} 尚无显著性结论")
    result.conclusion = conclusion

    required = to_int(experiment.get("最小样本量"))
    if required <= 0:
        gaps.append("最小样本量未配置")
    else:
        actual = to_int(experiment.get("当前样本量"))
        if conclusion is not None:
            actual = min(actual, to_int(conclusion.get("结论样本量")))
        if actual < required:
            gaps.append(f"样本量不足：当前 {actual} / 最小 {required}，缺少 {required - actual} 个样本")

    if conclusion is not None and float(conclusion.get("显著性阈值") or 0) != FROZEN_THRESHOLD:
        gaps.append(f"显著性阈值偏离冻结值 {FROZEN_THRESHOLD}")

    result.ok = not gaps
    return result


def build_closure(
    experiment: dict[str, Any],
    gate: GateResult,
    operator: str,
) -> dict[str, Any]:
    """生成结项记录：冻结当时的阈值、版本与样本量，只增不改。"""
    conclusion = gate.conclusion or {}
    return {
        "结项编号": f"CLOS-{experiment.get('方案编号')}",
        "方案编号": experiment.get("方案编号"),
        "实验名称": experiment.get("实验名称"),
        "分析方案编号": experiment.get("绑定分析方案"),
        "核心指标": experiment.get("核心指标"),
        "指标版本": (gate.metric or {}).get("口径版本"),
        "结论版本": conclusion.get("结论版本"),
        "显著性结论": conclusion.get("显著性结论"),
        "显著性阈值": FROZEN_THRESHOLD,
        "配置哈希": CONFIG_HASH,
        "最小样本量": to_int(experiment.get("最小样本量")),
        "结项样本量": to_int(experiment.get("当前样本量")),
        "分流比例": experiment.get("分流比例"),
        "操作人": operator,
        "结项时间": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "已生效",
        "pending": False,
        "abnormal": False,
    }
