"""Unit tests for the fault-alert Lambda, with a mocked SNS client."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambda"))

import fault_alert  # noqa: E402

NDJSON = (ROOT / "tests" / "sample_events.ndjson").read_bytes()
CONCAT = (ROOT / "tests" / "sample_events_concat.json").read_bytes()


class FakeSNS:
    def __init__(self) -> None:
        self.published = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "fake-id"}


class ParseTests(unittest.TestCase):
    def test_ndjson(self):
        events = fault_alert.parse_records(NDJSON)
        self.assertGreaterEqual(len(events), 10)
        types = [e["event_type"] for e in events]
        self.assertIn("GRIP_FAIL", types)
        self.assertIn("FAULT", types)
        self.assertIn("ESTOP", types)

    def test_concatenated_json(self):
        events = fault_alert.parse_records(CONCAT)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event_type"], "ITEM_PRESENTED")
        self.assertEqual(events[1]["event_type"], "FAULT")

    def test_empty(self):
        self.assertEqual(fault_alert.parse_records(b"   "), [])


class HandlerCoreTests(unittest.TestCase):
    def test_publishes_summary_when_faults_and_burst_present(self):
        sns = FakeSNS()
        result = fault_alert.handle_object(
            NDJSON,
            "s3://lab/telemetry/2026/11/22/15/events.json",
            sns,
            "arn:aws:sns:us-east-1:123:dtlab-fault-alerts",
            fault_alert.DEFAULT_FAULT_TYPES,
            grip_fail_burst=3,
        )
        self.assertGreaterEqual(result["events"], 10)
        # 3 GRIP_FAIL + 1 FAULT + 1 ESTOP
        self.assertEqual(result["faults"], 5)
        self.assertTrue(result["published"])
        self.assertEqual(len(sns.published), 1)
        msg = sns.published[0]["Message"]
        self.assertIn("FAULT", msg)
        self.assertIn("ESTOP", msg)
        self.assertIn("GRIP_FAIL", msg)
        self.assertIn("pack-v1.2.0", msg)
        self.assertIn("fw-3.4.1", msg)
        self.assertLessEqual(len(sns.published[0]["Subject"]), 100)

    def test_silent_when_no_faults(self):
        only_ok = b'{"event_type":"PLACE_OK","robot_id":"arm-01"}\n'
        sns = FakeSNS()
        result = fault_alert.handle_object(
            only_ok, "s3://lab/ok.json", sns, "arn:aws:sns:us-east-1:123:t", ("FAULT", "ESTOP", "GRIP_FAIL")
        )
        self.assertEqual(result["faults"], 0)
        self.assertFalse(result["published"])
        self.assertEqual(sns.published, [])

    def test_single_grip_fail_is_not_a_burst(self):
        body = b'{"event_type":"GRIP_FAIL","robot_id":"arm-01","box_id":2}\n{"event_type":"PICK_OK","robot_id":"arm-01"}\n'
        sns = FakeSNS()
        result = fault_alert.handle_object(
            body, "s3://lab/one.json", sns, "arn:aws:sns:us-east-1:123:t", ("FAULT", "ESTOP", "GRIP_FAIL"), grip_fail_burst=3
        )
        self.assertEqual(result["grip_fails"], 1)
        self.assertEqual(result["faults"], 0)
        self.assertFalse(result["published"])

    def test_grip_fail_burst_alone_publishes(self):
        body = b"".join(b'{"event_type":"GRIP_FAIL","robot_id":"arm-01"}\n' for _ in range(3))
        sns = FakeSNS()
        result = fault_alert.handle_object(
            body, "s3://lab/burst.json", sns, "arn:aws:sns:us-east-1:123:t", ("FAULT", "ESTOP", "GRIP_FAIL"), grip_fail_burst=3
        )
        self.assertEqual(result["faults"], 3)
        self.assertTrue(result["published"])


class HandlerWiringTests(unittest.TestCase):
    def test_handler_reads_s3_and_skips_error_prefix(self):
        fake_s3 = MagicMock()
        fake_s3.get_object.return_value = {"Body": MagicMock(read=lambda: NDJSON)}
        fake_sns = FakeSNS()

        event = {
            "Records": [
                {
                    "eventSource": "aws:s3",
                    "s3": {"bucket": {"name": "lab"}, "object": {"key": "telemetry/2026/11/22/15/file"}},
                },
                {
                    "eventSource": "aws:s3",
                    "s3": {"bucket": {"name": "lab"}, "object": {"key": "errors/2026/11/22/15/failed"}},
                },
            ]
        }

        with patch.object(fault_alert, "boto3") as boto3_mod:
            with patch.dict("os.environ", {"SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123:t"}):
                boto3_mod.client.side_effect = lambda service: fake_s3 if service == "s3" else fake_sns
                result = fault_alert.handler(event, None)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(fake_s3.get_object.call_count, 1)
        self.assertEqual(len(fake_sns.published), 1)


if __name__ == "__main__":
    unittest.main()
