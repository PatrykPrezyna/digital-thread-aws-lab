# Student lab guide — Digital Thread on AWS

This module follows a packed item from **design data** (the shared event
schema, including robot program and firmware version) through a
**device** (a simulated, then optionally a real, 5-axis packing arm)
into the **cloud** (Firehose, S3, Glue) and out to **analytics and
alerts** (Athena or Redshift Spectrum, Lambda, SNS). That path is the
digital thread:

**requirements / schema → robot program / config version → each pick
event → catalog → analytics.**

Each lab is written for a single ~2-hour session and ends with a
checkpoint plus exercises. Labs 1–4 can also be compressed into one
afternoon workshop (see the README). Lab 5 is optional and uses a
physical packing arm.

**Always create the budget alarm first and run `CONFIRM=yes ./infra/teardown.sh`
when you are finished.** Cloud safety is part of the lab, not an afterthought.

---

## Learning objectives (whole module)

By the end of the module you will be able to:

1. Explain the digital thread as *traceability* from a data contract and
   a named robot program / firmware version, through a device, into
   stored observations, into queries and alerts.
2. Write and evolve a shared event schema and keep producers and
   consumers in sync with it.
3. Ingest streaming device events with Amazon Kinesis Data Firehose into
   date-partitioned S3 using least-privilege IAM.
4. Catalog those events with AWS Glue and query them with Athena
   (and, optionally, Redshift Spectrum), including fault rate by
   program and firmware version.
5. Close the loop with a Lambda function that emails `FAULT`, `ESTOP`,
   and `GRIP_FAIL` bursts over SNS, and explain why that loop is *not*
   a safety function.
6. (Lab 5) Replace the simulator with a real 5-axis packing arm without
   changing the cloud pipeline, by honoring the same event contract.

---

## Lab 1 — Design data and the device (~2 hours)

### Concepts

The thread starts *before* AWS. `shared/event.schema.json` is design
data: a contract that says what a packing-arm event is, including the
`program_version` and `firmware_version` fields that later let you
blame (or exonerate) a software change. The simulator in
`simulator/simulate.py` is the first device that honors it. A later,
real arm must honor the same contract or the catalog, the queries, and
the alerts all break.

The cell is a desktop 5-axis robot arm (base, shoulder, elbow, wrist
pitch, wrist roll). It picks items from an infeed or tray, scans them,
and places them into 2–3 packing boxes by order. Its states are
`IDLE`, `HOMING`, `PICKING`, `MOVING`, `PLACING`, `FAULT`, `ESTOP`, and
`RECOVERING`.

### Steps

1. Read `shared/schema.md` and `shared/event.schema.json`.
2. Create a virtualenv and run the simulator locally (no AWS account yet):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python simulator/simulate.py --dry-run --duration 20 --seed 1 --rate 12
   ```

3. Pick one `item_id` in the output and list every event it produced, in
   order. Sketch the state machine by hand. Note the joint angles and
   the TCP pose on a `TELEMETRY` or `PLACE_OK` event.
4. Run the unit tests:

   ```bash
   pip install -r tests/requirements.txt
   python -m unittest discover -s tests -v
   ```

### Checkpoint

You can explain each required schema field (including why program and
firmware versions are required), show a dry-run trace for one item, and
point to the test that proves a `PLACE_OK` happens on the happy path.

### Exercises

1. What would break in Labs 3 and 4 if you renamed `event_type` to
   `type` only in the simulator? Which file should change first?
2. Raise `--grip-fail-probability` to `0.6` and `--fault-probability`
   to `0.4`. How do `GRIP_FAIL` events relate to a `FAULT` with
   `fault_code=GRIP_FAIL`, and when does `RECOVERING` go back through
   `HOMING`?
3. (Stretch) Add a `VACUUM_LOW` fault code. List every artifact you
   would have to touch to keep the thread consistent, including the
   Lambda burst logic and the SQL “faults per axis” query.

---

## Lab 2 — Ingest: IAM, Firehose, S3 (~2 hours)

### Concepts

Events leave the device and must land in durable, partitioned storage
without giving any service more permission than it needs. Firehose
batches records (60 s or 1 MiB) and writes them under

```text
s3://$BUCKET/telemetry/!{timestamp:yyyy/MM/dd/HH}/
```

Each IAM role in `iam/` is scoped to one service and one resource.

### Steps

1. Copy `infra/config.env.example` to `infra/config.env`. Set
   `NOTIFY_EMAIL` to your university address and keep `ENABLE_REDSHIFT=0`.
2. Make sure the AWS CLI can call `aws sts get-caller-identity`.
3. Run `./infra/setup.sh`. Confirm the SNS subscription email (and the
   budget email, if you received one).
4. Attach the managed policy `dtlab-simulator` (the script prints its
   ARN) to the IAM user or role you use in the terminal, unless staff
   already granted you that permission.
5. Stream two or three minutes of traffic:

   ```bash
   python simulator/simulate.py \
     --stream-name dtlab-telemetry \
     --region us-east-1 \
     --duration 180 \
     --rate 8 \
     --seed 7 \
     --program-version pack-v1.2.0 \
     --firmware-version fw-3.4.1
   ```

6. Wait at least 60 seconds for Firehose to flush. Then list the prefix:

   ```bash
   aws s3 ls s3://dtlab-telemetry-<your-account-id>/telemetry/ --recursive
   ```

7. Download one object and confirm it is newline-delimited JSON that
   still matches the schema, including joint angles and program version.

### Checkpoint

A date-partitioned object exists in *your* bucket, the bucket is not
public, and you can show which IAM role is allowed to write it
(Firehose) versus which identity is allowed to call `PutRecordBatch`
(you).

### Exercises

1. Open `iam/firehose-permissions.json` and `iam/simulator-permissions.json`.
   Why does the simulator policy *not* include `s3:PutObject`?
2. Firehose buffers for up to 60 seconds. If your instructor asked for
   a live dashboard at 1-second resolution, which service in the
   architecture would you change first, and what would that cost you?
3. (Stretch) What is the error prefix for, and what would you look for
   there if `aws s3 ls` stays empty after three minutes?

---

## Lab 3 — Catalog and query (~2 hours)

### Concepts

S3 holds bytes. A *catalog* (Glue) holds the meaning of those bytes:
column names, types, and location. Athena (and optionally Redshift
Spectrum) query the catalog. That is the analytics end of the thread:
you can ask “what is the grip-fail rate on box 2, and did firmware
`fw-3.4.1` raise joint overcurrents?” without writing a new program.

### Steps

1. Start the crawler and wait until it reports `SUCCESS`:

   ```bash
   aws glue start-crawler --name dtlab-telemetry-crawler --region us-east-1
   aws glue get-crawler --name dtlab-telemetry-crawler --region us-east-1 \
     --query 'Crawler.State' --output text
   ```

2. In the Athena console, set a query result location (for example
   `s3://$BUCKET/athena-results/`) and select the `dtlab_telemetry`
   database.
3. Run the seven queries in `sql/queries.sql` (Athena versions):
   - places (throughput) per minute
   - cycle time per box
   - grip-fail rate
   - faults per axis
   - mean time between faults
   - E-stop count and mean recovery time
   - per-joint temperature trend
4. **Digital-thread exercise — version vs. fault rate.** Run a second
   short simulation with a *different* program or firmware label and a
   *different* fault mix, wait for Firehose, re-run the crawler if it
   does not pick up new files automatically, and run the “fault rate
   by program / firmware version” query at the bottom of
   `sql/queries.sql`.

   ```bash
   python simulator/simulate.py \
     --stream-name dtlab-telemetry \
     --region us-east-1 \
     --duration 180 \
     --program-version pack-v0.9-beta \
     --firmware-version fw-3.4.0 \
     --grip-fail-probability 0.35 \
     --fault-probability 0.20
   ```

   In your write-up, argue from the query result whether you would
   promote `pack-v0.9-beta` to production. The point is not the random
   numbers — it is that the version stamped at the device is still
   queryable in the catalog.
5. (Optional, instructor-approved only) Set `ENABLE_REDSHIFT=1`,
   re-run setup, create the external schema at the top of
   `sql/queries.sql`, and compare one result with Athena. Tear the
   workgroup down the same day.

### Checkpoint

You have a screenshot or exported CSV of the core queries, a version
comparison table, and you can point from a row in the throughput query
back to a `PLACE_OK` record in S3 and to the schema field that made
that row possible.

### Exercises

1. Write one more query: mean `j2_temp_c` (shoulder) during `MOVING`
   vs. `IDLE`, by firmware version.
2. Trace one `item_id` from the schema, through a raw S3 line (include
   `program_version`), to an Athena `SELECT * WHERE item_id = …`
   result. That three-line trace *is* the digital thread for one
   physical thing.
3. The Firehose prefix is `yyyy/MM/dd/HH`, not Hive-style
   `year=yyyy/month=MM/…`. How did Glue represent that, and what would
   you change if you needed partition pruning later?

---

## Lab 4 — Close the loop: fault alerts (~2 hours)

### Concepts

Analytics looks backward. An alert looks *almost* now: a new S3 object
invokes Lambda, which scans for `FAULT` and `ESTOP`, and for a
**burst** of `GRIP_FAIL` events (default: three in the same object),
then publishes a summary to SNS. A single missed grip does not page
the lab; a streak does. This is still observational. It does **not**
stop the arm. A hardwired E-stop and a guarded workspace must exist on
any real cell; the cloud path is too slow and too unreliable to be a
safety function.

### Steps

1. Confirm your SNS email subscription is in the `Confirmed` state.
2. Read `lambda/fault_alert.py`. Note how it accepts both NDJSON and
   concatenated JSON (Firehose can produce either), and how the grip-
   fail burst threshold is configured (`GRIP_FAIL_BURST`).
3. Force faults and a grip-fail streak, then wait for the flush + email:

   ```bash
   python simulator/simulate.py \
     --stream-name dtlab-telemetry \
     --region us-east-1 \
     --duration 180 \
     --grip-fail-probability 0.6 \
     --fault-probability 0.4 \
     --estop-probability 0.25
   ```

4. If no email arrives after ~3 minutes, check CloudWatch logs for
   `/aws/lambda/dtlab-fault-alert` and the Firehose error prefix
   `s3://$BUCKET/errors/`.
5. Re-run the unit tests and explain what the mocked SNS assertion is
   checking, including why a single `GRIP_FAIL` does not publish.

### Checkpoint

You received (or can show in CloudWatch + a published SNS test message)
at least one fault alert, and you can state in one sentence why this
alert is not a safety-rated E-stop and does not replace a guarded
workspace.

### Exercises

1. Why does the Lambda stay silent when a file contains only
   `PLACE_OK` events, and also when it contains a single `GRIP_FAIL`?
   When would you *want* a heartbeat alert instead?
2. The Lambda role may `s3:GetObject` only on the telemetry prefix and
   `sns:Publish` only on one topic. Which attack is each restriction
   trying to make harder?
3. (Stretch) Add “shoulder temperature above 60 °C” as an alert
   condition without breaking the existing tests. Did you change the
   schema?

### Cleanup

If you are not continuing to Lab 5 today, tear the account down:

```bash
CONFIRM=yes ./infra/teardown.sh
```

---

## Lab 5 — Replace the simulator with a real packing arm (~2 hours, optional)

### Concepts

The thread holds together when the *device* changes but the *contract*
does not. `agent/arm_agent.py` is the hook: implement `RobotArmSource`
for your platform (serial, ROS 2, OPC UA, Modbus, a vendor SDK), keep
publishing events that pass `normalize_event()`, and reuse the
Firehose stream you already have.

### Steps

Follow `agent/README.md`. Minimum path:

1. Run `python agent/arm_agent.py --dry-run` and inspect the demo events.
2. Implement a source that reads your arm.
3. Map gripper, collision, axis-limit, overcurrent, and E-stop
   hardware signals onto `GRIP_FAIL`, `FAULT`, and `ESTOP`. Keep the
   hardware stop and the workspace guards *on the cell*.
4. Stamp `program_version` and `firmware_version` from the file you
   actually loaded on the controller.
5. Publish through `--backend firehose` (or IoT Core, if staff have
   issued device certificates).
6. Re-run the Lab 3 version-comparison query and confirm Lab 4 still
   emails on a real fault or a grip-fail burst.

### Checkpoint

A single item that existed in the physical world is visible in S3 and
in Athena, and your report shows the mapping from a hardware signal
and a program version to a schema field to a catalog column.

### Exercises

1. Compare IAM-user credentials on a laptop with IoT Core device
   certificates on the cell PC. Which would you take to a factory
   floor, and why?
2. If the cell drops off the Wi-Fi for two minutes, what should happen
   to events that were already observed on the controller? (Hint: the
   schema already has a device-side `timestamp`.)
3. Name one thing the cloud pipeline must never be allowed to do to
   the arm, and one thing it *should* be allowed to do.

### Cleanup

```bash
CONFIRM=yes ./infra/teardown.sh
```

Power the arm down, and verify the hardware E-stop and the workspace
interlock still work with the network unplugged.
