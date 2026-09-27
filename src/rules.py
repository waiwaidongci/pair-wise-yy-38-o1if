from __future__ import annotations
from datetime import datetime, timedelta
from .domain import ConflictError, ValidationError
TITLE='水库防汛调度与操作确认'; ENTITY='调度指令'; ID_PREFIX='RF'
SEVERITIES=['routine', 'attention', 'urgent', 'emergency']; STATES=['draft', 'checked', 'authorized', 'executed', 'closed']; TRANSITIONS={'draft': ['checked'], 'checked': ['authorized'], 'authorized': ['executed'], 'executed': ['closed'], 'closed': []}; TRANSITION_ROLES={'checked': ['duty_officer'], 'authorized': ['chief_engineer'], 'executed': ['dispatcher'], 'closed': ['chief_engineer']}
CREATE_ROLES=set(['duty_officer']); RECORD_ROLES=set(['duty_officer', 'dispatcher']); AUDIT_ROLES=set(['chief_engineer', 'viewer']); VIEW_ROLES=set(['duty_officer', 'chief_engineer', 'dispatcher', 'viewer'])
SEVERITY_WEIGHT={'routine': 1.0, 'attention': 3.0, 'urgent': 6.0, 'emergency': 9.0}; DEADLINE_HOURS={'routine': 72, 'attention': 24, 'urgent': 8, 'emergency': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
WARNING_REPORTS=['received', 'unreachable', 'evacuated']
WARNING_REGISTER_ROLES=set(['dispatcher']); WARNING_RECEIPT_ROLES=set(['duty_officer', 'dispatcher']); DISCHARGE_ADJUST_ROLES=set(['dispatcher'])
DISPOSITION_KIND='disposition'; DISPOSITION_ROLES=set(['chief_engineer'])
WARNING_TIMEOUT_MINUTES={'routine': 60, 'attention': 40, 'urgent': 20, 'emergency': 10}
def warning_timeout_minutes(severity):
    if severity not in WARNING_TIMEOUT_MINUTES: raise ValidationError("unknown severity")
    return WARNING_TIMEOUT_MINUTES[severity]
def warning_zone(quantity,threshold=1.0):
    ratio=quantity/threshold if threshold>0 else 1.0
    if ratio<0.5: return '下游0-5km'
    if ratio<1.0: return '下游5-10km'
    if ratio<1.5: return '下游10-20km'
    return '下游20km以外'
def warning_timed_out(task,now,timeout_minutes):
    base=datetime.fromisoformat(task["updated_at"])
    return now>=base+timedelta(minutes=timeout_minutes)
def warning_gaps(tasks,now,timeout_minutes):
    gaps=[]
    for task in tasks:
        reasons=[]; report=task.get("report")
        if report=='unreachable': reasons.append('站点失联')
        elif report is None and warning_timed_out(task,now,timeout_minutes): reasons.append('超时未回')
        planned=task.get("planned_evacuees",0); reported=task.get("reported_evacuees")
        if planned>0 and report in ('received','evacuated'):
            if reported is None: reasons.append('转移人数未回报')
            elif reported<planned: reasons.append('转移人数未核对完成')
        for reason in reasons:
            gaps.append({"task_id":task["id"],"station":task["station"],"reason":reason,"receipt_at":task.get("receipt_at"),"updated_at":task["updated_at"]})
    return gaps
def warning_progress(tasks,gaps):
    gap_ids=set(g["task_id"] for g in gaps)
    confirmed=sum(1 for t in tasks if t["id"] not in gap_ids and t.get("report") is not None)
    receipts=[{"station":t["station"],"report":t["report"],"receipt_at":t["receipt_at"],"range_changed":bool(t["range_changed"])} for t in tasks if t.get("receipt_at")]
    return {"total":len(tasks),"confirmed":confirmed,"pending":len(tasks)-confirmed,"gaps":gaps,"receipts":receipts}
