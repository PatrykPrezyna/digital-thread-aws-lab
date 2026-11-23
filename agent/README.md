# Connecting a real 5-axis packing arm

The AWS pipeline does not care whether events come from
`simulator/simulate.py` or from a physical desktop packing arm. It only
cares that each record matches `shared/event.schema.json`. This folder
is the hook students use in Lab 5 to swap the simulator for the arm.

## What you implement

Subclass `RobotArmSource` in `arm_agent.py` and yield a dict per event
from `poll()`. Call `normalize_event()` (already done for you inside
`run_agent`) so missing optional fields are filled in and illegal
states are rejected at the edge.

Map your controller onto the lab states and event types:

| Arm / hardware signal | Lab `state` | Lab `event_type` |
|---|---|---|
| Homing to a known pose | `HOMING` | `TELEMETRY` (optional) |
| Waiting at home, no item | `IDLE` | — |
| Item arrives at the infeed / tray, barcode read | `IDLE` / `PICKING` | `ITEM_PRESENTED`, then `ITEM_SCANNED` |
| Approach and close the gripper | `PICKING` | `PICK_START`, then `PICK_OK` or `GRIP_FAIL` |
| Joint motion toward the assigned box | `MOVING` | `TELEMETRY` (joints, currents, temps, TCP) |
| Release into the carton | `PLACING` | `PLACE_OK` (include `cycle_time_ms`, `box_id`, `order_id`) |
| Carton reaches capacity / is replaced | `IDLE` | `BOX_FULL`, `BOX_SWAP` |
| Grip miss repeated, collision, axis limit, overcurrent | `FAULT` | `FAULT` / `FAULT_CLEARED` (`fault_code`, `fault_axis`) |
| Hardware emergency stop | `ESTOP` | `ESTOP` / `ESTOP_RESET` |
| Motion resume after a fault | `RECOVERING` then usually `HOMING` | — |

Stamp every event with `program_version` and `firmware_version` from the
controller or the file you deployed. Lab 3 asks you to relate those
versions to fault rate — that is the digital thread from design data
to analytics.

## How events get to Firehose

Two publishers are included:

1. **FirehosePublisher** — the lab PC uses IAM credentials and
   `put_record_batch`, same as the simulator. Easiest path in a lab.
2. **IoTCorePublisher** — uses the AWS IoT data-plane API. To do this
   properly with *device certificates* instead of long-lived IAM keys:
   - Register an IoT Thing for the arm and attach a device cert/policy.
   - Publish MQTT to `dtlab/arms/<robot_id>/events` with paho-mqtt.
   - Create an IoT topic rule that forwards the payload to the lab
     Firehose stream. The least-privilege policy for that rule is
     `iam/iot-rule-trust-policy.json` + `iam/iot-rule-permissions.json`.

`StdoutPublisher` (`--dry-run`) is for checking the schema on the bench
before you touch AWS.

```bash
python arm_agent.py --dry-run
python arm_agent.py --backend firehose --stream-name dtlab-telemetry --region us-east-1
```

Typical controllers students wire in:

- Serial or USB CDC to a vendor teach pendant
- ROS 2 `joint_states` plus a pick-and-place action interface
- OPC UA to a soft-PLC or robot controller
- Modbus TCP holding registers for joints, gripper, and fault words

## Safety (read this before you power the arm)

The cloud path is **observational**. A `FAULT` or `ESTOP` event that
travels Firehose → S3 → Lambda → email is far too slow and far too
unreliable to be a safety function.

- The cell must have a **hardwired emergency stop** that removes motor
  power without any network hop.
- The arm must run inside a **guarded workspace** (fixed enclosure,
  interlocked door, or light curtain) that is also hardwired, not
  supervised from AWS.
- Do not write “the Lambda will stop the arm.”
- Treat grip fails and collisions the same way: the on-board
  controller stops motion; the cloud only records that it happened.

## Suggested student checklist

1. Produce one `PLACE_OK` event from the real arm in `--dry-run` and
   confirm it validates against `shared/event.schema.json`, including
   `program_version` and `firmware_version`.
2. Point the agent at the existing Firehose stream (do not create a
   second pipeline).
3. Re-run the Lab 3 queries (including fault rate by program / firmware
   version) and the Lab 4 fault alert on the new data.
4. In your report, show one item's events side by side: schema field,
   robot program version, device signal, S3 record, Athena row. That
   *is* the digital thread.
