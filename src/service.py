from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DISCHARGE_ADJUST_ROLES,
                    DISPOSITION_KIND, DISPOSITION_ROLES, ENTITY, RECORD_ROLES,
                    TITLE, VIEW_ROLES, WARNING_RECEIPT_ROLES,
                    WARNING_REGISTER_ROLES, WARNING_REPORTS, completion_blockers,
                    escalation_required, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition, warning_gaps,
                    warning_progress, warning_timeout_minutes, warning_zone)


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
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        if kind == DISPOSITION_KIND:
            ensure_role(role, DISPOSITION_ROLES)
        else:
            ensure_role(role, RECORD_ROLES)
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
        override = None
        if target == "executed":
            gaps = self._warning_gaps(item)
            if gaps:
                disposition = self._valid_disposition(item_id, gaps)
                if disposition is None:
                    reasons = "；".join(
                        f"{g['station']}:{g['reason']}" for g in gaps)
                    raise ConflictError(
                        f"预警联动存在缺口（{reasons}），指令保持待执行，需总工处置意见后授权")
                override = {
                    "gaps": [{"station": g["station"], "reason": g["reason"]} for g in gaps],
                    "disposition_by": disposition["created_by"],
                }
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        detail = {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        }
        if override is not None:
            detail["warning_override"] = override
        self.repository.append_audit("transition", ENTITY, item_id, actor, detail)
        return self.enrich(updated)

    def register_warning(self, item_id: int, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, WARNING_REGISTER_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] in ("executed", "closed"):
            raise ConflictError("指令已执行，无法登记预警")
        station = require_text(payload.get("station"), "station", 100)
        contact = require_text(payload.get("contact"), "contact", 100)
        zone = payload.get("zone")
        if zone is not None:
            zone = require_text(zone, "zone", 100)
        else:
            zone = warning_zone(item["quantity"], item["threshold"])
        planned = require_number(payload.get("planned_evacuees", 0), "planned_evacuees")
        if planned != int(planned):
            raise ValidationError("planned_evacuees必须是整数")
        task = self.repository.create_warning_task(
            item_id, station, contact, zone, int(planned), actor)
        self.repository.append_audit("warning_register", ENTITY, item_id, actor, {
            "task_id": task["id"], "station": station, "zone": zone,
            "planned_evacuees": task["planned_evacuees"],
        })
        return task

    def report_warning(self, item_id: int, task_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, WARNING_RECEIPT_ROLES)
        actor = require_text(actor, "actor", 100)
        report = payload.get("report")
        if report not in WARNING_REPORTS:
            raise ValidationError("report必须是received、unreachable或evacuated")
        reported: Optional[int] = None
        if report == "evacuated":
            number = require_number(payload.get("reported_evacuees"), "reported_evacuees")
            if number != int(number):
                raise ValidationError("reported_evacuees必须是整数")
            reported = int(number)
        task = self.repository.update_warning_receipt(
            item_id, task_id, report, reported, actor)
        self.repository.append_audit("warning_receipt", ENTITY, item_id, actor, {
            "task_id": task["id"], "station": task["station"], "report": report,
            "reported_evacuees": reported,
        })
        return task

    def adjust_discharge(self, item_id: int, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, DISCHARGE_ADJUST_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] in ("executed", "closed"):
            raise ConflictError("指令已执行，无法调整泄量")
        quantity = require_number(payload.get("quantity"), "quantity")
        zone = payload.get("zone")
        if zone is not None:
            zone = require_text(zone, "zone", 100)
        else:
            zone = warning_zone(quantity, item["threshold"])
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or expected < 1:
            raise ValidationError("expected_version必须是正整数")
        updated, rearranged, flagged = self.repository.adjust_discharge(
            item_id, quantity, zone, expected, actor)
        self.repository.append_audit("discharge_adjust", ENTITY, item_id, actor, {
            "quantity": quantity, "zone": zone,
            "rearranged": rearranged, "range_changed_kept": flagged,
        })
        return self.enrich(updated)

    def list_warnings(self, item_id: int, role: str) -> list:
        self._view(role)
        item = self.repository.get_item(item_id)
        tasks = self.repository.list_warning_tasks(item_id)
        gaps = warning_gaps(tasks, datetime.now(timezone.utc),
                            warning_timeout_minutes(item["severity"]))
        reasons: Dict[int, list] = {}
        for gap in gaps:
            reasons.setdefault(gap["task_id"], []).append(gap["reason"])
        for task in tasks:
            task["gap_reasons"] = reasons.get(task["id"], [])
        return tasks

    def _warning_gaps(self, item: Dict[str, Any]) -> list:
        tasks = self.repository.list_warning_tasks(item["id"])
        return warning_gaps(tasks, datetime.now(timezone.utc),
                            warning_timeout_minutes(item["severity"]))

    def _valid_disposition(self, item_id: int, gaps: list) -> Optional[Dict[str, Any]]:
        latest_change = max(g["updated_at"] for g in gaps)
        disposition = None
        for record in self.repository.list_records(item_id):
            if record["kind"] == DISPOSITION_KIND and record["created_at"] >= latest_change:
                disposition = record
        return disposition

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def _base_enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = self._base_enrich(item)
        tasks = self.repository.list_warning_tasks(item["id"])
        result["warning_progress"] = warning_progress(tasks, self._warning_gaps(item))
        return result
