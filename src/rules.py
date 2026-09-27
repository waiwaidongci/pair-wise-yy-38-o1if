from __future__ import annotations
from datetime import datetime
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

# ---- 预警联动 ----
WARNING_TIMEOUT_HOURS={'routine': 12, 'attention': 6, 'urgent': 2, 'emergency': 1}
RECEIPT_RESULTS=['received', 'unreachable']
WARNING_STATUS_LABELS={'pending': '待回报', 'received': '已收到', 'unreachable': '失联'}
WARNING_ROLES=set(['dispatcher']); RECEIPT_ROLES=set(['duty_officer', 'dispatcher'])
ADJUST_ROLES=set(['dispatcher']); DISPOSITION_ROLES=set(['chief_engineer'])
ADJUSTABLE_STATES=set(['draft', 'checked', 'authorized'])
GAP_UNREACHABLE='站点失联'; GAP_OVERDUE='超时未回'; GAP_HEADCOUNT='转移人数未核对完'
def warning_in_range(quantity,range_min,range_max): return range_min<=quantity<=range_max
def warning_overdue(due_at,now):
    if not due_at: return False
    try: return datetime.fromisoformat(str(now))>datetime.fromisoformat(str(due_at))
    except ValueError: return str(now)>str(due_at)
def warning_gap_reasons(status,due_at,expected_evacuees,evacuated_count,in_range,now):
    reasons=[]
    if not in_range: return reasons
    if status=='unreachable': reasons.append(GAP_UNREACHABLE)
    elif status=='pending' and warning_overdue(due_at,now): reasons.append(GAP_OVERDUE)
    if status=='received' and expected_evacuees>0 and (evacuated_count is None or evacuated_count<expected_evacuees): reasons.append(GAP_HEADCOUNT)
    return reasons
