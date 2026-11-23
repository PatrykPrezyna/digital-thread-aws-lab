"""Lambda: scan new Firehose objects on S3 for packing-arm faults and email via SNS.

Triggered by S3 ObjectCreated on the telemetry prefix. The function is
intentionally dependency-free (boto3 is provided by the Lambda runtime).

Alerts fire when a file contains:

  * any FAULT or ESTOP event, or
  * a GRIP_FAIL burst (default: 3 or more GRIP_FAIL events in the same object).

Environment:
  SNS_TOPIC_ARN        Required. Topic that fans out to the student/instructor email.
  FAULT_TYPES          Optional comma-separated list (default: FAULT,ESTOP,GRIP_FAIL).
  GRIP_FAIL_BURST      Optional integer threshold (default: 3). Isolated grip misses
                       below this count do not publish an email, unless FAULT/ESTOP
                       is also present.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Dict, Iterable, List, Sequence

try:
    import boto3
except ImportError:  # unit tests can run without AWS deps
    boto3 = None  # type: ignore[assignment]

DEFAULT_FAULT_TYPES = ("FAULT", "ESTOP", "GRIP_FAIL")
DEFAULT_GRIP_FAIL_BURST = 3


def parse_records(body: bytes) -> List[dict]:
    """Parse Firehose output: newline-delimited JSON or concatenated JSON objects.

    Firehose DirectPut writes each record's bytes back-to-back. This lab's
    simulator appends a newline to every record (NDJSON), but a real arm
    agent might not, so both formats are supported.
    """
    text = body.decode("utf-8").strip()
    if not text:
        return []

    events: List[dict] = []
    if "\n" in text:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
        return events

    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        obj, end = decoder.raw_decode(text, idx)
        events.append(obj)
        idx = end
    return events


def fault_types_from_env() -> Sequence[str]:
    raw = os.environ.get("FAULT_TYPES", "")
    if not raw.strip():
        return DEFAULT_FAULT_TYPES
    return tuple(part.strip().upper() for part in raw.split(",") if part.strip())


def grip_fail_burst_from_env() -> int:
    raw = os.environ.get("GRIP_FAIL_BURST", "")
    if not raw.strip():
        return DEFAULT_GRIP_FAIL_BURST
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_GRIP_FAIL_BURST


def select_alert_events(events: Iterable[dict], fault_types: Sequence[str], grip_fail_burst: int) -> List[dict]:
    """Return the events that should be mentioned in an SNS message.

    FAULT and ESTOP always qualify (if configured). GRIP_FAIL events qualify
    only when their count in this object reaches the burst threshold, so a
    single missed pick does not page the lab.
    """
    wanted = {name.upper() for name in fault_types}
    evs = list(events)
    grip = [ev for ev in evs if str(ev.get("event_type", "")).upper() == "GRIP_FAIL"]
    include_grip = "GRIP_FAIL" in wanted and len(grip) >= grip_fail_burst
    selected: List[dict] = []
    for ev in evs:
        et = str(ev.get("event_type", "")).upper()
        if et in wanted and et != "GRIP_FAIL":
            selected.append(ev)
        elif et == "GRIP_FAIL" and include_grip:
            selected.append(ev)
        # A FAULT whose code is GRIP_FAIL is already included via event_type=FAULT.
    return selected


def format_summary(faults: List[dict], source: str) -> str:
    by_type: Dict[str, List[dict]] = {}
    for ev in faults:
        by_type.setdefault(str(ev.get("event_type", "UNKNOWN")).upper(), []).append(ev)

    robots = sorted({str(ev.get("robot_id") or "?") for ev in faults})
    programs = sorted({str(ev.get("program_version") or "?") for ev in faults})
    firmwares = sorted({str(ev.get("firmware_version") or "?") for ev in faults})
    lines = [
        "Digital Thread lab — packing-arm fault alert",
        f"Source: {source}",
        f"Robots: {', '.join(robots)}",
        f"Program version(s): {', '.join(programs)}",
        f"Firmware version(s): {', '.join(firmwares)}",
        f"Alert events: {len(faults)}",
        "",
    ]
    for ftype in sorted(by_type):
        group = by_type[ftype]
        lines.append(f"{ftype}: {len(group)} event(s)")
        for ev in group[:20]:
            item = ev.get("item_id") or "-"
            box = ev.get("box_id") if ev.get("box_id") is not None else "-"
            ts = ev.get("timestamp") or "-"
            state = ev.get("state") or "-"
            code = ev.get("fault_code") or "-"
            axis = ev.get("fault_axis") if ev.get("fault_axis") is not None else "-"
            lines.append(
                f"  - {ts}  robot={ev.get('robot_id', '-')}  box={box}  "
                f"item={item}  state={state}  fault_code={code}  axis={axis}"
            )
        if len(group) > 20:
            lines.append(f"  - … {len(group) - 20} more")
        lines.append("")
    lines.append(
        "This alert is observational only. A hardware E-stop and a guarded "
        "workspace on a real packing arm must never depend on this cloud path."
    )
    return "\n".join(lines)


def _object_source(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def handle_object(
    body: bytes,
    source: str,
    sns_client,
    topic_arn: str,
    fault_types: Sequence[str],
    grip_fail_burst: int = DEFAULT_GRIP_FAIL_BURST,
) -> dict:
    """Pure-ish core used by the Lambda handler and by unit tests."""
    events = parse_records(body)
    faults = select_alert_events(events, fault_types, grip_fail_burst)
    result = {
        "source": source,
        "events": len(events),
        "faults": len(faults),
        "published": False,
        "grip_fails": sum(1 for e in events if str(e.get("event_type", "")).upper() == "GRIP_FAIL"),
    }
    if not faults:
        return result
    subject = f"[dtlab] {len(faults)} fault event(s) from {source.rsplit('/', 1)[-1]}"
    sns_client.publish(TopicArn=topic_arn, Subject=subject[:100], Message=format_summary(faults, source))
    result["published"] = True
    return result


def handler(event, context):  # noqa: ARG001 — Lambda signature
    topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if not topic_arn:
        raise RuntimeError("SNS_TOPIC_ARN is not set")
    if boto3 is None:
        raise RuntimeError("boto3 is not available in this runtime")

    s3 = boto3.client("s3")
    sns = boto3.client("sns")
    fault_types = fault_types_from_env()
    burst = grip_fail_burst_from_env()
    results = []

    for record in event.get("Records", []):
        if record.get("eventSource") != "aws:s3":
            continue
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        if key.startswith("errors/") or "/errors/" in key:
            continue
        obj = s3.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()
        results.append(handle_object(body, _object_source(bucket, key), sns, topic_arn, fault_types, burst))

    return {"ok": True, "results": results}
