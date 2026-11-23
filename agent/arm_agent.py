#!/usr/bin/env python3
"""Hook to replace the simulator with a real 5-axis packing arm.

The AWS pipeline (Firehose → S3 → Glue → Athena/Redshift, Lambda, SNS) is
built around the event contract in shared/event.schema.json. This module
gives students a publisher interface plus two concrete backends:

  * FirehosePublisher  — same path the simulator uses (good for a laptop
    tethered to the arm's controller).
  * IoTCorePublisher   — publishes to an AWS IoT Core MQTT topic; an IoT
    topic rule can forward the messages to the same Firehose stream.

Implement RobotArmSource for your platform (serial, ROS 2, OPC UA,
Modbus, a vendor SDK, etc.) and call normalize_event() so every record
has the required fields. See agent/README.md and Lab 5 in the student
guide.

Examples:
  python arm_agent.py --dry-run
  python arm_agent.py --backend firehose --stream-name dtlab-telemetry --region us-east-1
  python arm_agent.py --backend iot --iot-topic dtlab/arms/arm-01/events --region us-east-1
"""

from __future__ import annotations

import argparse
import json
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Iterable, Iterator, List, Optional

REQUIRED_FIELDS = (
    "timestamp",
    "event_type",
    "robot_id",
    "cell_id",
    "state",
    "program_version",
    "firmware_version",
    "gripper_state",
    "j1_deg",
    "j2_deg",
    "j3_deg",
    "j4_deg",
    "j5_deg",
    "tcp_x_mm",
    "tcp_y_mm",
    "tcp_z_mm",
)

VALID_STATES = ("IDLE", "HOMING", "PICKING", "MOVING", "PLACING", "FAULT", "ESTOP", "RECOVERING")

VALID_EVENT_TYPES = (
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

VALID_GRIPPER = ("OPEN", "CLOSED", "GRIPPING")
VALID_FAULT_CODES = ("GRIP_FAIL", "COLLISION", "AXIS_LIMIT", "JOINT_OVERCURRENT")
PUT_RECORD_BATCH_MAX = 500


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def normalize_event(raw: dict, *, defaults: Optional[dict] = None) -> dict:
    """Fill aliases and required fields; raise ValueError if the contract is broken.

    This is the Digital Thread 'data contract' check at the edge: design data
    (the JSON Schema) constrains what the device is allowed to publish.
    """
    ev = dict(defaults or {})
    ev.update({k: v for k, v in raw.items() if v is not None or k in raw})
    # If a key was explicitly None, keep defaults for optional identity fields only.
    ev.setdefault("timestamp", utc_now_iso())
    ev.setdefault("item_id", None)
    ev.setdefault("barcode", None)
    ev.setdefault("box_id", None)
    ev.setdefault("order_id", None)
    ev.setdefault("fault_code", None)
    ev.setdefault("fault_axis", None)
    ev.setdefault("cycle_time_ms", None)
    ev.setdefault("gripper_state", "OPEN")
    for i in range(1, 6):
        ev.setdefault(f"j{i}_deg", 0.0)
        ev.setdefault(f"j{i}_current_a", 0.0)
        ev.setdefault(f"j{i}_temp_c", 0.0)
    ev.setdefault("tcp_x_mm", 0.0)
    ev.setdefault("tcp_y_mm", 0.0)
    ev.setdefault("tcp_z_mm", 0.0)
    ev.setdefault("tcp_rx_deg", 0.0)
    ev.setdefault("tcp_ry_deg", 0.0)
    ev.setdefault("tcp_rz_deg", 0.0)

    missing = [f for f in REQUIRED_FIELDS if f not in ev or ev[f] is None]
    if missing:
        raise ValueError(f"event missing required field(s): {', '.join(missing)}")
    if ev["state"] not in VALID_STATES:
        raise ValueError(f"invalid state {ev['state']!r}; expected one of {VALID_STATES}")
    if ev["event_type"] not in VALID_EVENT_TYPES:
        raise ValueError(f"invalid event_type {ev['event_type']!r}; expected one of {VALID_EVENT_TYPES}")
    if ev["gripper_state"] not in VALID_GRIPPER:
        raise ValueError(f"invalid gripper_state {ev['gripper_state']!r}")
    if ev.get("fault_code") not in (None, *VALID_FAULT_CODES):
        raise ValueError(f"invalid fault_code {ev['fault_code']!r}")
    return ev


class EventPublisher(ABC):
    """Students can add a new backend (HTTP, Kafka, …) without changing the arm source."""

    @abstractmethod
    def publish(self, event: dict) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.flush()


class StdoutPublisher(EventPublisher):
    def publish(self, event: dict) -> None:
        sys.stdout.write(json.dumps(event) + "\n")


class FirehosePublisher(EventPublisher):
    """Direct put to the lab Firehose stream (same path as the simulator)."""

    def __init__(self, stream_name: str, region: Optional[str] = None, batch_size: int = 25) -> None:
        import boto3

        self.stream_name = stream_name
        self.batch_size = max(1, min(batch_size, PUT_RECORD_BATCH_MAX))
        self.client = boto3.client("firehose", region_name=region)
        self._buf: List[dict] = []

    def publish(self, event: dict) -> None:
        self._buf.append(event)
        if len(self._buf) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return
        records = [{"Data": (json.dumps(e, separators=(",", ":")) + "\n").encode("utf-8")} for e in self._buf]
        resp = self.client.put_record_batch(DeliveryStreamName=self.stream_name, Records=records)
        failed = resp.get("FailedPutCount", 0)
        if failed:
            sys.stderr.write(f"warning: Firehose rejected {failed} record(s)\n")
        self._buf.clear()


class IoTCorePublisher(EventPublisher):
    """Publish each event to an AWS IoT Core topic via the data-plane HTTP API.

    For an arm that should use device certificates instead of IAM user keys,
    use an MQTT client (for example paho-mqtt) with the IoT Core endpoint and
    the thing's cert/key — then keep the same event schema. See agent/README.md.
    """

    def __init__(self, topic: str, region: Optional[str] = None) -> None:
        import boto3

        self.topic = topic
        self.client = boto3.client("iot-data", region_name=region)

    def publish(self, event: dict) -> None:
        self.client.publish(topic=self.topic, qos=1, payload=json.dumps(event, separators=(",", ":")))


class RobotArmSource(ABC):
    """Implement poll() for your hardware. Yield raw dicts; the agent normalizes them.

    Typical adapters students write in Lab 5:

    * Serial / USB CDC to a vendor teach pendant or motion controller
    * ROS 2 joint_states + a packing-cell action interface
    * OPC UA to a soft-PLC or robot controller
    * Modbus TCP holding registers for joints, gripper, and fault words
    """

    @abstractmethod
    def poll(self) -> Iterator[dict]:
        raise NotImplementedError


class ManualDemoSource(RobotArmSource):
    """A tiny stand-in used by --dry-run so students can test the agent without an arm."""

    def __init__(
        self,
        robot_id: str = "arm-01",
        cell_id: str = "lab-cell-01",
        program_version: str = "pack-v1.2.0",
        firmware_version: str = "fw-3.4.1",
    ) -> None:
        self.robot_id = robot_id
        self.cell_id = cell_id
        self.program_version = program_version
        self.firmware_version = firmware_version

    def poll(self) -> Iterator[dict]:
        base = {
            "robot_id": self.robot_id,
            "cell_id": self.cell_id,
            "item_id": "I-DEMO01",
            "barcode": "8800000DEMO",
            "box_id": 2,
            "order_id": "ORD-DEMO",
            "program_version": self.program_version,
            "firmware_version": self.firmware_version,
            "gripper_state": "OPEN",
            "j1_deg": 0.0,
            "j2_deg": 50.0,
            "j3_deg": -60.0,
            "j4_deg": 18.0,
            "j5_deg": 0.0,
            "tcp_x_mm": 200.0,
            "tcp_y_mm": 0.0,
            "tcp_z_mm": 80.0,
        }
        yield {**base, "event_type": "ITEM_PRESENTED", "state": "IDLE"}
        yield {**base, "event_type": "ITEM_SCANNED", "state": "IDLE"}
        yield {**base, "event_type": "PICK_START", "state": "PICKING"}
        yield {**base, "event_type": "PICK_OK", "state": "PICKING", "gripper_state": "GRIPPING"}
        yield {**base, "event_type": "TELEMETRY", "state": "MOVING", "gripper_state": "GRIPPING", "j1_deg": 5.0, "tcp_x_mm": 210.0}
        yield {**base, "event_type": "PLACE_OK", "state": "PLACING", "gripper_state": "CLOSED", "cycle_time_ms": 8200, "tcp_z_mm": 40.0}


def run_agent(source: RobotArmSource, publisher: EventPublisher, *, defaults: Optional[dict] = None) -> int:
    count = 0
    try:
        for raw in source.poll():
            ev = normalize_event(raw, defaults=defaults)
            publisher.publish(ev)
            count += 1
    finally:
        publisher.close()
    return count


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Publish packing-arm events into the Digital Thread lab pipeline.")
    p.add_argument("--dry-run", action="store_true", help="Print normalized events to stdout.")
    p.add_argument("--backend", choices=("stdout", "firehose", "iot"), default="stdout")
    p.add_argument("--stream-name", default=None, help="Firehose stream (backend=firehose).")
    p.add_argument("--iot-topic", default="dtlab/arms/arm-01/events", help="IoT Core topic (backend=iot).")
    p.add_argument("--region", default=None)
    p.add_argument("--robot-id", default="arm-01")
    p.add_argument("--cell-id", default="lab-cell-01")
    p.add_argument("--program-version", default="pack-v1.2.0")
    p.add_argument("--firmware-version", default="fw-3.4.1")
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    backend = "stdout" if args.dry_run else args.backend
    if backend == "stdout":
        publisher: EventPublisher = StdoutPublisher()
    elif backend == "firehose":
        if not args.stream_name:
            sys.stderr.write("error: --stream-name is required with --backend firehose\n")
            return 2
        publisher = FirehosePublisher(args.stream_name, region=args.region)
    else:
        publisher = IoTCorePublisher(args.iot_topic, region=args.region)

    source = ManualDemoSource(
        robot_id=args.robot_id,
        cell_id=args.cell_id,
        program_version=args.program_version,
        firmware_version=args.firmware_version,
    )
    # Students: replace ManualDemoSource with your RobotArmSource subclass.
    count = run_agent(source, publisher)
    sys.stderr.write(f"published {count} events via {backend}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
