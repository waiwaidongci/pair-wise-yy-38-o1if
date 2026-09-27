import unittest
from src import rules
from src.domain import ConflictError, ValidationError
class RulesTest(unittest.TestCase):
    def test_priority_deadline_and_escalation(self):
        low=rules.priority_score(rules.SEVERITIES[0],1,10,0); high=rules.priority_score(rules.SEVERITIES[-1],30,10,3)
        self.assertGreater(high,low); self.assertLessEqual(rules.response_deadline_hours(rules.SEVERITIES[-1],30,10),rules.response_deadline_hours(rules.SEVERITIES[0],1,10))
        self.assertTrue(rules.escalation_required(rules.SEVERITIES[-1],1,10)); self.assertTrue(rules.escalation_required(rules.SEVERITIES[0],10,10))
    def test_transition_guards(self):
        self.assertTrue(rules.can_transition(rules.STATES[0],rules.STATES[1]))
        with self.assertRaises(ConflictError): rules.validate_transition(rules.STATES[0],rules.STATES[-1])
        with self.assertRaises(ValidationError): rules.priority_score("not-a-severity",1,1)
    def test_warning_gap_reasons(self):
        past="2000-01-01T00:00:00+00:00"; future="2999-01-01T00:00:00+00:00"; now="2026-09-27T00:00:00+00:00"
        self.assertEqual(rules.warning_gap_reasons('pending',past,0,None,True,now),[rules.GAP_OVERDUE])
        self.assertEqual(rules.warning_gap_reasons('pending',future,0,None,True,now),[])
        self.assertEqual(rules.warning_gap_reasons('pending',past,0,None,False,now),[])
        self.assertEqual(rules.warning_gap_reasons('unreachable',future,0,None,True,now),[rules.GAP_UNREACHABLE])
        self.assertEqual(rules.warning_gap_reasons('unreachable',future,0,None,False,now),[])
        self.assertEqual(rules.warning_gap_reasons('received',future,10,4,True,now),[rules.GAP_HEADCOUNT])
        self.assertEqual(rules.warning_gap_reasons('received',future,10,None,True,now),[rules.GAP_HEADCOUNT])
        self.assertEqual(rules.warning_gap_reasons('received',future,10,10,True,now),[])
        self.assertEqual(rules.warning_gap_reasons('received',future,0,None,True,now),[])
        self.assertTrue(rules.warning_in_range(500,100,1000)); self.assertFalse(rules.warning_in_range(90,100,1000))
if __name__=="__main__": unittest.main()
