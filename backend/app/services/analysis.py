"""分析方案业务规则：状态机、字段校验与显著性结论的版本管理。

状态机：草稿 → 方案评审中 → 已启用 → 已归档。
分析方案与实验方案共享核心指标与结论版本：结论落在共享的 conclusion 表，
启用方案重复执行只生效一次，生成结论时旧版本标记「已取代」而不是残留。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services import conclusion as conclusion_mod
from app.services.guard import Outcome, once_guard, require_operator
from app.store import store

MODULE = "analysis"
REQUIRED_FIELDS = ["方案编号", "分析主题", "数据集范围"]
PERSIST_FIELDS = [
    "方案编号", "分析主题", "数据集范围", "分析方法",
    "对比基准", "显著性阈值", "输出图表类型", "调度周期", "核心指标",
]
STATUS_ORDER = ["草稿", "方案评审中", "已启用", "已归档"]
TERMINAL_STATUSES = {"已归档"}
TRANSITIONS: dict[str, tuple[set[str], str]] = {
    "提交方案评审": ({"草稿"}, "方案评审中"),
    "启用方案": ({"方案评审中"}, "已启用"),
    "归档方案": ({"已启用"}, "已归档"),
    "生成显著性结论": ({"已启用"}, "已启用"),
}
NEGATIVE_ACTIONS: set[str] = set()
# 与实验侧同一份重复判断能力：重复启用、重复归档、重复提交同一结论只放行第一次。
IDEMPOTENT_ACTIONS = {"启用方案", "归档方案", "生成显著性结论"}


class AnalysisService:
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
            rows = [row for row in rows if keyword in str(row.get("方案编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_detail(self, entry_id: int) -> dict[str, Any] | None:
        """分析方案详情：结论版本与实验列表读同一份共享结论，不再各看一版。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        plan_code = str(entry.get("方案编号") or "")
        experiments = [
            row for row in store.rows("abtest")
            if str(row.get("绑定分析方案") or "") == plan_code
        ]
        return {
            "entry": entry,
            "conclusion": conclusion_mod.active_conclusion(plan_code),
            "conclusions": conclusion_mod.conclusions_for(plan_code),
            "experiments": experiments,
        }

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in PERSIST_FIELDS if field in values})
        entry["结论版本"] = str(values.get("结论版本") or "")
        entry["显著性结论"] = str(values.get("显著性结论") or "暂无")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        store.append_row(MODULE, entry)
        return entry, []

    def run_action(self, entry_id: int, action: str, values: dict[str, Any]) -> Outcome:
        operator = require_operator(values)
        if operator is None:
            return Outcome(False, "缺少操作人身份，动作被拒绝")
        if action not in IDEMPOTENT_ACTIONS:
            return self._apply(entry_id, action, values, operator)
        dedup_key = f"{entry_id}:{action}"
        if action == "生成显著性结论":
            dedup_key += f":{values.get('显著性结论')}:{values.get('结论样本量')}"
        return once_guard.run_once(
            MODULE, dedup_key, lambda: self._apply(entry_id, action, values, operator)
        )

    def _apply(self, entry_id: int, action: str, values: dict[str, Any], operator: str) -> Outcome:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return Outcome(False, f"分析方案 {entry_id} 不存在或已归档")
        if action not in TRANSITIONS:
            return Outcome(False, f"动作「{action}」不属于分析方案可执行范围")
        if entry.get("status") in TERMINAL_STATUSES:
            return Outcome(False, f"分析方案处于「{entry.get('status')}」终态，记录不允许重新打开或覆盖")
        sources, target = TRANSITIONS[action]
        if target not in STATUS_ORDER:
            return Outcome(False, f"目标状态「{target}」不在允许的状态序列里")
        if entry.get("status") not in sources:
            return Outcome(
                False,
                f"当前状态「{entry.get('status')}」不允许执行「{action}」，"
                f"分析方案只能按 {' → '.join(STATUS_ORDER)} 推进",
            )
        try:
            if action == "生成显著性结论":
                return self._generate_conclusion(entry, values, operator)
            entry["status"] = target
            entry["pending"] = target in {"草稿", "方案评审中"}
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return Outcome(True, f"分析方案已{action}", entry)
        except Exception as exc:  # 事务已回滚，这里只把失败说清
            return Outcome(False, f"动作「{action}」写入失败，已整体回滚：{exc}")

    def _generate_conclusion(
        self, entry: dict[str, Any], values: dict[str, Any], operator: str
    ) -> Outcome:
        metric_code = str(entry.get("核心指标") or "").strip()
        if not metric_code:
            return Outcome(False, "核心指标未绑定，无法生成显著性结论")
        metric = conclusion_mod.find_metric(metric_code)
        if metric is None:
            return Outcome(False, f"核心指标 {metric_code} 不存在")
        if metric.get("status") != "已发布":
            return Outcome(False, f"核心指标 {metric_code} 未发布（当前：{metric.get('status')}）")
        verdict = str(values.get("显著性结论") or "").strip()
        if verdict not in conclusion_mod.VERDICTS:
            return Outcome(False, "请提供显著性结论（显著 / 不显著）")
        plan_code = str(entry.get("方案编号") or "")
        samples = conclusion_mod.to_int(values.get("结论样本量"))
        if samples <= 0:
            bound = [
                row for row in store.rows("abtest")
                if str(row.get("绑定分析方案") or "") == plan_code
            ]
            samples = max((conclusion_mod.to_int(row.get("当前样本量")) for row in bound), default=0)
        if samples <= 0:
            return Outcome(False, "结论样本量缺失，请携带「结论样本量」或先同步实验样本量")

        # 结论、方案版本、关联实验的版本指针同处一个事务，任一侧失败同时退回。
        with store.transaction(MODULE, "abtest", conclusion_mod.MODULE_CONCLUSION):
            version = conclusion_mod.next_conclusion_version(plan_code)
            for old in conclusion_mod.conclusions_for(plan_code):
                if old.get("状态") == conclusion_mod.CONCLUSION_ACTIVE:
                    old["状态"] = conclusion_mod.CONCLUSION_REPLACED
            record = {
                "结论编号": f"CONC-{plan_code}-{version}",
                "分析方案编号": plan_code,
                "核心指标": metric_code,
                "指标版本": metric.get("口径版本"),
                "结论版本": version,
                "显著性结论": verdict,
                "显著性阈值": conclusion_mod.FROZEN_THRESHOLD,
                "结论样本量": samples,
                "配置哈希": conclusion_mod.CONFIG_HASH,
                "操作人": operator,
                "生成时间": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "状态": conclusion_mod.CONCLUSION_ACTIVE,
                "pending": False,
                "abnormal": False,
            }
            store.append_row(conclusion_mod.MODULE_CONCLUSION, record)
            entry["结论版本"] = version
            entry["显著性结论"] = verdict
            entry["显著性阈值"] = str(conclusion_mod.FROZEN_THRESHOLD)
            for row in store.rows("abtest"):
                if str(row.get("绑定分析方案") or "") == plan_code and row.get("status") == "实验中":
                    row["结论版本"] = version
        return Outcome(
            True,
            f"已生成显著性结论（版本 {version}，阈值冻结 {conclusion_mod.FROZEN_THRESHOLD}，配置 {conclusion_mod.CONFIG_HASH}）",
            entry,
            {"conclusion": record},
        )
