"""Local tests for the 5-axis packing-arm simulator (no AWS calls)."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "agent"))

from simulate import (  # noqa: E402
    EVENT_TYPES,
    STATES,
    PackingArmSimulator,
    SimulatorConfig,
    encode_record,
    forward_kinematics,
)
from arm_agent import normalize_event  # noqa: E402


class SimulatorTests(unittest.TestCase):
    def _run(self, duration: float = 45.0, **kwargs) -> list[dict]:
        params = dict(
            duration_s=duration,
            tick_s=0.2,
            seed=42,
            rate_per_min=12,
            grip_fail_probability=0.0,
            fault_probability=0.0,
            estop_probability=0.0,
        )
        params.update(kwargs)
        cfg = SimulatorConfig(**params)
        sim = PackingArmSimulator(cfg)
        events: list[dict] = []
        sim.run(events.append)
        return events

    def test_emits_valid_event_types_and_states(self):
        events = self._run()
        self.assertGreater(len(events), 5)
        for ev in events:
            self.assertIn(ev["event_type"], EVENT_TYPES)
            self.assertIn(ev["state"], STATES)
            self.assertEqual(ev["robot_id"], "arm-01")
            self.assertEqual(ev["program_version"], "pack-v1.2.0")
            self.assertEqual(ev["firmware_version"], "fw-3.4.1")
            for i in range(1, 6):
                self.assertIn(f"j{i}_deg", ev)
                self.assertIn(f"j{i}_temp_c", ev)
                self.assertIn(f"j{i}_current_a", ev)
            self.assertIn("tcp_x_mm", ev)
            self.assertIn("gripper_state", ev)
            normalize_event(ev)

    def test_happy_path_includes_place(self):
        events = self._run(duration=40.0, rate_per_min=20)
        types = [e["event_type"] for e in events]
        for needed in ("ITEM_PRESENTED", "ITEM_SCANNED", "PICK_START", "PICK_OK", "PLACE_OK"):
            self.assertIn(needed, types, msg=f"missing {needed} in {types[:30]}…")
        placed = [e for e in events if e["event_type"] == "PLACE_OK"]
        self.assertGreater(len(placed), 0)
        self.assertIsNotNone(placed[0]["cycle_time_ms"])
        self.assertGreater(placed[0]["cycle_time_ms"], 0)
        self.assertIn(placed[0]["box_id"], (1, 2, 3))
        self.assertIsNotNone(placed[0]["order_id"])

    def test_box_full_and_swap(self):
        events = self._run(duration=80.0, rate_per_min=30, boxes=2, box_capacity=2)
        types = {e["event_type"] for e in events}
        self.assertIn("BOX_FULL", types)
        self.assertIn("BOX_SWAP", types)

    def test_grip_fails_and_motion_faults_and_estops(self):
        events = self._run(
            duration=60.0,
            rate_per_min=20,
            grip_fail_probability=0.7,
            fault_probability=0.5,
            estop_probability=0.25,
        )
        types = {e["event_type"] for e in events}
        self.assertIn("GRIP_FAIL", types)
        self.assertIn("FAULT", types)
        self.assertIn("FAULT_CLEARED", types)
        self.assertIn("ESTOP", types)
        self.assertIn("ESTOP_RESET", types)
        faults = [e for e in events if e["event_type"] == "FAULT"]
        self.assertTrue(any(e["fault_code"] in ("GRIP_FAIL", "COLLISION", "AXIS_LIMIT", "JOINT_OVERCURRENT") for e in faults))

    def test_versions_are_stamped(self):
        events = self._run(
            duration=20.0,
            rate_per_min=20,
            program_version="pack-v0.9-beta",
            firmware_version="fw-9.9.9",
        )
        self.assertTrue(events)
        for ev in events:
            self.assertEqual(ev["program_version"], "pack-v0.9-beta")
            self.assertEqual(ev["firmware_version"], "fw-9.9.9")

    def test_forward_kinematics_home_is_finite(self):
        x, y, z, rx, ry, rz = forward_kinematics((0.0, 30.0, -30.0, 0.0, 0.0))
        for v in (x, y, z, rx, ry, rz):
            self.assertTrue(v == v)  # not NaN
        self.assertGreater(z, 0)

    def test_encode_record_is_newline_delimited_json(self):
        ev = self._run(duration=12.0, rate_per_min=30)[0]
        raw = encode_record(ev)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(json.loads(raw.decode()), ev)

    def test_cli_dry_run(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "simulator" / "simulate.py"),
                "--dry-run",
                "--duration",
                "12",
                "--rate",
                "20",
                "--grip-fail-probability",
                "0",
                "--fault-probability",
                "0",
                "--estop-probability",
                "0",
                "--seed",
                "1",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertGreater(len(lines), 2)
        for ln in lines:
            ev = json.loads(ln)
            self.assertIn(ev["event_type"], EVENT_TYPES)
            self.assertIn("j1_deg", ev)


if __name__ == "__main__":
    unittest.main()
