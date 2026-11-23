# Event contract (design data)

This folder is the start of the **digital thread** for the lab: a shared
description of what a 5-axis packing-arm event looks like, written down
before any device or cloud resource exists.

The thread you are building is:

**design / requirements (this schema, including program and firmware
version fields) → robot program / config running on the arm → each pick
event → cloud catalog → analytics and alerts.**

If you change a field name or add a required property, update the schema
first, then the producers, then the SQL queries and Lambda. That sequence
is itself a digital-thread exercise (Lab 1).

| Artifact | Role |
|---|---|
| `event.schema.json` | Machine-readable JSON Schema (draft 2020-12). |
| Simulator (`simulator/simulate.py`) | First producer. |
| Arm agent (`agent/arm_agent.py`) | Producer used when a real packing arm replaces the simulator. |
| Glue Data Catalog + Athena/Redshift | Consumers for analytics. |
| Fault-alert Lambda | Consumer that closes the loop on `FAULT`, `ESTOP`, and `GRIP_FAIL` bursts. |

## The cell

A desktop 5-axis robot arm picks items from an infeed conveyor or tray,
scans them, and places them into 2 or 3 packing boxes / cartons by
order. The joints are:

| Joint | Name |
|---|---|
| j1 | Base |
| j2 | Shoulder |
| j3 | Elbow |
| j4 | Wrist pitch |
| j5 | Wrist roll |

A simplified teaching forward-kinematics model in the simulator turns
those angles into a tool-center-point (TCP) pose. It is not a particular
vendor's DH table; Lab 5 is where you replace it with the real arm's
controller feedback.

## States

`HOMING` → `IDLE` → (item presented, scanned) → `PICKING` → `MOVING` →
`PLACING` → `MOVING` (home or next pick) → `IDLE`

Faults can interrupt the happy path:

- `GRIP_FAIL` (event, often a retry in `PICKING`). Repeated misses raise
  a `FAULT` with `fault_code=GRIP_FAIL`.
- `FAULT` with `COLLISION`, `AXIS_LIMIT`, or `JOINT_OVERCURRENT` →
  `FAULT_CLEARED` → `RECOVERING` → usually `HOMING`, then resume.
- `ESTOP` → `ESTOP_RESET` → `RECOVERING` → `HOMING`.

## Event types

| event_type | Typical state | Notes |
|---|---|---|
| `ITEM_PRESENTED` | `IDLE` / `PICKING` | An item is at the infeed or tray. |
| `ITEM_SCANNED` | `IDLE` / `PICKING` | Barcode is known; `box_id` / `order_id` may be assigned. |
| `PICK_START` | `PICKING` | Approach and close the gripper. |
| `PICK_OK` | `PICKING` | Grip confirmed. |
| `GRIP_FAIL` | `PICKING` or `FAULT` | Missed or dropped grip. |
| `PLACE_OK` | `PLACING` | Item in the box. Includes `cycle_time_ms`. |
| `BOX_FULL` | `PLACING` / `IDLE` | The carton has reached capacity. |
| `BOX_SWAP` | `IDLE` | A new carton is in the fixture. |
| `TELEMETRY` | `MOVING` (also other motion) | Periodic joints, currents, temperatures, TCP, gripper. |
| `FAULT` | `FAULT` | See `fault_code` and `fault_axis`. |
| `FAULT_CLEARED` | `FAULT` or `RECOVERING` | Controller accepted a reset. |
| `ESTOP` | `ESTOP` | Emergency stop. Observational in the cloud. |
| `ESTOP_RESET` | `ESTOP` or `RECOVERING` | Reset issued. |

Every event is stamped with `program_version` and `firmware_version` so
you can later ask “did the new packing program raise the grip-fail
rate?” — that query *is* the digital thread.

## Example

```json
{
  "timestamp": "2026-11-22T10:15:03.123Z",
  "event_type": "PLACE_OK",
  "robot_id": "arm-01",
  "cell_id": "lab-cell-01",
  "item_id": "I-000123",
  "barcode": "88000000123",
  "box_id": 2,
  "order_id": "ORD-00045",
  "state": "PLACING",
  "fault_code": null,
  "fault_axis": null,
  "cycle_time_ms": 8450,
  "program_version": "pack-v1.2.0",
  "firmware_version": "fw-3.4.1",
  "gripper_state": "CLOSED",
  "j1_deg": 0.2,
  "j2_deg": 48.1,
  "j3_deg": -58.0,
  "j4_deg": 18.0,
  "j5_deg": 0.0,
  "j1_current_a": 0.21,
  "j2_current_a": 0.64,
  "j3_current_a": 0.40,
  "j4_current_a": 0.11,
  "j5_current_a": 0.05,
  "j1_temp_c": 34.2,
  "j2_temp_c": 37.8,
  "j3_temp_c": 36.1,
  "j4_temp_c": 32.0,
  "j5_temp_c": 31.4,
  "tcp_x_mm": 210.0,
  "tcp_y_mm": 5.0,
  "tcp_z_mm": 40.0,
  "tcp_rx_deg": 180.0,
  "tcp_ry_deg": 0.0,
  "tcp_rz_deg": 0.0
}
```
