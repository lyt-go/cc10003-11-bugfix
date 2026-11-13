"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
跨模块写入（实验结项 ↔ 分析结论）通过 transaction 保证同进同退。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self.reset()

    def reset(self) -> None:
        """把全量表恢复到种子数据；测试与重启用。"""
        with self._lock:
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if str(row.get(field, "")) == str(value):
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def append_row(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        """追加一条记录；分配自增 id。单独成方法，方便事务与失败注入。"""
        with self._lock:
            row.setdefault("id", self.next_id(module))
            self.rows(module).append(row)
            return row

    @contextmanager
    def transaction(self, *modules: str) -> Iterator[None]:
        """多表写入的原子边界：任一表写失败，所有涉及的表一起回滚。

        实验结项要同时动实验状态、分析结论与结项记录，任何一步失败都不能
        留下半套显著性结果，所以这里按表做整体快照、异常时整体还原。
        """
        with self._lock:
            snapshot = {
                module: copy.deepcopy(self._tables.get(module, [])) for module in modules
            }
            try:
                yield
            except Exception:
                self._tables.update(snapshot)
                raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
