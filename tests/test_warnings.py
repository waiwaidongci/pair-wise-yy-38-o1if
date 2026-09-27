import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
class WarningLinkageTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"泄洪指令","description":"下游乡镇预警联动","severity":"urgent","quantity":500,"threshold":100,"external_ref":"WARN-1"},"creator","duty_officer")
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _warning(self,station="幸福镇",expected=10,range_min=100,range_max=1000):
        return self.service.add_warning(self.item["id"],{"station":station,"contact":"站长","range_min":range_min,"range_max":range_max,"expected_evacuees":expected},"dispatcher","dispatcher")
    def _to_authorized(self):
        current=self.service.transition(self.item["id"],"checked",self.item["version"],"reviewer","duty_officer")
        return self.service.transition(current["id"],"authorized",current["version"],"reviewer","chief_engineer")
    def _fresh(self): return self.service.get_item(self.item["id"],"viewer")
    def test_register_warning_roles_and_validation(self):
        warning=self._warning()
        self.assertEqual(warning["status"],"pending"); self.assertTrue(warning["in_range"]); self.assertIsNotNone(warning["due_at"])
        with self.assertRaises(PermissionDenied): self.service.add_warning(self.item["id"],{"station":"A","contact":"B","range_min":0,"range_max":1},"x","duty_officer")
        with self.assertRaises(ValidationError): self._warning(range_min=900,range_max=100)
        with self.assertRaises(ValidationError): self._warning(expected=-1)
    def test_receipts_and_gap_reasons(self):
        w1=self._warning("幸福镇",expected=10); w2=self._warning("和平乡",expected=5)
        self.service.add_receipt(self.item["id"],w1["id"],{"result":"received","evacuated_count":10},"op","dispatcher")
        self.service.add_receipt(self.item["id"],w2["id"],{"result":"unreachable"},"op","duty_officer")
        detail=self._fresh()
        self.assertEqual(len(detail["gaps"]),1)
        self.assertEqual(detail["gaps"][0]["station"],"和平乡"); self.assertIn("站点失联",detail["gaps"][0]["reasons"])
        progress=detail["warning_progress"]
        self.assertEqual(progress["confirmed"],1); self.assertEqual(progress["unreachable"],1); self.assertEqual(progress["gaps"],1)
        self.service.add_receipt(self.item["id"],w1["id"],{"result":"received","evacuated_count":4},"op","dispatcher")
        detail=self._fresh(); stations={g["station"]:g["reasons"] for g in detail["gaps"]}
        self.assertIn("转移人数未核对完",stations["幸福镇"])
        view=[w for w in detail["warnings"] if w["station"]=="幸福镇"][0]
        self.assertIsNotNone(view["last_receipt_at"]); self.assertEqual(view["evacuated_count"],4)
        with self.assertRaises(PermissionDenied): self.service.add_receipt(self.item["id"],w1["id"],{"result":"received"},"x","viewer")
    def test_execution_blocked_until_disposition(self):
        warning=self._warning("幸福镇",expected=10)
        self.service.add_receipt(self.item["id"],warning["id"],{"result":"unreachable"},"op","dispatcher")
        self._to_authorized(); item=self._fresh()
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(item["id"],"executed",item["version"],"exec","dispatcher")
        self.assertIn("站点失联",str(ctx.exception)); self.assertIn("处置意见",str(ctx.exception))
        with self.assertRaises(PermissionDenied):
            self.service.add_disposition(self.item["id"],{"opinion":"强行执行"},"x","dispatcher")
        self.service.add_disposition(self.item["id"],{"opinion":"已电话确认副站长组织转移，同意执行"},"chief","chief_engineer")
        item=self._fresh()
        executed=self.service.transition(item["id"],"executed",item["version"],"exec","dispatcher")
        self.assertEqual(executed["status"],"executed")
        events=[e for e in self.service.audit("viewer",self.item["id"]) if e["action"]=="transition" and e["detail"].get("to")=="executed"]
        self.assertIn("gaps_overridden",events[0]["detail"]); self.assertTrue(self.repo.verify_audit_chain())
    def test_disposition_must_follow_latest_warning_activity(self):
        w1=self._warning("幸福镇"); w2=self._warning("和平乡")
        self.service.add_receipt(self.item["id"],w2["id"],{"result":"unreachable"},"op","dispatcher")
        self._to_authorized()
        self.service.add_disposition(self.item["id"],{"opinion":"和平乡由副站长兜底"},"chief","chief_engineer")
        self.service.add_receipt(self.item["id"],w1["id"],{"result":"unreachable"},"op","dispatcher")
        item=self._fresh()
        with self.assertRaises(ConflictError): self.service.transition(item["id"],"executed",item["version"],"exec","dispatcher")
        self.service.add_disposition(self.item["id"],{"opinion":"两站均已电话兜底确认"},"chief","chief_engineer")
        item=self._fresh()
        self.assertEqual(self.service.transition(item["id"],"executed",item["version"],"exec","dispatcher")["status"],"executed")
    def test_adjust_requeues_unconfirmed_and_marks_range_change(self):
        w1=self._warning("幸福镇",expected=2)
        w2=self._warning("和平乡",expected=5)
        w3=self._warning("下游滩区",expected=0,range_min=100,range_max=600)
        self.service.add_receipt(self.item["id"],w1["id"],{"result":"received","evacuated_count":2},"op","dispatcher")
        self.service.add_receipt(self.item["id"],w3["id"],{"result":"unreachable"},"op","dispatcher")
        before=self._fresh(); self.assertIn("下游滩区",[g["station"] for g in before["gaps"]])
        result=self.service.adjust_quantity(self.item["id"],{"quantity":900,"expected_version":before["version"]},"adj","dispatcher")
        self.assertEqual(result["quantity"],900)
        views={w["station"]:w for w in result["warnings"]}
        self.assertEqual(views["和平乡"]["requeue_count"],1)
        self.assertEqual(views["下游滩区"]["requeue_count"],1)
        self.assertEqual(views["幸福镇"]["requeue_count"],0)
        self.assertEqual(views["幸福镇"]["status"],"received")
        self.assertTrue(views["幸福镇"]["range_changed"])
        self.assertFalse(views["下游滩区"]["in_range"])
        self.assertEqual(result["gaps"],[])
        self.assertEqual(views["幸福镇"]["receipt_count"],1)
    def test_adjust_constraints(self):
        with self.assertRaises(PermissionDenied): self.service.adjust_quantity(self.item["id"],{"quantity":600,"expected_version":1},"x","duty_officer")
        with self.assertRaises(ValidationError): self.service.adjust_quantity(self.item["id"],{"quantity":500,"expected_version":1},"x","dispatcher")
        with self.assertRaises(ValueError): self.service.adjust_quantity(self.item["id"],{"quantity":600},"x","dispatcher")
        self._to_authorized(); item=self._fresh()
        self.service.transition(item["id"],"executed",item["version"],"exec","dispatcher")
        item=self._fresh()
        with self.assertRaises(ConflictError): self.service.adjust_quantity(self.item["id"],{"quantity":600,"expected_version":item["version"]},"x","dispatcher")
        with self.assertRaises(ConflictError): self._warning()
    def test_overdue_blocks_execution(self):
        warning=self._warning("幸福镇")
        with self.repo._lock, self.repo.conn:
            self.repo.conn.execute("UPDATE warnings SET due_at=? WHERE id=?",("2000-01-01T00:00:00+00:00",warning["id"]))
        detail=self._fresh()
        self.assertIn("超时未回",detail["gaps"][0]["reasons"])
        self._to_authorized(); item=self._fresh()
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(item["id"],"executed",item["version"],"exec","dispatcher")
        self.assertIn("超时未回",str(ctx.exception))
    def test_list_shows_progress_reasons_receipt_time(self):
        w1=self._warning("幸福镇",expected=3); w2=self._warning("和平乡",expected=5)
        self.service.add_receipt(self.item["id"],w1["id"],{"result":"received","evacuated_count":3},"op","dispatcher")
        self.service.add_receipt(self.item["id"],w2["id"],{"result":"unreachable"},"op","dispatcher")
        entry=self.service.list_items("viewer")[0]
        progress=entry["warning_progress"]
        self.assertEqual(progress["total"],2); self.assertEqual(progress["confirmed"],1); self.assertEqual(progress["gaps"],1)
        self.assertTrue(any("和平乡" in reason for reason in progress["reasons"]))
        self.assertIsNotNone(progress["last_receipt_at"])
if __name__=="__main__": unittest.main()
