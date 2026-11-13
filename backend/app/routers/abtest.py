"""实验设计接口：维护实验方案，覆盖提交方案评审、启动实验、结项实验、取消实验等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.abtest import AbtestService

router = APIRouter(prefix="/api/abtest", tags=["实验设计"])

service = AbtestService()

LIST_FIELDS = ["方案编号", "实验名称", "对照组定义", "实验组定义", "分流比例", "核心指标", "最小样本量", "当前样本量", "预计周期", "绑定分析方案", "结论版本"]
STATUSES = ["草稿", "方案评审中", "实验中", "已结项", "已取消"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按方案编号检索"),
    status: str | None = Query(default=None, description="草稿、方案评审中、实验中、已结项、已取消"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按方案编号与状态过滤实验设计列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出实验设计清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "abtest", "total": total, "items": items}


@router.get("/closures")
def list_closures() -> dict[str, Any]:
    """结项记录清单：只增不改，历史结项可追溯到当时冻结的阈值与版本。"""
    items = service.list_closures()
    return {"module": "abtest_closure", "total": len(items), "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条实验方案明细：含结项门禁预检与结项记录，与列表是同一份数据。"""
    detail = service.get_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"实验方案 {entry_id} 不存在或已归档")
    return detail


@router.get("/{entry_id}/closures")
def list_entry_closures(entry_id: int) -> dict[str, Any]:
    """单条实验的结项记录；不存在时给出可读的错误说明。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"实验方案 {entry_id} 不存在或已归档")
    items = service.list_closures(entry_id)
    return {"module": "abtest_closure", "total": len(items), "items": items}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条实验方案，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="实验方案已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条实验方案执行动作；门禁不通过时保持「实验中」并逐项说明缺口。"""
    action = str(payload.values.get("action") or "").strip()
    outcome = service.run_action(entry_id, action, payload.values)
    return ActionResult(ok=outcome.ok, message=outcome.message, entry=outcome.entry)
