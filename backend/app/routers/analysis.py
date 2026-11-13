"""分析方案接口：维护分析方案，覆盖提交方案评审、启用方案、生成显著性结论、归档方案等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.analysis import AnalysisService

router = APIRouter(prefix="/api/analysis", tags=["分析方案"])

service = AnalysisService()

LIST_FIELDS = ["方案编号", "分析主题", "数据集范围", "分析方法", "对比基准", "显著性阈值", "输出图表类型", "调度周期", "核心指标", "结论版本", "显著性结论"]
STATUSES = ["草稿", "方案评审中", "已启用", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按方案编号检索"),
    status: str | None = Query(default=None, description="草稿、方案评审中、已启用、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按方案编号与状态过滤分析方案列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分析方案清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "analysis", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条分析方案明细：含共享结论与关联实验，和实验列表是同一份版本。"""
    detail = service.get_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"分析方案 {entry_id} 不存在或已归档")
    return detail


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条分析方案，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="分析方案已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条分析方案执行动作；重复启用、重复生成结论只生效一次。"""
    action = str(payload.values.get("action") or "").strip()
    outcome = service.run_action(entry_id, action, payload.values)
    return ActionResult(ok=outcome.ok, message=outcome.message, entry=outcome.entry)
