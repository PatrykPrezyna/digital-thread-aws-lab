# Instructor notes — Digital Thread AWS lab

This file is for course staff. Students should use `docs/student-lab-guide.md`
and the root README. Do not publish complete exercise answers to the
student branch if you want to grade the written questions as-is.

## What this module is

A five-session (or one-afternoon plus extension) teaching module that
treats an AWS telemetry pipeline as the cloud half of a **digital
thread**. The physical cell is a desktop 5-axis packing arm that picks
items from an infeed or tray and places them into two or three boxes /
cartons by order. A Python simulator stands in for the arm until Lab 5.

The module is intentionally *not* a factory-floor controls lab. There
is no safety-rated PLC in the critical path. The teaching point is
traceability: schema and requirements → robot program / firmware
version → each pick event → object store → catalog → query / alert,
plus least-privilege IAM and cost hygiene.

## Suggested schedules

**Five-week module** (recommended): one lab per week, 2 hours of contact
plus a short write-up. Lab 5 needs access to a packing arm and can be
replaced by a paper design of the agent if hardware is not available.

**One afternoon workshop** (≈2 hours of AWS time, 3 hours wall-clock):
follow the compressed timeline in the README (schema and dry-run,
setup.sh, simulator, crawler + two queries including the version
comparison, one fault email, teardown). Assign the written exercises as
homework.

**Flipped:** students run Labs 1–2 at home on a tightly budgeted
account; contact time is Lab 3 queries (especially program / firmware
vs. fault rate) and the Lab 4 safety discussion.

## Accounts, IAM, and cost control

- Prefer **one AWS account per student** or per pair, with a hard
  spending limit if your university can set one. The lab also creates
  an AWS Budget at `$MONTHLY_BUDGET_USD` (default $5) with 50 / 80 /
  100 % emails. Creating budgets requires `budgets:*` and a valid
  email; if your classroom IAM role cannot do this, set the alarm
  yourself at the payer account and tell students.
- Required IAM (if you are not handing out AdministratorAccess):
  S3, IAM (role/policy create, but consider scoping it), Firehose,
  Lambda, SNS, Glue, CloudWatch Logs, STS, Budgets, and optionally
  Redshift Serverless and IoT. A lab role that can only mutate
  resources tagged `Purpose=digital-thread-lab` is a good extra
  credit discussion, but the stock `setup.sh` does not assume an SCP.
- **Leave `ENABLE_REDSHIFT=0` unless you have a reason not to.**
  Athena is enough to hit every learning objective in Lab 3.
  Redshift Serverless bills in RPU-seconds with a 60-second minimum
  per query and is the most common way a student overruns a $5
  budget. If you enable it, require teardown the same day and watch
  the workgroup list.
- **Skip EMR.** It is slow to start and expensive at this scale. The
  Glue catalog on S3 is already a reasonable future Spark/EMR
  landing zone; mention that, do not provision it.
- Expected cost for Labs 1–4, torn down the same day, is typically
  well under a dollar (Firehose, S3, a few Lambda invocations, one
  Glue crawler run, Athena bytes scanned in the megabytes). The
  risk is *forgotten* resources, not the happy path.
- End every session with `CONFIRM=yes ./infra/teardown.sh`. Spot-check
  a few accounts 24 hours later for leftover Redshift workgroups,
  Firehose streams, and un-empty versioned buckets.

## Setup gotchas (the ones students will hit)

| Symptom | Likely cause | What to tell them |
|---|---|---|
| `PutRecordBatch` AccessDenied | Simulator policy not attached to their user/role | The ARN is printed at the end of `setup.sh`. |
| Nothing in S3 after the simulator exits | Firehose 60 s / 1 MiB buffer | Wait a full minute, then `aws s3 ls --recursive`. |
| SNS email never arrives | Subscription not confirmed, or university spam filter | Resend confirmation; check junk; use a personal inbox if needed. Isolated `GRIP_FAIL` events also do not alert — they need a burst, or a `FAULT` / `ESTOP`. |
| Lambda not firing | Notification overwritten, or still flushing | `put-bucket-notification-configuration` replaces the whole document. Re-run setup. Inspect `/aws/lambda/dtlab-fault-alert`. |
| Glue crawler fails | Role / empty prefix | Confirm the Firehose role wrote objects under `telemetry/`. Re-run the crawler after files exist. |
| Athena `GENERIC_INTERNAL_ERROR` or empty schema | Crawler ran on an empty prefix, or JSON was concatenated without newlines | The simulator writes NDJSON. Re-run the crawler after new objects land. |
| Firehose stream create fails with assume-role | IAM eventual consistency | `setup.sh` already waits 12 s; wait and re-run — it is idempotent-ish. |
| Budget create fails | Student IAM cannot call Budgets | Staff-side alarm; do not block the rest of the lab. |
| Bucket delete fails at teardown | Versioning left delete markers | The teardown script tries `object_versions.delete()` via boto3. Re-run, or empty versions in the console. |

`setup.sh` and `teardown.sh` are safe to re-run. Teardown requires
`CONFIRM=yes` and will refuse to delete a bucket whose name or
`Purpose` tag does not look like this lab.

## Safety briefing (say this out loud before Lab 5)

The `ESTOP` and `FAULT` events in this repo are *telemetry events*.
They are not safety functions. A cloud path (Firehose → S3 → Lambda →
email) has unbounded latency and many single points of failure. Any
real packing arm used in class must have:

1. A **hardwired emergency stop** that removes motor power with the
   network unplugged.
2. A **guarded workspace** — a fixed enclosure, an interlocked door,
   or a light curtain — also hardwired, never supervised from AWS.

Staff should test both paths before students power the arm. Do not
let a student “just jog it slowly without the guard.”

## Grading rubric (suggested, 20 points)

| Item | Points |
|---|---|
| Lab 1 dry-run trace of one item + a correct state-machine sketch | 3 |
| Lab 2: screenshot of a date-partitioned S3 object and a one-paragraph explanation of why the simulator cannot write to S3 directly | 3 |
| Lab 3: results of the core queries, plus the one-item trace (schema field → S3 line → Athena row) and the program / firmware vs. fault-rate table | 5 |
| Lab 4: fault email (or CloudWatch + SNS evidence) and a written sentence on why the alert is not a safety-rated E-stop and does not replace a guarded workspace | 4 |
| Exercises (pick two per lab, or the stretch in Lab 1) | 3 |
| Teardown evidence (empty resource list, or a staff check) and budget alarm in place | 2 |

Lab 5 can replace one written exercise or be offered as extra credit.

## Hint key (not full solutions)

- **Lab 1 ex. 1.** Athena and the Lambda key on `event_type`. Rename the
  schema field first, then every producer and consumer. Changing only
  the simulator silently drops places and alerts.
- **Lab 1 ex. 2.** A single `GRIP_FAIL` usually retries in `PICKING`.
  After several misses the simulator raises `FAULT` / `GRIP_FAIL`.
  Motion faults and E-stops recover through `RECOVERING` then `HOMING`.
- **Lab 2 ex. 1.** The simulator talks only to Firehose. Firehose, not
  the student, holds `s3:PutObject` on the lab prefix. That is the
  least-privilege split.
- **Lab 2 ex. 2.** Firehose is the wrong buffer for 1-second dashboards;
  Kinesis Data Streams (or IoT Core + a stream) would be the usual
  next step, at higher cost and operational complexity.
- **Lab 3 version exercise.** The numeric rates are from the flags the
  student passed; the learning check is that `program_version` and
  `firmware_version` survive into the catalog and group the result.
  Reject a table that does not include both versions.
- **Lab 3 ex. 2.** The three-line trace is the core learning check.
  Reject screenshots that do not share a common `item_id` and
  `program_version`.
- **Lab 4 ex. 1.** No faults (and no grip-fail burst) → no publish, so
  a quiet cell or a single missed pick does not spam email. A heartbeat
  is a different requirement (freshness).
- **Lab 4 ex. 2.** Prefix-scoped `GetObject` limits data exfiltration
  if the function is tricked into reading another key; topic-scoped
  `sns:Publish` stops the role being used as a spam relay.
- **Lab 5 ex. 3.** The pipeline must never be the actuator for an
  E-stop or a guard override. It should record, catalog, and notify.

## What staff should customize

- `NOTIFY_EMAIL` in the example config, plus any course-wide budget
  threshold.
- The enforcement contact in `CODE_OF_CONDUCT.md`.
- Copyright holder in `LICENSE` if your university wants a named
  copyright (the license is MIT either way).
- If your arm platform is already known (a specific desktop 5-axis,
  ROS 2, OPC UA, Modbus), add a short `RobotArmSource` subclass under
  `agent/` and point Lab 5 at it.
- If you need a classroom Git remote, this repository ships with no
  remote on purpose. Add one after the first commit.

## Maintenance

The unit tests (`python -m unittest discover -s tests -v`) do not need
AWS credentials and are the fast regression check after a schema or
Lambda change. `bash -n` the two infra scripts after any edit.
