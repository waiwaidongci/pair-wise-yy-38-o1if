import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES, warning_zone


class WarningTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()

    def make_item(self, **kw):
        payload = {"title": "泄洪指令", "description": "预警联动测试",
                   "severity": "urgent", "quantity": 5, "threshold": 10,
                   "external_ref": kw.pop("external_ref", None)}
        payload.update(kw)
        return self.service.create_item(payload, "creator", "duty_officer")

    def to_authorized(self, item):
        current = item
        for target in STATES[1:3]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def execute(self, item):
        return self.service.transition(item["id"], "executed", item["version"],
                                       "dispatcher", "dispatcher")


class WarningLinkageTest(WarningTestBase):
    def test_register_report_and_progress(self):
        item = self.make_item()
        t1 = self.service.register_warning(item["id"], {
            "station": "甲镇", "contact": "张三", "planned_evacuees": 12},
            "disp", "dispatcher")
        t2 = self.service.register_warning(item["id"], {
            "station": "乙镇", "contact": "李四", "zone": "下游5-10km",
            "planned_evacuees": 0}, "disp", "dispatcher")
        self.assertEqual(t1["zone"], warning_zone(5, 10))
        self.assertEqual(t2["zone"], "下游5-10km")
        self.service.report_warning(item["id"], t1["id"], {
            "report": "evacuated", "reported_evacuees": 12}, "op", "duty_officer")
        self.service.report_warning(item["id"], t2["id"], {
            "report": "received"}, "op", "duty_officer")
        detail = self.service.get_item(item["id"], "viewer")
        progress = detail["warning_progress"]
        self.assertEqual(progress["total"], 2)
        self.assertEqual(progress["confirmed"], 2)
        self.assertEqual(progress["gaps"], [])
        self.assertEqual(len(progress["receipts"]), 2)
        self.assertTrue(all(r["receipt_at"] for r in progress["receipts"]))
        tasks = self.service.list_warnings(item["id"], "viewer")
        self.assertTrue(all(t["gap_reasons"] == [] for t in tasks))

    def test_unreachable_blocks_execution_until_disposition(self):
        item = self.to_authorized(self.make_item())
        task = self.service.register_warning(item["id"], {
            "station": "丙镇", "contact": "王五", "planned_evacuees": 0},
            "disp", "dispatcher")
        self.service.report_warning(item["id"], task["id"], {
            "report": "unreachable"}, "op", "duty_officer")
        with self.assertRaises(ConflictError) as ctx:
            self.execute(self.service.get_item(item["id"], "viewer"))
        self.assertIn("丙镇", str(ctx.exception))
        self.assertIn("站点失联", str(ctx.exception))
        with self.assertRaises(PermissionDenied):
            self.service.add_record(item["id"], {
                "kind": "disposition", "detail": "强行执行", "status": "closed"},
                "disp", "dispatcher")
        self.service.add_record(item["id"], {
            "kind": "disposition", "detail": "已派预备队现场核实，同意执行",
            "status": "closed"}, "chief", "chief_engineer")
        executed = self.execute(self.service.get_item(item["id"], "viewer"))
        self.assertEqual(executed["status"], "executed")
        events = self.service.audit("viewer", item["id"])
        override = [e for e in events if e["action"] == "transition"
                    and "warning_override" in e["detail"]]
        self.assertEqual(len(override), 1)
        self.assertEqual(override[0]["detail"]["warning_override"]
                         ["gaps"][0]["reason"], "站点失联")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_timeout_and_headcount_gaps(self):
        item = self.to_authorized(self.make_item())
        t1 = self.service.register_warning(item["id"], {
            "station": "丁镇", "contact": "赵六", "planned_evacuees": 0},
            "disp", "dispatcher")
        t2 = self.service.register_warning(item["id"], {
            "station": "戊镇", "contact": "孙七", "planned_evacuees": 10},
            "disp", "dispatcher")
        # 未超时且无回报：不构成缺口
        self.assertEqual(self.service.get_item(item["id"], "viewer")
                         ["warning_progress"]["gaps"], [])
        # 人为回拨更新时间，模拟超时未回
        self.repo.conn.execute(
            "UPDATE warning_tasks SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (t1["id"],))
        # 人数没核对完
        self.service.report_warning(item["id"], t2["id"], {
            "report": "evacuated", "reported_evacuees": 6}, "op", "duty_officer")
        progress = self.service.get_item(item["id"], "viewer")["warning_progress"]
        reasons = {(g["station"], g["reason"]) for g in progress["gaps"]}
        self.assertIn(("丁镇", "超时未回"), reasons)
        self.assertIn(("戊镇", "转移人数未核对完成"), reasons)
        with self.assertRaises(ConflictError):
            self.execute(self.service.get_item(item["id"], "viewer"))
        # 补齐人数后该缺口消除，但超时缺口仍在
        self.service.report_warning(item["id"], t2["id"], {
            "report": "evacuated", "reported_evacuees": 10}, "op", "duty_officer")
        progress = self.service.get_item(item["id"], "viewer")["warning_progress"]
        self.assertEqual([g["reason"] for g in progress["gaps"]], ["超时未回"])

    def test_discharge_adjust_rearranges_and_flags(self):
        item = self.to_authorized(self.make_item(quantity=5, threshold=10))
        t1 = self.service.register_warning(item["id"], {
            "station": "己镇", "contact": "周八", "planned_evacuees": 0},
            "disp", "dispatcher")
        t2 = self.service.register_warning(item["id"], {
            "station": "庚镇", "contact": "吴九", "planned_evacuees": 3},
            "disp", "dispatcher")
        self.service.report_warning(item["id"], t2["id"], {
            "report": "evacuated", "reported_evacuees": 3}, "op", "duty_officer")
        with self.assertRaises(ConflictError):
            self.service.adjust_discharge(item["id"], {
                "quantity": 12, "expected_version": 99}, "disp", "dispatcher")
        current = self.service.get_item(item["id"], "viewer")
        updated = self.service.adjust_discharge(item["id"], {
            "quantity": 12, "expected_version": current["version"]},
            "disp", "dispatcher")
        self.assertEqual(updated["quantity"], 12)
        tasks = {t["station"]: t
                 for t in self.service.list_warnings(item["id"], "viewer")}
        new_zone = warning_zone(12, 10)
        # 未确认预警按新范围重排
        self.assertEqual(tasks["己镇"]["zone"], new_zone)
        self.assertEqual(tasks["己镇"]["range_changed"], 0)
        self.assertIsNone(tasks["己镇"]["receipt_at"])
        # 已有回执保留并标出范围变化
        self.assertEqual(tasks["庚镇"]["zone"], new_zone)
        self.assertEqual(tasks["庚镇"]["range_changed"], 1)
        self.assertEqual(tasks["庚镇"]["report"], "evacuated")
        self.assertEqual(tasks["庚镇"]["reported_evacuees"], 3)
        self.assertIsNotNone(tasks["庚镇"]["receipt_at"])
        receipts = updated["warning_progress"]["receipts"]
        self.assertTrue(any(r["range_changed"] for r in receipts))

    def test_disposition_expires_when_gaps_change(self):
        item = self.to_authorized(self.make_item())
        task = self.service.register_warning(item["id"], {
            "station": "辛镇", "contact": "郑十", "planned_evacuees": 0},
            "disp", "dispatcher")
        self.service.report_warning(item["id"], task["id"], {
            "report": "unreachable"}, "op", "duty_officer")
        self.service.add_record(item["id"], {
            "kind": "disposition", "detail": "同意先行执行", "status": "closed"},
            "chief", "chief_engineer")
        # 处置意见写于缺口变化之前（模拟），泄量调整重排后意见失效
        self.repo.conn.execute(
            "UPDATE records SET created_at='2000-01-01T00:00:00+00:00'"
            " WHERE kind='disposition'")
        current = self.service.get_item(item["id"], "viewer")
        self.service.adjust_discharge(item["id"], {
            "quantity": 8, "expected_version": current["version"]},
            "disp", "dispatcher")
        with self.assertRaises(ConflictError):
            self.execute(self.service.get_item(item["id"], "viewer"))

    def test_permission_and_validation(self):
        item = self.make_item()
        with self.assertRaises(PermissionDenied):
            self.service.register_warning(item["id"], {
                "station": "壬镇", "contact": "某人"}, "v", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.report_warning(item["id"], 1, {"report": "received"},
                                        "c", "chief_engineer")
        with self.assertRaises(ValidationError):
            self.service.register_warning(item["id"], {
                "station": "壬镇", "contact": "某人", "planned_evacuees": 1.5},
                "disp", "dispatcher")
        task = self.service.register_warning(item["id"], {
            "station": "壬镇", "contact": "某人", "planned_evacuees": 2},
            "disp", "dispatcher")
        with self.assertRaises(ValidationError):
            self.service.report_warning(item["id"], task["id"],
                                        {"report": "evacuated"}, "op", "dispatcher")
        with self.assertRaises(ValidationError):
            self.service.report_warning(item["id"], task["id"],
                                        {"report": "bogus"}, "op", "dispatcher")


if __name__ == "__main__":
    unittest.main()
