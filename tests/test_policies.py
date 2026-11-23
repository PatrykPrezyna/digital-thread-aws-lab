"""Validate IAM policy JSON and the shared event schema file."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IAM = ROOT / "iam"
PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")
EXPECTED_PLACEHOLDERS = {
    "ACCOUNT_ID",
    "BUCKET",
    "REGION",
    "S3_PREFIX",
    "S3_ERROR_PREFIX",
    "SNS_TOPIC_NAME",
    "LAMBDA_FUNCTION_NAME",
    "GLUE_DATABASE",
    "FIREHOSE_STREAM_NAME",
}


class PolicyTests(unittest.TestCase):
    def test_all_policies_parse_and_use_known_placeholders(self):
        files = sorted(IAM.glob("*.json"))
        self.assertGreaterEqual(len(files), 9)
        seen: set[str] = set()
        for path in files:
            with self.subTest(path.name):
                text = path.read_text(encoding="utf-8")
                data = json.loads(text)
                self.assertEqual(data.get("Version"), "2012-10-17")
                self.assertIn("Statement", data)
                found = set(PLACEHOLDER.findall(text))
                unknown = found - EXPECTED_PLACEHOLDERS
                self.assertFalse(unknown, msg=f"{path.name} has unknown placeholders: {unknown}")
                seen |= found
        missing = EXPECTED_PLACEHOLDERS - seen
        self.assertFalse(missing, msg=f"placeholders never used: {missing}")

    def test_least_privilege_shapes(self):
        firehose = json.loads((IAM / "firehose-permissions.json").read_text())
        actions = {a for s in firehose["Statement"] for a in s["Action"]}
        self.assertIn("s3:PutObject", actions)
        self.assertNotIn("s3:*", actions)
        self.assertNotIn("s3:DeleteBucket", actions)

        sim = json.loads((IAM / "simulator-permissions.json").read_text())
        sim_actions = {a for s in sim["Statement"] for a in s["Action"]}
        self.assertEqual(sim_actions, {"firehose:PutRecord", "firehose:PutRecordBatch"})
        resources = sim["Statement"][0]["Resource"]
        if isinstance(resources, str):
            resources = [resources]
        self.assertTrue(all("deliverystream/${FIREHOSE_STREAM_NAME}" in r for r in resources))

        lam = json.loads((IAM / "lambda-permissions.json").read_text())
        joined = json.dumps(lam)
        self.assertIn("sns:Publish", joined)
        self.assertIn("s3:GetObject", joined)
        self.assertNotIn("sns:*", joined)

    def test_event_schema_is_valid_json_and_lists_states(self):
        schema = json.loads((ROOT / "shared" / "event.schema.json").read_text(encoding="utf-8"))
        states = schema["properties"]["state"]["enum"]
        for s in ("IDLE", "HOMING", "PICKING", "MOVING", "PLACING", "FAULT", "ESTOP", "RECOVERING"):
            self.assertIn(s, states)
        types = schema["properties"]["event_type"]["enum"]
        for t in ("PLACE_OK", "GRIP_FAIL", "FAULT", "ESTOP", "TELEMETRY"):
            self.assertIn(t, types)
        for field in ("timestamp", "event_type", "robot_id", "program_version", "firmware_version", "j1_deg", "tcp_x_mm"):
            self.assertIn(field, schema["required"])
        # Car-era vocabulary must stay out of the contract.
        blob = json.dumps(schema)
        for banned in ("car_id", "PARCEL_", "OBSTACLE", "DRIVING", "battery_pct", "speed_mps"):
            self.assertNotIn(banned, blob)


if __name__ == "__main__":
    unittest.main()
