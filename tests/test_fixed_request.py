"""The attended CLIs read their request from world-writable /private/tmp.

The path cannot move — Antigravity grants a permission against the exact
command text, so a per-run argument would mean a fresh prompt for every status
update — so the read is what has to be safe. The sticky bit stops another local
account deleting or renaming a file it does not own; it does not stop one
creating the name first, before the agent writes.

These are the cases that matters: a symlink planted at the path, a file someone
else owns, a file anyone else may rewrite, a FIFO that would block the reader
forever, and a file grown past what should be pulled into memory.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from applications import status as application_status  # noqa: E402
from applications.handoff import read_fixed_request  # noqa: E402


class TestReadFixedRequest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "request.json"

    def _write(self, payload, mode=0o644):
        self.path.write_text(json.dumps(payload))
        os.chmod(self.path, mode)
        return self.path

    def test_reads_the_mode_the_agent_actually_writes(self):
        """0644, the live mode on the M1 (umask 022). Hardening must not break it."""
        self._write({"workflow_id": "apply_abc"}, mode=0o644)
        self.assertEqual(read_fixed_request(self.path), {"workflow_id": "apply_abc"})

    def test_refuses_a_symlink(self):
        secret = self.tmp / "elsewhere.json"
        secret.write_text('{"workflow_id": "apply_attacker"}')
        self.path.symlink_to(secret)
        with self.assertRaises(ValueError) as cm:
            read_fixed_request(self.path)
        self.assertIn("symlink", str(cm.exception))

    def test_refuses_a_group_or_world_writable_file(self):
        for mode in (0o666, 0o664, 0o622):
            with self.subTest(mode=oct(mode)):
                self._write({"workflow_id": "apply_abc"}, mode=mode)
                with self.assertRaises(ValueError) as cm:
                    read_fixed_request(self.path)
                self.assertIn("writable by another account", str(cm.exception))

    def test_refuses_a_non_regular_file(self):
        fifo = self.tmp / "fifo.json"
        os.mkfifo(fifo, 0o644)
        # O_NONBLOCK so the open itself does not park waiting for a writer.
        fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        os.close(fd)
        with self.assertRaises(ValueError):
            read_fixed_request(fifo)

    def test_refuses_a_file_owned_by_another_user(self):
        self._write({"workflow_id": "apply_abc"})
        real_getuid = os.getuid

        def other_uid():
            return real_getuid() + 1

        os.getuid = other_uid
        try:
            with self.assertRaises(ValueError) as cm:
                read_fixed_request(self.path)
        finally:
            os.getuid = real_getuid
        self.assertIn("owned by another user", str(cm.exception))

    def test_refuses_an_oversized_file(self):
        self.path.write_text('{"a": "' + "x" * 5000 + '"}')
        with self.assertRaises(ValueError) as cm:
            read_fixed_request(self.path, max_bytes=1024)
        self.assertIn("size limit", str(cm.exception))

    def test_refuses_json_that_is_not_an_object(self):
        self.path.write_text('["workflow_id"]')
        with self.assertRaises(ValueError):
            read_fixed_request(self.path)

    def test_refuses_invalid_json(self):
        self.path.write_text("{not json")
        with self.assertRaises(ValueError):
            read_fixed_request(self.path)

    def test_missing_file_raises_value_error_not_oserror(self):
        with self.assertRaises(ValueError):
            read_fixed_request(self.tmp / "absent.json")


class TestStatusRequestStillParses(unittest.TestCase):
    """The hardening sits under the existing shape validation, not beside it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "status.json"

    def test_valid_request_round_trips(self):
        self.path.write_text(json.dumps({
            "workflow_id": "apply_" + "a" * 32,
            "status": "review_ready",
            "detail": "final page visible",
        }))
        request = application_status._read_request(self.path)
        self.assertEqual(request["status"], "review_ready")
        self.assertEqual(request["workflow_id"], "apply_" + "a" * 32)

    def test_symlinked_request_is_refused_before_shape_validation(self):
        real = self.tmp / "real.json"
        real.write_text(json.dumps({
            "workflow_id": "apply_" + "a" * 32, "status": "completed"}))
        self.path.symlink_to(real)
        with self.assertRaises(ValueError):
            application_status._read_request(self.path)

    def test_unknown_key_is_still_refused(self):
        self.path.write_text(json.dumps({
            "workflow_id": "apply_" + "a" * 32, "status": "failed", "token": "x"}))
        with self.assertRaises(ValueError):
            application_status._read_request(self.path)

    def test_agent_cannot_report_completed_shape_bypass(self):
        """`completed` is the owner's alone; the CLI only validates the state
        name, the server enforces the rule. Guard the vocabulary here."""
        self.path.write_text(json.dumps({
            "workflow_id": "apply_" + "a" * 32, "status": "not_a_state"}))
        with self.assertRaises(ValueError):
            application_status._read_request(self.path)


class TestOtherCLIsUseTheHardenedRead(unittest.TestCase):
    """The account broker and the other fixed-request commands read the same
    kind of file from the same directory. The broker prints credentials, so it matters most."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _planted_symlink(self, payload):
        real = self.tmp / "real.json"
        real.write_text(json.dumps(payload))
        link = self.tmp / "link.json"
        link.symlink_to(real)
        return link

    def test_account_broker_refuses_a_symlink(self):
        from unittest import mock
        from applications import account as broker
        link = self._planted_symlink({"workflow_id": "apply_1", "action": "credentials"})
        with mock.patch.object(broker, "FIXED_REQUEST", link):
            with self.assertRaises(ValueError):
                broker._fixed_request()

    def test_account_broker_refuses_a_world_writable_file(self):
        from unittest import mock
        from applications import account as broker
        path = self.tmp / "req.json"
        path.write_text(json.dumps({"workflow_id": "apply_1", "action": "credentials"}))
        os.chmod(path, 0o666)
        with mock.patch.object(broker, "FIXED_REQUEST", path):
            with self.assertRaises(ValueError):
                broker._fixed_request()



if __name__ == "__main__":
    unittest.main()
