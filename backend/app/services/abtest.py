"""实验设计业务规则：状态机、字段校验、结项门禁都收在这里。

状态机：草稿 → 方案评审中 → 实验中 → 已结项 / 已取消。
结项是唯一带门禁的流转：核心指标、分流比例、分析结论、最小样本量缺一不可；
门禁不通过时实验保持「实验中」并逐项说明缺口。已结项/已取消是终态，
任何动作都不能把记录重新打开覆盖。
"""
from __future__ import annotations

from typing import Any

from app.services import conclusion as conclusion_mod
from app.services.guard import Outcome, once_guard, require_operator
from app.store import store

MODULE = "abtest"
REQUIRED_FIELDS = ["方案编号", "实验名称", "对照组定义"]
PERSIST_FIELDS = [
    "方案编号", "实验名称", "对照组定义", "实验组定义",
    "分流比例", "核心指标", "最小样本量", "当前样本量", "预计周期", "绑定分析方案",
]
STATUS_ORDER = ["草稿", "方案评审中", "实验中", "已结项", "已取消"]
TERMINAL_STATUSES = {"已结项", "已取消"}
# 动作 → (允许的来源状态, 目标状态)；目标与来源相同表示原地更新。
TRANSITIONS: dict[str, tuple[set[str], str]] = {
    "提交方案评审": ({"草稿"}, "方案评审中"),
    "启动实验": ({"方案评审中"}, "实验中"),
    "结项实验": ({"实验中"}, "已结项"),
    "取消实验": ({"草稿", "方案评审中", "实验中"}, "已取消"),
    "同步实验样本量": ({"实验中"}, "实验中"),
}
NEGATIVE_ACTIONS = {"取消实验"}
# 终态/幂等动作走共用重复判断：并发与重复提交只放行第一次；
# 普通流转每次都必须过状态机，不能靠缓存重放。
IDEMPOTENT_ACTIONS = {"结项实验", "取消实验"}


class AbtestService:
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
        """实验详情：带上结项门禁预检与结项记录，复核入口与列表读同一份数据。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        gate = conclusion_mod.evaluate_close_gate(entry)
        return {
            "entry": entry,
            "gate": {"ok": gate.ok, "gaps": gate.gaps},
            "closures": self.list_closures(entry_id),
        }

    def list_closures(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        """结项记录只增不改：历史结项永远能追溯到当时冻结的阈值与版本。"""
        rows = store.rows(conclusion_mod.MODULE_CLOSURE)
        if entry_id is not None:
            entry = self.get_entry(entry_id)
            if entry is None:
                return []
            rows = [row for row in rows if row.get("方案编号") == entry.get("方案编号")]
        return list(rows)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = {"id": store.next_id(MODULE)}
        # 全量保留登记字段：分流比例、最小样本量等不能再在详情里丢失。
        entry.update({field: values.get(field) for field in PERSIST_FIELDS if field in values})
        entry["结论版本"] = str(values.get("结论版本") or "")
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
        # 重复判断走共用能力：并发结项、重复点击只放行第一次，结果全系统一致。
        return once_guard.run_once(
            MODULE, f"{entry_id}:{action}", lambda: self._apply(entry_id, action, values, operator)
        )

    def _apply(self, entry_id: int, action: str, values: dict[str, Any], operator: str) -> Outcome:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return Outcome(False, f"实验方案 {entry_id} 不存在或已归档")
        if action not in TRANSITIONS:
            return Outcome(False, f"动作「{action}」不属于实验设计可执行范围")
        if entry.get("status") in TERMINAL_STATUSES:
            return Outcome(False, f"实验方案处于「{entry.get('status')}」终态，记录不允许重新打开或覆盖")
        sources, target = TRANSITIONS[action]
        if target not in STATUS_ORDER:
            return Outcome(False, f"目标状态「{target}」不在允许的状态序列里")
        if entry.get("status") not in sources:
            return Outcome(
                False,
                f"当前状态「{entry.get('status')}」不允许执行「{action}」，"
                f"实验只能按 {' → '.join(STATUS_ORDER)} 推进",
            )
        try:
            if action == "结项实验":
                return self._close(entry, operator)
            if action == "同步实验样本量":
                return self._sync_samples(entry, values)
            entry["status"] = target
            entry["pending"] = target not in TERMINAL_STATUSES
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return Outcome(True, f"实验方案已{action}", entry)
        except Exception as exc:  # 事务已回滚，这里只把失败说清
            return Outcome(False, f"动作「{action}」写入失败，已整体回滚：{exc}")

    def _sync_samples(self, entry: dict[str, Any], values: dict[str, Any]) -> Outcome:
        samples = conclusion_mod.to_int(values.get("当前样本量"))
        if samples <= 0:
            return Outcome(False, "同步实验样本量需要携带正整数「当前样本量」")
        entry["当前样本量"] = samples
        return Outcome(True, f"实验样本量已同步为 {samples}", entry)

    def _close(self, entry: dict[str, Any], operator: str) -> Outcome:
        gate = conclusion_mod.evaluate_close_gate(entry)
        if not gate.ok:
            return Outcome(
                False,
                "结项门禁未通过，实验保持「实验中」：" + "；".join(gate.gaps),
                entry,
                {"gaps": gate.gaps},
            )
        # 实验状态、分析结论、结项记录同处一个事务：任一侧失败同时退回。
        with store.transaction(MODULE, "analysis", conclusion_mod.MODULE_CONCLUSION, conclusion_mod.MODULE_CLOSURE):
            closure = conclusion_mod.build_closure(entry, gate, operator)
            store.append_row(conclusion_mod.MODULE_CLOSURE, closure)
            if gate.conclusion is not None:
                gate.conclusion["状态"] = conclusion_mod.CONCLUSION_ADOPTED
            entry["结论版本"] = str((gate.conclusion or {}).get("结论版本") or entry.get("结论版本") or "")
            entry["status"] = "已结项"
            entry["pending"] = False
            entry["abnormal"] = False
        return Outcome(
            True,
            f"实验方案已结项，结项记录 {closure['结项编号']}（阈值冻结 {conclusion_mod.FROZEN_THRESHOLD}）",
            entry,
            {"closure": closure},
        )
