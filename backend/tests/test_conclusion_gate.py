"""结项门禁验收：指标换版、样本不足、重复结项、回滚与历史追溯。

运行方式：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from app.services import conclusion as conclusion_mod
from app.services.abtest import AbtestService
from app.services.analysis import AnalysisService
from app.services.guard import reset_once_guard
from app.services.metric import MetricService
from app.store import store

OPERATOR = {"operator": "测试员"}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        reset_once_guard()
        self.abtest = AbtestService()
        self.analysis = AnalysisService()
        self.metric = MetricService()

    def exp(self, code: str) -> dict:
        row = store.find_by("abtest", "方案编号", code)
        assert row is not None, f"实验 {code} 不存在"
        return row

    def plan(self, code: str) -> dict:
        row = store.find_by("analysis", "方案编号", code)
        assert row is not None, f"分析方案 {code} 不存在"
        return row

    def metric_row(self, code: str) -> dict:
        row = store.find_by("metric", "指标编号", code)
        assert row is not None, f"指标 {code} 不存在"
        return row

    def closures(self, code: str) -> list[dict]:
        return [row for row in store.rows("abtest_closure") if row.get("方案编号") == code]

    def close(self, code: str):
        entry = self.exp(code)
        return self.abtest.run_action(entry["id"], "结项实验", dict(OPERATOR))

    def walk_to_running(self, values: dict) -> dict:
        entry, missing = self.abtest.create_entry(values)
        assert not missing, f"登记缺字段：{missing}"
        assert entry is not None
        for action in ("提交方案评审", "启动实验"):
            outcome = self.abtest.run_action(entry["id"], action, dict(OPERATOR))
            assert outcome.ok, f"{action} 失败：{outcome.message}"
        return entry


class TestStateMachine(Base):
    def test_experiment_advances_only_in_order(self):
        entry, missing = self.abtest.create_entry(
            {"方案编号": "ABTE-T1", "实验名称": "顺序校验", "对照组定义": "A"}
        )
        self.assertFalse(missing)
        assert entry is not None
        # 草稿不能直接启动、更不能直接结项
        self.assertFalse(self.abtest.run_action(entry["id"], "启动实验", dict(OPERATOR)).ok)
        self.assertFalse(self.abtest.run_action(entry["id"], "结项实验", dict(OPERATOR)).ok)
        self.assertEqual(entry["status"], "草稿")
        # 非法动作
        self.assertFalse(self.abtest.run_action(entry["id"], "删除实验", dict(OPERATOR)).ok)
        # 正序推进
        self.assertTrue(self.abtest.run_action(entry["id"], "提交方案评审", dict(OPERATOR)).ok)
        self.assertEqual(entry["status"], "方案评审中")
        self.assertTrue(self.abtest.run_action(entry["id"], "启动实验", dict(OPERATOR)).ok)
        self.assertEqual(entry["status"], "实验中")
        # 取消后进入终态
        self.assertTrue(self.abtest.run_action(entry["id"], "取消实验", dict(OPERATOR)).ok)
        self.assertEqual(entry["status"], "已取消")
        self.assertFalse(entry["pending"])
        self.assertFalse(self.abtest.run_action(entry["id"], "提交方案评审", dict(OPERATOR)).ok)

    def test_closed_record_cannot_be_reopened(self):
        outcome = self.close("ABTE-SEARCH")
        self.assertTrue(outcome.ok, outcome.message)
        entry = self.exp("ABTE-SEARCH")
        self.assertEqual(entry["status"], "已结项")
        for action in ("提交方案评审", "启动实验", "取消实验"):
            rejected = self.abtest.run_action(entry["id"], action, dict(OPERATOR))
            self.assertFalse(rejected.ok, f"{action} 不应放行")
            self.assertIn("终态", rejected.message)
        self.assertEqual(entry["status"], "已结项")
        # 种子里历史结项的 ABTE-PAY 同样不允许重开
        pay = self.exp("ABTE-PAY")
        self.assertFalse(self.abtest.run_action(pay["id"], "启动实验", dict(OPERATOR)).ok)

    def test_operator_is_required(self):
        entry = self.exp("ABTE-CHECKOUT")
        outcome = self.abtest.run_action(entry["id"], "结项实验", {})
        self.assertFalse(outcome.ok)
        self.assertIn("操作人", outcome.message)


class TestCloseGate(Base):
    def test_anomaly_sample_rejected_with_exact_sample_gap(self):
        """异常样例：15000 最小样本量 vs 12000 当前样本，且 ANAL-CONVERSION 无结论。"""
        outcome = self.close("ABTE-CHECKOUT")
        self.assertFalse(outcome.ok)
        self.assertIn("缺少 3000 个样本", outcome.message)
        self.assertIn("尚无显著性结论", outcome.message)
        entry = self.exp("ABTE-CHECKOUT")
        self.assertEqual(entry["status"], "实验中")
        self.assertEqual(self.closures("ABTE-CHECKOUT"), [])

    def test_close_requires_core_metric(self):
        entry = self.walk_to_running(
            {
                "方案编号": "ABTE-T2",
                "实验名称": "缺指标",
                "对照组定义": "A",
                "分流比例": "50/50",
                "最小样本量": 100,
                "当前样本量": 200,
                "绑定分析方案": "ANAL-SEARCH",
            }
        )
        outcome = self.abtest.run_action(entry["id"], "结项实验", dict(OPERATOR))
        self.assertFalse(outcome.ok)
        self.assertIn("核心指标未绑定", outcome.message)
        self.assertEqual(entry["status"], "实验中")

    def test_close_requires_complete_split(self):
        entry = self.walk_to_running(
            {
                "方案编号": "ABTE-T3",
                "实验名称": "分流不完整",
                "对照组定义": "A",
                "分流比例": "30/30",
                "核心指标": "METR-CONVERSION",
                "最小样本量": 100,
                "当前样本量": 200,
                "绑定分析方案": "ANAL-SEARCH",
            }
        )
        outcome = self.abtest.run_action(entry["id"], "结项实验", dict(OPERATOR))
        self.assertFalse(outcome.ok)
        self.assertIn("分流比例不完整", outcome.message)

    def test_metric_reversion_invalidates_conclusion(self):
        """指标换版：旧口径结论失效，结项被门禁拦下。"""
        metric = self.metric_row("METR-CONVERSION")
        entry, message = self.metric.run_action(metric["id"], "指标换版")
        self.assertIsNotNone(entry, message)
        self.assertEqual(metric["口径版本"], "v3")
        stale = store.find_by("conclusion", "结论编号", "CONC-ANAL-SEARCH-v1")
        assert stale is not None
        self.assertEqual(stale["状态"], "已失效")
        outcome = self.close("ABTE-SEARCH")
        self.assertFalse(outcome.ok)
        self.assertIn("已换版", outcome.message)
        self.assertEqual(self.exp("ABTE-SEARCH")["status"], "实验中")

    def test_close_success_freezes_threshold_and_hash(self):
        outcome = self.close("ABTE-SEARCH")
        self.assertTrue(outcome.ok, outcome.message)
        entry = self.exp("ABTE-SEARCH")
        self.assertEqual(entry["status"], "已结项")
        self.assertFalse(entry["pending"])
        closures = self.closures("ABTE-SEARCH")
        self.assertEqual(len(closures), 1)
        closure = closures[0]
        self.assertEqual(closure["结项编号"], "CLOS-ABTE-SEARCH")
        self.assertEqual(closure["显著性阈值"], 0.05)
        self.assertEqual(closure["配置哈希"], "ab_eq_11_v5")
        self.assertEqual(closure["结论版本"], "v1")
        self.assertEqual(closure["指标版本"], "v2")
        self.assertEqual(closure["结项样本量"], 20000)
        conclusion = store.find_by("conclusion", "结论编号", "CONC-ANAL-SEARCH-v1")
        assert conclusion is not None
        self.assertEqual(conclusion["状态"], "已采纳")

    def test_full_flow_generate_conclusion_then_close(self):
        """ANAL-CONVERSION 生成结论 → 版本共享到实验 → 样本补齐 → 结项放行。"""
        plan = self.plan("ANAL-CONVERSION")
        outcome = self.analysis.run_action(
            plan["id"], "生成显著性结论", {"显著性结论": "显著", "结论样本量": 15000, **OPERATOR}
        )
        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual(plan["结论版本"], "v1")
        # 实验与分析共享同一份核心指标与结论版本
        self.assertEqual(self.exp("ABTE-CHECKOUT")["结论版本"], "v1")
        record = store.find_by("conclusion", "结论编号", "CONC-ANAL-CONVERSION-v1")
        assert record is not None
        self.assertEqual(record["指标版本"], "v2")
        self.assertEqual(record["显著性阈值"], 0.05)
        self.assertEqual(record["配置哈希"], "ab_eq_11_v5")
        # 实验侧样本仍不足：保持实验中并指出缺口
        rejected = self.close("ABTE-CHECKOUT")
        self.assertFalse(rejected.ok)
        self.assertIn("缺少 3000 个样本", rejected.message)
        # 补齐样本后放行
        entry = self.exp("ABTE-CHECKOUT")
        synced = self.abtest.run_action(entry["id"], "同步实验样本量", {"当前样本量": 15000, **OPERATOR})
        self.assertTrue(synced.ok, synced.message)
        closed = self.close("ABTE-CHECKOUT")
        self.assertTrue(closed.ok, closed.message)
        self.assertEqual(entry["status"], "已结项")
        self.assertEqual(self.closures("ABTE-CHECKOUT")[0]["结项样本量"], 15000)


class TestIdempotency(Base):
    def test_duplicate_close_yields_single_closure(self):
        first = self.close("ABTE-SEARCH")
        second = self.close("ABTE-SEARCH")
        self.assertTrue(first.ok and second.ok)
        self.assertEqual(len(self.closures("ABTE-SEARCH")), 1)
        self.assertEqual(
            first.extra["closure"]["结项编号"], second.extra["closure"]["结项编号"]
        )

    def test_concurrent_close_yields_single_closure(self):
        results = []

        def worker() -> None:
            results.append(self.close("ABTE-SEARCH"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(all(outcome.ok for outcome in results))
        self.assertEqual(len(self.closures("ABTE-SEARCH")), 1)
        self.assertEqual(self.exp("ABTE-SEARCH")["status"], "已结项")

    def test_repeated_enable_keeps_single_version(self):
        entry, missing = self.analysis.create_entry(
            {"方案编号": "ANAL-T1", "分析主题": "幂等启用", "数据集范围": "全量"}
        )
        self.assertFalse(missing)
        assert entry is not None
        self.assertTrue(self.analysis.run_action(entry["id"], "提交方案评审", dict(OPERATOR)).ok)
        first = self.analysis.run_action(entry["id"], "启用方案", dict(OPERATOR))
        second = self.analysis.run_action(entry["id"], "启用方案", dict(OPERATOR))
        self.assertTrue(first.ok and second.ok)
        self.assertEqual(entry["status"], "已启用")
        # 并发启用同样只生效一次
        store.reset()
        reset_once_guard()
        entry, missing = self.analysis.create_entry(
            {"方案编号": "ANAL-T2", "分析主题": "并发启用", "数据集范围": "全量"}
        )
        self.assertFalse(missing)
        assert entry is not None
        self.assertTrue(self.analysis.run_action(entry["id"], "提交方案评审", dict(OPERATOR)).ok)
        results = []

        def worker() -> None:
            results.append(self.analysis.run_action(entry["id"], "启用方案", dict(OPERATOR)))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(all(outcome.ok for outcome in results))
        self.assertEqual(entry["status"], "已启用")

    def test_identical_conclusion_payload_generates_once(self):
        plan = self.plan("ANAL-SEARCH")
        payload = {"显著性结论": "显著", "结论样本量": 21000, **OPERATOR}
        first = self.analysis.run_action(plan["id"], "生成显著性结论", dict(payload))
        second = self.analysis.run_action(plan["id"], "生成显著性结论", dict(payload))
        self.assertTrue(first.ok and second.ok)
        versions = conclusion_mod.conclusions_for("ANAL-SEARCH")
        self.assertEqual(len(versions), 2)  # v1（种子）+ v2（新生成），重复提交没有产生 v3
        self.assertEqual(plan["结论版本"], "v2")


class TestTransactionRollback(Base):
    def test_rollback_when_closure_write_fails(self):
        """结项记录写入失败：实验状态与分析结论一起退回，不留半套显著性结果。"""
        original = store.append_row

        def broken_append(module: str, row: dict) -> dict:
            if module == "abtest_closure":
                raise RuntimeError("模拟结项记录写入失败")
            return original(module, row)

        with mock.patch.object(store, "append_row", side_effect=broken_append):
            outcome = self.close("ABTE-SEARCH")
        self.assertFalse(outcome.ok)
        self.assertIn("回滚", outcome.message)
        entry = self.exp("ABTE-SEARCH")
        self.assertEqual(entry["status"], "实验中")
        self.assertEqual(self.closures("ABTE-SEARCH"), [])
        conclusion = store.find_by("conclusion", "结论编号", "CONC-ANAL-SEARCH-v1")
        assert conclusion is not None
        self.assertEqual(conclusion["状态"], "有效")
        # 失败结果不缓存：修复后可以重新结项
        retry = self.close("ABTE-SEARCH")
        self.assertTrue(retry.ok, retry.message)
        self.assertEqual(len(self.closures("ABTE-SEARCH")), 1)

    def test_rollback_when_conclusion_write_fails(self):
        plan = self.plan("ANAL-CONVERSION")
        original = store.append_row

        def broken_append(module: str, row: dict) -> dict:
            if module == "conclusion":
                raise RuntimeError("模拟结论写入失败")
            return original(module, row)

        with mock.patch.object(store, "append_row", side_effect=broken_append):
            outcome = self.analysis.run_action(
                plan["id"], "生成显著性结论", {"显著性结论": "显著", "结论样本量": 15000, **OPERATOR}
            )
        self.assertFalse(outcome.ok)
        self.assertEqual(plan["结论版本"], "")
        self.assertEqual(conclusion_mod.conclusions_for("ANAL-CONVERSION"), [])
        self.assertEqual(self.exp("ABTE-CHECKOUT")["结论版本"], "")


class TestSharedVersionAndTraceability(Base):
    def test_old_conclusion_replaced_not_residual(self):
        plan = self.plan("ANAL-SEARCH")
        outcome = self.analysis.run_action(
            plan["id"], "生成显著性结论", {"显著性结论": "不显著", "结论样本量": 21000, **OPERATOR}
        )
        self.assertTrue(outcome.ok, outcome.message)
        old = store.find_by("conclusion", "结论编号", "CONC-ANAL-SEARCH-v1")
        assert old is not None
        self.assertEqual(old["状态"], "已取代")
        active = conclusion_mod.active_conclusion("ANAL-SEARCH")
        assert active is not None
        self.assertEqual(active["结论版本"], "v2")
        # 实验详情与分析详情读到同一份共享版本
        exp_detail = self.abtest.get_detail(self.exp("ABTE-SEARCH")["id"])
        plan_detail = self.analysis.get_detail(plan["id"])
        assert exp_detail is not None and plan_detail is not None
        self.assertEqual(exp_detail["entry"]["结论版本"], "v2")
        self.assertEqual(plan_detail["conclusion"]["结论版本"], "v2")

    def test_create_preserves_split_and_sample_fields(self):
        entry, missing = self.abtest.create_entry(
            {
                "方案编号": "ABTE-T4",
                "实验名称": "字段保留",
                "对照组定义": "A",
                "分流比例": "50/50",
                "核心指标": "METR-CONVERSION",
                "最小样本量": 15000,
                "绑定分析方案": "ANAL-CONVERSION",
            }
        )
        self.assertFalse(missing)
        assert entry is not None
        fetched = self.abtest.get_entry(entry["id"])
        assert fetched is not None
        self.assertEqual(fetched["分流比例"], "50/50")
        self.assertEqual(fetched["最小样本量"], 15000)
        self.assertEqual(fetched["绑定分析方案"], "ANAL-CONVERSION")

    def test_historical_closure_stays_traceable(self):
        """历史结项记录只增不改：换版、新结论之后仍能追溯当时阈值。"""
        closures = self.abtest.list_closures(self.exp("ABTE-PAY")["id"])
        self.assertEqual(len(closures), 1)
        closure = closures[0]
        self.assertEqual(closure["显著性阈值"], 0.05)
        self.assertEqual(closure["配置哈希"], "ab_eq_11_v5")
        self.assertEqual(closure["指标版本"], "v2")
        # 指标换版 + 新结论都不影响历史结项记录
        metric = self.metric_row("METR-CONVERSION")
        self.metric.run_action(metric["id"], "指标换版")
        plan = self.plan("ANAL-SEARCH")
        self.analysis.run_action(
            plan["id"], "生成显著性结论", {"显著性结论": "显著", "结论样本量": 21000, **OPERATOR}
        )
        again = self.abtest.list_closures(self.exp("ABTE-PAY")["id"])
        self.assertEqual(again[0]["显著性阈值"], 0.05)
        self.assertEqual(again[0]["指标版本"], "v2")
        self.assertEqual(again[0]["结项时间"], "2026-10-21T09:00:00+00:00")


class TestRoutes(Base):
    def test_export_and_closures_not_shadowed_by_entry_id(self):
        from app.main import app

        ordered: list[str] = []
        for route in app.routes:
            router = getattr(route, "original_router", None)
            if router is not None:
                ordered.extend(getattr(inner, "path", "") for inner in router.routes)
        for prefix in ("/api/abtest", "/api/analysis"):
            paths = [path for path in ordered if path.startswith(prefix)]
            self.assertLess(paths.index(f"{prefix}/export"), paths.index(f"{prefix}/{'{entry_id}'}"))
        abtest_paths = [path for path in ordered if path.startswith("/api/abtest")]
        self.assertLess(
            abtest_paths.index("/api/abtest/closures"),
            abtest_paths.index("/api/abtest/{entry_id}"),
        )


if __name__ == "__main__":
    unittest.main()
