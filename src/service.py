from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .audit import utc_now
from .domain import (ConflictError, NotFoundError, ValidationError, ensure_role,
                     normalize_severity, require_int, require_number, require_text)
from .repository import Repository
from .rules import (ADJUST_ROLES, ADJUSTABLE_STATES, AUDIT_ROLES, CREATE_ROLES,
                    DISPOSITION_ROLES, ENTITY, RECEIPT_RESULTS, RECEIPT_ROLES,
                    RECORD_ROLES, TITLE, VIEW_ROLES, WARNING_ROLES,
                    WARNING_STATUS_LABELS, WARNING_TIMEOUT_HOURS,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition, warning_gap_reasons, warning_in_range,
                    warning_overdue)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        gaps: List[Dict[str, Any]] = []
        if target == "executed":
            gaps = self.warning_gaps(item_id)
            if gaps and not self._disposition_fresh(item_id):
                gap_text = "；".join(gap["label"] for gap in gaps)
                raise ConflictError(f"预警缺口未闭环：{gap_text}。需总工处置意见后才能执行")
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        detail: Dict[str, Any] = {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        }
        if target == "executed" and gaps:
            disposition = self.repository.latest_disposition(item_id)
            detail["gaps_overridden"] = [gap["label"] for gap in gaps]
            detail["disposition_id"] = disposition["id"] if disposition else None
        self.repository.append_audit("transition", ENTITY, item_id, actor, detail)
        return self.enrich(updated)

    def add_warning(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, WARNING_ROLES)
        actor = require_text(actor, "actor", 100)
        station = require_text(payload.get("station"), "station", 100)
        contact = require_text(payload.get("contact"), "contact", 100)
        range_min = require_number(payload.get("range_min"), "range_min")
        range_max = require_number(payload.get("range_max"), "range_max")
        if range_min > range_max:
            raise ValidationError("预警区间下限不能大于上限")
        expected = require_int(payload.get("expected_evacuees", 0), "expected_evacuees")
        item = self.repository.get_item(item_id)
        if item["status"] not in ADJUSTABLE_STATES:
            raise ConflictError(f"{item['status']}状态不能登记预警")
        warning = self.repository.create_warning(
            item_id, station, contact, range_min, range_max, expected,
            self._warning_due(item["severity"]), actor)
        self.repository.append_audit("warning", ENTITY, item_id, actor, {
            "warning_id": warning["id"], "station": station,
            "range_min": range_min, "range_max": range_max,
            "expected_evacuees": expected, "due_at": warning["due_at"],
        })
        return self._warning_view_by_id(item, warning["id"])

    def add_receipt(self, item_id: int, warning_id: int, payload: Dict[str, Any],
                    actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECEIPT_ROLES)
        actor = require_text(actor, "actor", 100)
        result = payload.get("result")
        if result not in RECEIPT_RESULTS:
            raise ValidationError("result必须是received或unreachable")
        evacuated = payload.get("evacuated_count")
        if evacuated is not None:
            evacuated = require_int(evacuated, "evacuated_count")
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 500)
        if result == "unreachable":
            evacuated = None
        item = self.repository.get_item(item_id)
        if item["status"] == "closed":
            raise ConflictError("指令已关闭，不能登记回执")
        warning = self.repository.get_warning(warning_id)
        if warning["item_id"] != item_id:
            raise NotFoundError("预警不存在")
        receipt = self.repository.add_receipt(
            warning_id, item_id, result, evacuated, note,
            item["quantity"], warning["range_min"], warning["range_max"], actor)
        self.repository.append_audit("receipt", ENTITY, item_id, actor, {
            "warning_id": warning_id, "station": warning["station"],
            "result": result, "evacuated_count": evacuated,
        })
        return receipt

    def add_disposition(self, item_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, DISPOSITION_ROLES)
        actor = require_text(actor, "actor", 100)
        opinion = require_text(payload.get("opinion"), "opinion")
        self.repository.get_item(item_id)
        record = self.repository.add_record(item_id, "disposition", opinion,
                                            "closed", None, actor)
        self.repository.append_audit("disposition", ENTITY, item_id, actor, {
            "record_id": record["id"],
            "gaps": [gap["label"] for gap in self.warning_gaps(item_id)],
        })
        return record

    def adjust_quantity(self, item_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, ADJUST_ROLES)
        actor = require_text(actor, "actor", 100)
        quantity = require_number(payload.get("quantity"), "quantity")
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or expected < 1:
            raise ValueError("expected_version必须是正整数")
        item = self.repository.get_item(item_id)
        if item["status"] not in ADJUSTABLE_STATES:
            raise ConflictError(f"{item['status']}状态不能调整泄量")
        if quantity == item["quantity"]:
            raise ValidationError("新泄量与当前泄量一致")
        updated = self.repository.adjust_quantity(item_id, quantity, expected, actor)
        views = self._warning_views(updated)
        requeued = [view["id"] for view in views if view["status"] != "received"]
        if requeued:
            self.repository.requeue_warnings(
                item_id, requeued, self._warning_due(updated["severity"]))
        self.repository.append_audit("adjust", ENTITY, item_id, actor, {
            "from": item["quantity"], "to": quantity, "requeued": requeued,
        })
        return self._detail(self.repository.get_item(item_id))

    def warning_gaps(self, item_id: int) -> List[Dict[str, Any]]:
        item = self.repository.get_item(item_id)
        gaps = []
        for view in self._warning_views(item):
            if view["gap_reasons"]:
                gaps.append({
                    "warning_id": view["id"], "station": view["station"],
                    "reasons": view["gap_reasons"],
                    "label": f"{view['station']}：{'、'.join(view['gap_reasons'])}",
                })
        return gaps

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._detail(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        items = []
        for item in self.repository.list_items(status):
            row = self.enrich(item)
            row["warning_progress"] = self._progress(self._warning_views(item))
            items.append(row)
        return items

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result

    @staticmethod
    def _warning_due(severity: str) -> str:
        hours = WARNING_TIMEOUT_HOURS[severity]
        moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=hours)
        return moment.isoformat()

    def _disposition_fresh(self, item_id: int) -> bool:
        last_disposition = None
        last_warning_event = None
        for event in self.repository.list_audit(item_id):
            if event["action"] == "disposition":
                last_disposition = event["id"]
            elif event["action"] in ("warning", "receipt", "adjust"):
                last_warning_event = event["id"]
        return (last_disposition is not None
                and (last_warning_event is None or last_disposition > last_warning_event))

    def _warning_views(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        now = utc_now()
        warnings = self.repository.list_warnings(item["id"])
        receipts = self.repository.list_item_receipts(item["id"])
        by_warning: Dict[int, List[Dict[str, Any]]] = {}
        for receipt in receipts:
            by_warning.setdefault(receipt["warning_id"], []).append(receipt)
        views = []
        for warning in warnings:
            history = by_warning.get(warning["id"], [])
            latest = history[-1] if history else None
            status = latest["result"] if latest else "pending"
            evacuated = (latest["evacuated_count"]
                         if latest and latest["result"] == "received" else None)
            in_range = warning_in_range(item["quantity"], warning["range_min"],
                                        warning["range_max"])
            reasons = warning_gap_reasons(status, warning["due_at"],
                                          warning["expected_evacuees"], evacuated,
                                          in_range, now)
            view = dict(warning)
            view.update({
                "status": status,
                "status_label": WARNING_STATUS_LABELS[status],
                "in_range": in_range,
                "overdue": status == "pending" and warning_overdue(warning["due_at"], now),
                "evacuated_count": evacuated,
                "last_receipt_at": latest["created_at"] if latest else None,
                "receipt_count": len(history),
                "range_changed": bool(latest) and (
                    latest["quantity"] != item["quantity"]
                    or latest["range_min"] != warning["range_min"]
                    or latest["range_max"] != warning["range_max"]),
                "gap_reasons": reasons,
                "receipts": history,
            })
            views.append(view)
        return views

    def _warning_view_by_id(self, item: Dict[str, Any], warning_id: int) -> Dict[str, Any]:
        for view in self._warning_views(item):
            if view["id"] == warning_id:
                return view
        raise NotFoundError("预警不存在")

    @staticmethod
    def _progress(views: List[Dict[str, Any]]) -> Dict[str, Any]:
        confirmed = sum(1 for view in views if view["status"] == "received")
        unreachable = sum(1 for view in views if view["status"] == "unreachable")
        gap_views = [view for view in views if view["gap_reasons"]]
        receipt_times = [view["last_receipt_at"] for view in views if view["last_receipt_at"]]
        return {
            "total": len(views), "confirmed": confirmed, "unreachable": unreachable,
            "pending": len(views) - confirmed - unreachable,
            "in_range": sum(1 for view in views if view["in_range"]),
            "gaps": len(gap_views),
            "reasons": [f"{view['station']}：{'、'.join(view['gap_reasons'])}"
                        for view in gap_views],
            "last_receipt_at": max(receipt_times) if receipt_times else None,
        }

    def _detail(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = self.enrich(item)
        views = self._warning_views(item)
        result["warnings"] = views
        result["gaps"] = [{"warning_id": view["id"], "station": view["station"],
                           "reasons": view["gap_reasons"]}
                          for view in views if view["gap_reasons"]]
        result["warning_progress"] = self._progress(views)
        result["disposition"] = None
        disposition = self.repository.latest_disposition(item["id"])
        if disposition:
            result["disposition"] = {
                "opinion": disposition["detail"],
                "created_by": disposition["created_by"],
                "created_at": disposition["created_at"],
                "fresh": self._disposition_fresh(item["id"]),
            }
        return result
