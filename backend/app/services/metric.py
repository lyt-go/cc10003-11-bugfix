"""指标口径业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

import re
from typing import Any

from app.services import conclusion as conclusion_mod
from app.store import store

MODULE = "metric"
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "口径评审中", "已发布", "已下线"]
ACTION_RULES = {"提交口径评审": "口径评审中", "发布指标": "已发布", "下线指标": "已下线", "指标换版": "已发布"}
NEGATIVE_ACTIONS = ["下线指标"]


def bump_version(version: Any) -> str:
    """口径版本自增：v2 → v3；没有数字后缀时补 -v2。"""
    text = str(version or "").strip() or "v1"
    match = re.fullmatch(r"(.*?)(\d+)", text)
    if match:
        return f"{match.group(1)}{int(match.group(2)) + 1}"
    return f"{text}-v2"


class MetricService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("指标编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标口径可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "指标换版":
            return self._reversion(entry)
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标定义已{action}"

    def _reversion(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """指标换版：口径版本自增，基于旧版本的有效结论同步失效。

        指标与显著性结论同处一个事务：版本没换成，结论也不能半废。
        """
        if entry.get("status") != "已发布":
            return None, f"当前状态「{entry.get('status')}」不允许指标换版，仅已发布指标可换版"
        with store.transaction(MODULE, conclusion_mod.MODULE_CONCLUSION):
            entry["口径版本"] = bump_version(entry.get("口径版本"))
            invalidated = conclusion_mod.invalidate_for_metric(
                str(entry.get("指标编号") or ""), str(entry.get("口径版本") or "")
            )
        if invalidated:
            plans = "、".join(str(row.get("分析方案编号")) for row in invalidated)
            return entry, f"指标已换版为 {entry['口径版本']}，{plans} 的旧结论已失效"
        return entry, f"指标已换版为 {entry['口径版本']}"
