import runpy
import unittest
from unittest.mock import Mock

from helpers import REPO

healthy_supervisor_pid = runpy.run_path(str(REPO / "tools" / "auto_upgrade_gate.py"))["healthy_supervisor_pid"]


class GateReadinessTests(unittest.TestCase):
    def test_exited_supervisor_record_defers_readiness_until_adopter_is_healthy(self):
        old = dict(code="v2.0.9", healthy=True, completed_ns=101, pid=123, ticks=11, boot_id="boot")
        adopted = dict(old, completed_ns=102, pid=456, ticks=12)
        proc_identity = Mock(side_effect=[None, dict(pid=456, ticks=12, boot_id="boot")])
        self.assertIsNone(healthy_supervisor_pid(old, "v2.0.9", 100, proc_identity))
        self.assertEqual(healthy_supervisor_pid(adopted, "v2.0.9", 100, proc_identity), 456)

    def test_previous_pass_and_reused_pid_do_not_make_adoption_ready(self):
        record = dict(code="v2.0.9", healthy=True, completed_ns=100, pid=123, ticks=11, boot_id="boot")
        proc_identity = Mock(return_value=dict(pid=123, ticks=12, boot_id="boot"))
        self.assertIsNone(healthy_supervisor_pid(record, "v2.0.9", 100, proc_identity))
        proc_identity.assert_not_called()
        self.assertIsNone(healthy_supervisor_pid(dict(record, completed_ns=101), "v2.0.9", 100, proc_identity))
