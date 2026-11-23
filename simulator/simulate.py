#!/usr/bin/env python3
"""Simulate a 5-axis pick-and-place packing arm and stream telemetry events.

The arm starts by homing, then picks items from an infeed / tray, scans
them, and places them into 2–3 packing boxes by order. Along the way it
may miss a grip, hit a collision, overtravel an axis, over-current a
joint, or take an emergency stop. Events use the shared contract in
shared/event.schema.json so a real packing arm can replace this
simulator without changing the AWS pipeline.

Joint names (j1..j5): base, shoulder, elbow, wrist pitch, wrist roll.

Examples:
  python simulate.py --dry-run --duration 20
  python simulate.py --stream-name dtlab-telemetry --region us-east-1 --duration 180
  python simulate.py --dry-run --program-version pack-v0.9-beta --fault-probability 0.25
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Schema constants (keep in sync with shared/event.schema.json)
# ---------------------------------------------------------------------------

STATES = ("IDLE", "HOMING", "PICKING", "MOVING", "PLACING", "FAULT", "ESTOP", "RECOVERING")

EVENT_TYPES = (
    "ITEM_PRESENTED",
    "ITEM_SCANNED",
    "PICK_START",
    "PICK_OK",
    "GRIP_FAIL",
    "PLACE_OK",
    "BOX_FULL",
    "BOX_SWAP",
    "TELEMETRY",
    "FAULT",
    "FAULT_CLEARED",
    "ESTOP",
    "ESTOP_RESET",
)

FAULT_CODES = ("GRIP_FAIL", "COLLISION", "AXIS_LIMIT", "JOINT_OVERCURRENT")
GRIPPER_STATES = ("OPEN", "CLOSED", "GRIPPING")
JOINT_NAMES = ("base", "shoulder", "elbow", "wrist_pitch", "wrist_roll")

# Teaching-model poses in degrees (j1..j5). Not a vendor DH table.
HOME_DEG: Tuple[float, ...] = (0.0, 30.0, -30.0, 0.0, 0.0)
PICK_DEG: Tuple[float, ...] = (25.0, 62.0, -78.0, 20.0, 0.0)
BOX_DEG: Dict[int, Tuple[float, ...]] = {
    1: (-40.0, 52.0, -62.0, 22.0, 0.0),
    2: (0.0, 50.0, -60.0, 18.0, 0.0),
    3: (40.0, 52.0, -62.0, 22.0, 0.0),
}

# Simplified 5-axis link lengths (mm) for the teaching forward-kinematics model.
L_BASE_MM = 80.0
L_UPPER_MM = 180.0
L_FORE_MM = 150.0
L_WRIST_MM = 50.0

SLEW_DEG_PER_S = 70.0
ARRIVAL_TOLERANCE_DEG = 1.5
AMBIENT_TEMP_C = 28.0
DEFAULT_BATCH_SIZE = 50
PUT_RECORD_BATCH_MAX = 500
RETRY_ATTEMPTS = 3


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_barcode(item_id: str) -> str:
    suffix = item_id.replace("I-", "")
    return f"880{suffix.zfill(10)}"


def lerp_joints(current: Sequence[float], target: Sequence[float], slew: float, dt: float) -> List[float]:
    out = []
    max_step = slew * dt
    for c, t in zip(current, target):
        delta = t - c
        if abs(delta) <= max_step:
            out.append(t)
        else:
            out.append(c + math.copysign(max_step, delta))
    return out


def joints_arrived(current: Sequence[float], target: Sequence[float], tol: float = ARRIVAL_TOLERANCE_DEG) -> bool:
    return all(abs(c - t) <= tol for c, t in zip(current, target))


def forward_kinematics(joints_deg: Sequence[float]) -> Tuple[float, float, float, float, float, float]:
    """Simplified teaching FK: planar 3-link arm on a rotating base.

    j1 rotates the arm about Z. j2 (shoulder), j3 (elbow) and j4 (wrist
    pitch) live in the vertical plane. j5 (wrist roll) is tool rotation.
    This is good enough for plots and alerts; it is not a real robot's
    calibration. Replace it with controller feedback on a real arm.
    """
    j1, j2, j3, j4, j5 = (math.radians(v) for v in joints_deg)
    # Angles from horizontal in the arm plane.
    a2 = j2
    a3 = j2 + j3
    a4 = j2 + j3 + j4
    r = L_UPPER_MM * math.cos(a2) + L_FORE_MM * math.cos(a3) + L_WRIST_MM * math.cos(a4)
    z = L_BASE_MM + L_UPPER_MM * math.sin(a2) + L_FORE_MM * math.sin(a3) + L_WRIST_MM * math.sin(a4)
    x = r * math.cos(j1)
    y = r * math.sin(j1)
    # Tool orientation, degrees: roll / pitch / yaw in a simple ZYX convention.
    rx = math.degrees(a4)
    ry = 0.0
    rz = math.degrees(j1 + j5)
    return (x, y, z, rx, ry, rz)


@dataclass
class SimulatorConfig:
    robot_id: str = "arm-01"
    cell_id: str = "lab-cell-01"
    boxes: int = 3
    box_capacity: int = 4
    rate_per_min: float = 6.0
    duration_s: float = 60.0
    grip_fail_probability: float = 0.10
    fault_probability: float = 0.08
    estop_probability: float = 0.03
    telemetry_hz: float = 2.0
    tick_s: float = 0.2
    seed: Optional[int] = None
    program_version: str = "pack-v1.2.0"
    firmware_version: str = "fw-3.4.1"
    box_poses: Dict[int, Tuple[float, ...]] = field(default_factory=lambda: dict(BOX_DEG))


class PackingArmSimulator:
    """Tick-based state machine for one 5-axis pick-and-place packing arm."""

    def __init__(self, config: SimulatorConfig, rng: Optional[random.Random] = None) -> None:
        if config.boxes < 1:
            raise ValueError("boxes must be >= 1")
        if config.box_capacity < 1:
            raise ValueError("box_capacity must be >= 1")
        self.cfg = config
        self.rng = rng or random.Random(config.seed)
        self.state = "HOMING"
        self.joints = list(HOME_DEG)
        self.target = list(HOME_DEG)
        self.temps = [AMBIENT_TEMP_C + 2.0, AMBIENT_TEMP_C + 3.0, AMBIENT_TEMP_C + 2.5, AMBIENT_TEMP_C + 1.5, AMBIENT_TEMP_C + 1.2]
        self.currents = [0.05, 0.06, 0.05, 0.03, 0.02]
        self.gripper_state = "OPEN"
        self.item_id: Optional[str] = None
        self.barcode: Optional[str] = None
        self.box_id: Optional[int] = None
        self.order_id: Optional[str] = None
        self.fault_code: Optional[str] = None
        self.fault_axis: Optional[int] = None
        self.cycle_started_at: Optional[float] = None
        self.resume_state = "IDLE"
        self.resume_target = list(HOME_DEG)
        self.hold_until = 0.0
        self._item_seq = 0
        self._order_seq = 1
        self._sim_t = 0.0
        self._next_telemetry_t = 0.0
        self._last_cycle_t = -1e9
        self._box_counts: Dict[int, int] = {i: 0 for i in range(1, config.boxes + 1)}
        self._grip_attempts = 0
        self._homed = False
        for i in range(1, config.boxes + 1):
            if i not in self.cfg.box_poses:
                # Spread extra boxes in a fan around the home yaw.
                yaw = -40.0 + (80.0 * (i - 1) / max(config.boxes - 1, 1))
                self.cfg.box_poses[i] = (yaw, 52.0, -62.0, 22.0, 0.0)

    def _tcp(self) -> Tuple[float, float, float, float, float, float]:
        return forward_kinematics(self.joints)

    def _event(self, event_type: str, *, cycle_time_ms: Optional[int] = None, extra: Optional[dict] = None) -> dict:
        tcp = self._tcp()
        ev = {
            "timestamp": utc_now_iso(),
            "event_type": event_type,
            "robot_id": self.cfg.robot_id,
            "cell_id": self.cfg.cell_id,
            "item_id": self.item_id,
            "barcode": self.barcode,
            "box_id": self.box_id,
            "order_id": self.order_id,
            "state": self.state,
            "fault_code": self.fault_code,
            "fault_axis": self.fault_axis,
            "cycle_time_ms": cycle_time_ms,
            "program_version": self.cfg.program_version,
            "firmware_version": self.cfg.firmware_version,
            "gripper_state": self.gripper_state,
            "j1_deg": round(self.joints[0], 3),
            "j2_deg": round(self.joints[1], 3),
            "j3_deg": round(self.joints[2], 3),
            "j4_deg": round(self.joints[3], 3),
            "j5_deg": round(self.joints[4], 3),
            "j1_current_a": round(self.currents[0], 3),
            "j2_current_a": round(self.currents[1], 3),
            "j3_current_a": round(self.currents[2], 3),
            "j4_current_a": round(self.currents[3], 3),
            "j5_current_a": round(self.currents[4], 3),
            "j1_temp_c": round(self.temps[0], 2),
            "j2_temp_c": round(self.temps[1], 2),
            "j3_temp_c": round(self.temps[2], 2),
            "j4_temp_c": round(self.temps[3], 2),
            "j5_temp_c": round(self.temps[4], 2),
            "tcp_x_mm": round(tcp[0], 2),
            "tcp_y_mm": round(tcp[1], 2),
            "tcp_z_mm": round(tcp[2], 2),
            "tcp_rx_deg": round(tcp[3], 2),
            "tcp_ry_deg": round(tcp[4], 2),
            "tcp_rz_deg": round(tcp[5], 2),
        }
        if extra:
            ev.update(extra)
        return ev

    def _new_item(self) -> None:
        self._item_seq += 1
        self.item_id = f"I-{self._item_seq:06d}"
        self.barcode = make_barcode(self.item_id)
        self.cycle_started_at = self._sim_t
        # Round-robin boxes map onto rotating lab orders.
        self.box_id = ((self._item_seq - 1) % self.cfg.boxes) + 1
        if self._item_seq % self.cfg.box_capacity == 1:
            self.order_id = f"ORD-{self._order_seq:05d}"
            self._order_seq += 1
        self._grip_attempts = 0
        self.fault_code = None
        self.fault_axis = None

    def _clear_item(self) -> None:
        self.item_id = None
        self.barcode = None
        self.cycle_started_at = None
        self._grip_attempts = 0

    def _cycle_ms(self) -> int:
        if self.cycle_started_at is None:
            return 0
        return int((self._sim_t - self.cycle_started_at) * 1000)

    def _set_state(self, new_state: str) -> None:
        if new_state not in STATES:
            raise ValueError(f"invalid state: {new_state}")
        self.state = new_state

    def _update_physics(self, dt: float, moving: bool) -> None:
        if moving:
            self.joints = lerp_joints(self.joints, self.target, SLEW_DEG_PER_S, dt)
            # Current scales with remaining joint error (a crude torque proxy).
            for i, (c, t) in enumerate(zip(self.joints, self.target)):
                self.currents[i] = 0.08 + min(abs(t - c) / 40.0, 1.0) * (0.9 if i < 3 else 0.35)
                self.temps[i] += (0.15 + self.currents[i] * 0.2) * dt
        else:
            for i in range(5):
                self.currents[i] = 0.04 + 0.01 * i
                # Slow cool-down toward ambient.
                self.temps[i] += (AMBIENT_TEMP_C - self.temps[i]) * 0.02 * dt
        # Clamp to a plausible lab range so queries stay readable.
        self.temps = [min(max(t, AMBIENT_TEMP_C), 75.0) for t in self.temps]

    def _maybe_telemetry(self, events: List[dict]) -> None:
        if self._sim_t >= self._next_telemetry_t:
            events.append(self._event("TELEMETRY"))
            self._next_telemetry_t = self._sim_t + (1.0 / max(self.cfg.telemetry_hz, 0.1))

    def _enter_fault(self, code: str, axis: Optional[int] = None) -> dict:
        self.fault_code = code
        self.fault_axis = axis
        self.resume_state = "HOMING"
        self.resume_target = list(HOME_DEG)
        self.gripper_state = "OPEN" if code != "GRIP_FAIL" else self.gripper_state
        self._set_state("FAULT")
        self.hold_until = self._sim_t + self.rng.uniform(1.2, 2.5)
        return self._event("FAULT")

    def _enter_estop(self) -> dict:
        self.resume_state = "HOMING"
        self.resume_target = list(HOME_DEG)
        self.fault_code = None
        self.fault_axis = None
        self._set_state("ESTOP")
        self.hold_until = self._sim_t + self.rng.uniform(1.5, 3.0)
        return self._event("ESTOP")

    def _per_tick_prob(self, per_cycle: float, dt: float, typical_s: float = 5.0) -> float:
        p = 1.0 - (1.0 - min(max(per_cycle, 0.0), 0.95)) ** (dt / typical_s)
        return min(p, 0.5)

    def tick(self, dt: float) -> List[dict]:
        """Advance the arm by dt seconds and return zero or more events."""
        events: List[dict] = []
        self._sim_t += dt
        t = self._sim_t

        if self.state == "ESTOP":
            self._update_physics(dt, moving=False)
            if t >= self.hold_until:
                events.append(self._event("ESTOP_RESET"))
                self._set_state("RECOVERING")
                self.hold_until = t + self.rng.uniform(0.6, 1.2)
            return events

        if self.state == "FAULT":
            self._update_physics(dt, moving=False)
            if t >= self.hold_until:
                events.append(self._event("FAULT_CLEARED"))
                self.fault_code = None
                self.fault_axis = None
                self._set_state("RECOVERING")
                self.hold_until = t + self.rng.uniform(0.4, 1.0)
            return events

        if self.state == "RECOVERING":
            self._update_physics(dt, moving=False)
            if t >= self.hold_until:
                self.target = list(self.resume_target)
                self._set_state(self.resume_state)
            return events

        # E-stop can interrupt motion and work states (not already faulted).
        if self.state in ("IDLE", "HOMING", "PICKING", "MOVING", "PLACING") and self.rng.random() < self._per_tick_prob(
            self.cfg.estop_probability, dt, typical_s=8.0
        ):
            self._update_physics(dt, moving=False)
            events.append(self._enter_estop())
            return events

        if self.state == "HOMING":
            self.target = list(HOME_DEG)
            self.gripper_state = "OPEN"
            self._update_physics(dt, moving=True)
            self._maybe_telemetry(events)
            if joints_arrived(self.joints, self.target):
                self.joints = list(HOME_DEG)
                self._homed = True
                self._set_state("IDLE")
            return events

        if self.state == "IDLE":
            self._update_physics(dt, moving=False)
            interval = 60.0 / max(self.cfg.rate_per_min, 0.1)
            if self._homed and t - self._last_cycle_t >= interval:
                self._new_item()
                events.append(self._event("ITEM_PRESENTED"))
                events.append(self._event("ITEM_SCANNED"))
                self.target = list(PICK_DEG)
                self._set_state("PICKING")
                events.append(self._event("PICK_START"))
                self._last_cycle_t = t
                self._next_telemetry_t = t
            return events

        if self.state == "PICKING":
            self._update_physics(dt, moving=True)
            self._maybe_telemetry(events)
            if not joints_arrived(self.joints, self.target):
                return events
            self.joints = list(self.target)
            # Attempt a grip at the infeed pose.
            self._grip_attempts += 1
            if self.rng.random() < self.cfg.grip_fail_probability:
                self.gripper_state = "OPEN"
                self.fault_code = "GRIP_FAIL"
                events.append(self._event("GRIP_FAIL"))
                self.fault_code = None
                if self._grip_attempts >= 3:
                    events.append(self._enter_fault("GRIP_FAIL"))
                else:
                    # Back off slightly and retry the same pick.
                    self.target = list(PICK_DEG)
                return events
            self.gripper_state = "GRIPPING"
            events.append(self._event("PICK_OK"))
            assert self.box_id is not None
            self.target = list(self.cfg.box_poses[self.box_id])
            self._set_state("MOVING")
            return events

        if self.state == "MOVING":
            # Motion faults: collision, axis limit, or joint overcurrent.
            if self.rng.random() < self._per_tick_prob(self.cfg.fault_probability, dt, typical_s=4.0):
                code = self.rng.choice(("COLLISION", "AXIS_LIMIT", "JOINT_OVERCURRENT"))
                axis = self.rng.randint(1, 5) if code != "COLLISION" else self.rng.choice([None, 1, 2, 3])
                self._update_physics(dt, moving=False)
                events.append(self._enter_fault(code, axis))
                return events
            self._update_physics(dt, moving=True)
            self._maybe_telemetry(events)
            if joints_arrived(self.joints, self.target):
                self.joints = list(self.target)
                if self.item_id is not None and self.target != list(HOME_DEG):
                    self._set_state("PLACING")
                    self.hold_until = t + self.rng.uniform(0.4, 0.8)
                else:
                    self._set_state("IDLE")
            return events

        if self.state == "PLACING":
            self._update_physics(dt, moving=False)
            if t >= self.hold_until:
                events.append(self._event("PLACE_OK", cycle_time_ms=self._cycle_ms()))
                assert self.box_id is not None
                self._box_counts[self.box_id] = self._box_counts.get(self.box_id, 0) + 1
                self.gripper_state = "OPEN"
                if self._box_counts[self.box_id] >= self.cfg.box_capacity:
                    events.append(self._event("BOX_FULL"))
                    self._box_counts[self.box_id] = 0
                    events.append(self._event("BOX_SWAP"))
                self._clear_item()
                self.target = list(HOME_DEG)
                self._set_state("MOVING")
            return events

        return events

    def run(self, emit: Callable[[dict], None]) -> int:
        """Run for config.duration_s, calling emit() for every event. Returns count."""
        count = 0
        end = self.cfg.duration_s
        dt = self.cfg.tick_s
        while self._sim_t < end:
            for ev in self.tick(dt):
                emit(ev)
                count += 1
        return count


def encode_record(event: dict) -> bytes:
    """One Firehose record: a JSON object followed by a newline."""
    return (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")


def put_record_batch(
    client,
    stream_name: str,
    events: List[dict],
    *,
    max_retries: int = RETRY_ATTEMPTS,
) -> int:
    """Send events with put_record_batch, retrying failed records.

    Returns the number of records that were still failing after retries.
    """
    if not events:
        return 0
    pending = [encode_record(e) for e in events]
    leftover = 0
    for attempt in range(max_retries):
        still_failed: List[bytes] = []
        for i in range(0, len(pending), PUT_RECORD_BATCH_MAX):
            chunk = pending[i : i + PUT_RECORD_BATCH_MAX]
            records = [{"Data": data} for data in chunk]
            resp = client.put_record_batch(DeliveryStreamName=stream_name, Records=records)
            for rec, data in zip(resp.get("RequestResponses", []), chunk):
                if rec.get("ErrorCode"):
                    still_failed.append(data)
        if not still_failed:
            return 0
        pending = still_failed
        leftover = len(still_failed)
        time.sleep(0.4 * (2 ** attempt))
    sys.stderr.write(f"warning: {leftover} record(s) failed after {max_retries} attempts\n")
    return leftover


class BatchEmitter:
    def __init__(
        self,
        *,
        dry_run: bool,
        stream_name: Optional[str],
        region: Optional[str],
        batch_size: int,
    ) -> None:
        self.dry_run = dry_run
        self.stream_name = stream_name
        self.batch_size = max(1, min(batch_size, PUT_RECORD_BATCH_MAX))
        self.buffer: List[dict] = []
        self.failed = 0
        self.client = None
        if not dry_run:
            try:
                import boto3
            except ImportError as exc:
                raise SystemExit(
                    "boto3 is required unless you pass --dry-run. "
                    "Install it with: pip install -r simulator/requirements.txt"
                ) from exc
            self.client = boto3.client("firehose", region_name=region)

    def emit(self, event: dict) -> None:
        if self.dry_run:
            sys.stdout.write(json.dumps(event) + "\n")
            return
        self.buffer.append(event)
        if len(self.buffer) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if self.dry_run or not self.buffer:
            return
        self.failed += put_record_batch(self.client, self.stream_name, self.buffer)
        self.buffer.clear()


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Simulate a 5-axis packing arm and stream events to Kinesis Data Firehose.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true", help="Print JSON events to stdout; do not call AWS.")
    p.add_argument("--stream-name", default=None, help="Firehose delivery stream name (required unless --dry-run).")
    p.add_argument("--region", default=None, help="AWS region of the Firehose stream.")
    p.add_argument("--robot-id", default="arm-01")
    p.add_argument("--cell-id", default="lab-cell-01")
    p.add_argument("--boxes", type=int, default=3, help="Number of packing boxes / cartons.")
    p.add_argument("--box-capacity", type=int, default=4, help="Items per box before BOX_FULL / BOX_SWAP.")
    p.add_argument("--rate", type=float, default=6.0, help="Target pick cycles started per minute.")
    p.add_argument("--duration", type=float, default=60.0, help="Simulated duration in seconds.")
    p.add_argument("--grip-fail-probability", type=float, default=0.10, help="Chance a given grip attempt misses.")
    p.add_argument("--fault-probability", type=float, default=0.08, help="Approx. chance of a motion fault per move.")
    p.add_argument("--estop-probability", type=float, default=0.03, help="Approx. chance of an E-stop per cycle.")
    p.add_argument("--telemetry-hz", type=float, default=2.0, help="TELEMETRY events per second while moving.")
    p.add_argument("--tick", type=float, default=0.2, help="Simulator time step in seconds.")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Firehose put_record_batch size.")
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible classroom demos.")
    p.add_argument("--program-version", default="pack-v1.2.0", help="Packing program / config version stamped on events.")
    p.add_argument("--firmware-version", default="fw-3.4.1", help="Controller firmware version stamped on events.")
    p.add_argument(
        "--realtime",
        action="store_true",
        help="Sleep in real time between ticks (watchable dry-run). Default is to run as fast as possible.",
    )
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    if not args.dry_run and not args.stream_name:
        sys.stderr.write("error: --stream-name is required unless you pass --dry-run\n")
        return 2

    cfg = SimulatorConfig(
        robot_id=args.robot_id,
        cell_id=args.cell_id,
        boxes=args.boxes,
        box_capacity=args.box_capacity,
        rate_per_min=args.rate,
        duration_s=args.duration,
        grip_fail_probability=args.grip_fail_probability,
        fault_probability=args.fault_probability,
        estop_probability=args.estop_probability,
        telemetry_hz=args.telemetry_hz,
        tick_s=args.tick,
        seed=args.seed,
        program_version=args.program_version,
        firmware_version=args.firmware_version,
    )
    sim = PackingArmSimulator(cfg)
    emitter = BatchEmitter(
        dry_run=args.dry_run,
        stream_name=args.stream_name,
        region=args.region,
        batch_size=args.batch_size,
    )

    original_tick = sim.tick

    def tick_maybe_sleep(dt: float) -> List[dict]:
        evs = original_tick(dt)
        if args.realtime:
            time.sleep(dt)
        return evs

    sim.tick = tick_maybe_sleep  # type: ignore[method-assign]
    count = sim.run(emitter.emit)
    emitter.flush()
    sys.stderr.write(
        f"emitted {count} events over {cfg.duration_s:.1f}s simulated time"
        + (f" ({emitter.failed} failed uploads)" if emitter.failed else "")
        + "\n"
    )
    return 1 if emitter.failed else 0


if __name__ == "__main__":
    sys.exit(main())
