-- Digital Thread AWS Lab — analytics queries
--
-- After the Glue crawler has run, a table (usually named "telemetry") appears
-- in the lab database (default: dtlab_telemetry). Replace the placeholders
-- before you run anything:
--   ${ACCOUNT_ID}  ${REGION}  ${PROJECT_NAME}  ${GLUE_DATABASE}
--
-- Athena is the default engine for this lab (cheap, no cluster). Redshift
-- Serverless + Spectrum is optional and more expensive — only use it if your
-- instructor asked you to, and tear it down the same day.
--
-- The crawler may add partition columns (partition_0, partition_1, …) from
-- the Firehose prefix telemetry/yyyy/MM/dd/HH/. You can ignore them; they
-- are not needed for these queries. If a column name differs in your
-- catalog, describe the table first:
--   Athena:    SHOW COLUMNS IN ${GLUE_DATABASE}.telemetry;
--   Redshift:  SELECT * FROM SVV_EXTERNAL_COLUMNS WHERE schemaname = 'dt_spectrum';


-- =============================================================================
-- Redshift Spectrum: expose the Glue catalog as an external schema
-- =============================================================================
-- Run once on the Serverless workgroup (or provisioned cluster) that has
-- the ${PROJECT_NAME}-redshift IAM role attached to its namespace.
--
-- CREATE EXTERNAL SCHEMA dt_spectrum
-- FROM DATA CATALOG
-- DATABASE '${GLUE_DATABASE}'
-- IAM_ROLE 'arn:aws:iam::${ACCOUNT_ID}:role/${PROJECT_NAME}-redshift'
-- REGION '${REGION}';
--
-- After that, the same table is ${GLUE_DATABASE}.telemetry in Athena and
-- dt_spectrum.telemetry in Redshift. The queries below are given in
-- both dialects (Redshift versions are commented).


-- =============================================================================
-- 1. Throughput — successful picks (or places) per minute
-- =============================================================================
-- PICK_OK is the grasp; PLACE_OK is the completed pack. Use PLACE_OK as
-- the default throughput measure.

-- Athena
SELECT
  date_trunc('minute', from_iso8601_timestamp(timestamp)) AS minute,
  count(*) AS places_completed
FROM ${GLUE_DATABASE}.telemetry
WHERE event_type = 'PLACE_OK'
GROUP BY 1
ORDER BY 1;

-- Redshift Spectrum
-- SELECT
--   date_trunc('minute', timestamp::timestamptz) AS minute,
--   count(*) AS places_completed
-- FROM dt_spectrum.telemetry
-- WHERE event_type = 'PLACE_OK'
-- GROUP BY 1
-- ORDER BY 1;


-- =============================================================================
-- 2. Cycle time per box
-- =============================================================================
-- cycle_time_ms is stamped on PLACE_OK (elapsed from ITEM_PRESENTED).

-- Athena
SELECT
  box_id,
  count(*) AS places,
  avg(cycle_time_ms) AS mean_cycle_time_ms,
  min(cycle_time_ms) AS min_cycle_time_ms,
  max(cycle_time_ms) AS max_cycle_time_ms
FROM ${GLUE_DATABASE}.telemetry
WHERE event_type = 'PLACE_OK'
GROUP BY box_id
ORDER BY box_id;

-- Redshift Spectrum
-- SELECT
--   box_id,
--   count(*) AS places,
--   avg(cycle_time_ms) AS mean_cycle_time_ms,
--   min(cycle_time_ms) AS min_cycle_time_ms,
--   max(cycle_time_ms) AS max_cycle_time_ms
-- FROM dt_spectrum.telemetry
-- WHERE event_type = 'PLACE_OK'
-- GROUP BY box_id
-- ORDER BY box_id;


-- =============================================================================
-- 3. Grip-fail rate
-- =============================================================================
-- Rate = GRIP_FAIL events / (PICK_OK + GRIP_FAIL) grip attempts.

-- Athena
SELECT
  count(*) FILTER (WHERE event_type = 'GRIP_FAIL') AS grip_fails,
  count(*) FILTER (WHERE event_type = 'PICK_OK') AS pick_oks,
  count(*) FILTER (WHERE event_type IN ('PICK_OK', 'GRIP_FAIL')) AS grip_attempts,
  1.0 * count(*) FILTER (WHERE event_type = 'GRIP_FAIL')
    / nullif(count(*) FILTER (WHERE event_type IN ('PICK_OK', 'GRIP_FAIL')), 0)
    AS grip_fail_rate
FROM ${GLUE_DATABASE}.telemetry
WHERE event_type IN ('PICK_OK', 'GRIP_FAIL');

-- Redshift Spectrum (FILTER is Athena / Trino; use CASE)
-- SELECT
--   sum(CASE WHEN event_type = 'GRIP_FAIL' THEN 1 ELSE 0 END) AS grip_fails,
--   sum(CASE WHEN event_type = 'PICK_OK' THEN 1 ELSE 0 END) AS pick_oks,
--   sum(CASE WHEN event_type IN ('PICK_OK', 'GRIP_FAIL') THEN 1 ELSE 0 END) AS grip_attempts,
--   1.0 * sum(CASE WHEN event_type = 'GRIP_FAIL' THEN 1 ELSE 0 END)
--     / nullif(sum(CASE WHEN event_type IN ('PICK_OK', 'GRIP_FAIL') THEN 1 ELSE 0 END), 0)
--     AS grip_fail_rate
-- FROM dt_spectrum.telemetry
-- WHERE event_type IN ('PICK_OK', 'GRIP_FAIL');


-- =============================================================================
-- 4. Faults per axis
-- =============================================================================
-- AXIS_LIMIT and JOINT_OVERCURRENT carry fault_axis (1–5). COLLISION may
-- also name an axis. Rows with a null axis are counted separately.

-- Athena
SELECT
  fault_code,
  fault_axis,
  count(*) AS fault_events
FROM ${GLUE_DATABASE}.telemetry
WHERE event_type = 'FAULT'
GROUP BY fault_code, fault_axis
ORDER BY fault_code, fault_axis;

-- Redshift Spectrum
-- SELECT
--   fault_code,
--   fault_axis,
--   count(*) AS fault_events
-- FROM dt_spectrum.telemetry
-- WHERE event_type = 'FAULT'
-- GROUP BY fault_code, fault_axis
-- ORDER BY fault_code, fault_axis;


-- =============================================================================
-- 5. Mean time between faults (seconds)
-- =============================================================================
-- A "fault" here is a FAULT or ESTOP event. Add PARTITION BY robot_id if
-- you run more than one arm.

-- Athena
WITH faults AS (
  SELECT from_iso8601_timestamp(timestamp) AS ts
  FROM ${GLUE_DATABASE}.telemetry
  WHERE event_type IN ('FAULT', 'ESTOP')
),
gaps AS (
  SELECT date_diff('second', lag(ts) OVER (ORDER BY ts), ts) AS gap_s
  FROM faults
)
SELECT
  (SELECT count(*) FROM faults) AS fault_events,
  avg(gap_s) AS mean_time_between_faults_s
FROM gaps
WHERE gap_s IS NOT NULL;

-- Redshift Spectrum
-- WITH faults AS (
--   SELECT timestamp::timestamptz AS ts
--   FROM dt_spectrum.telemetry
--   WHERE event_type IN ('FAULT', 'ESTOP')
-- ),
-- gaps AS (
--   SELECT datediff(second, lag(ts) OVER (ORDER BY ts), ts) AS gap_s
--   FROM faults
-- )
-- SELECT
--   (SELECT count(*) FROM faults) AS fault_events,
--   avg(gap_s) AS mean_time_between_faults_s
-- FROM gaps
-- WHERE gap_s IS NOT NULL;


-- =============================================================================
-- 6. E-stop count and mean recovery time
-- =============================================================================
-- Recovery time is the interval from ESTOP to the next ESTOP_RESET on the
-- same robot. Unpaired E-stops (simulation ended while still stopped) are
-- counted separately and excluded from the mean.

-- Athena
WITH ordered AS (
  SELECT
    robot_id,
    event_type,
    from_iso8601_timestamp(timestamp) AS ts
  FROM ${GLUE_DATABASE}.telemetry
  WHERE event_type IN ('ESTOP', 'ESTOP_RESET')
),
paired AS (
  SELECT
    robot_id,
    event_type,
    ts,
    lead(event_type) OVER (PARTITION BY robot_id ORDER BY ts) AS next_type,
    lead(ts)         OVER (PARTITION BY robot_id ORDER BY ts) AS next_ts
  FROM ordered
)
SELECT
  count(*) FILTER (WHERE event_type = 'ESTOP') AS estop_count,
  count(*) FILTER (WHERE event_type = 'ESTOP' AND next_type = 'ESTOP_RESET') AS paired_resets,
  avg(date_diff('second', ts, next_ts))
    FILTER (WHERE event_type = 'ESTOP' AND next_type = 'ESTOP_RESET') AS mean_recovery_s
FROM paired;

-- Redshift Spectrum
-- WITH ordered AS (
--   SELECT
--     robot_id,
--     event_type,
--     timestamp::timestamptz AS ts
--   FROM dt_spectrum.telemetry
--   WHERE event_type IN ('ESTOP', 'ESTOP_RESET')
-- ),
-- paired AS (
--   SELECT
--     robot_id,
--     event_type,
--     ts,
--     lead(event_type) OVER (PARTITION BY robot_id ORDER BY ts) AS next_type,
--     lead(ts)         OVER (PARTITION BY robot_id ORDER BY ts) AS next_ts
--   FROM ordered
-- )
-- SELECT
--   sum(CASE WHEN event_type = 'ESTOP' THEN 1 ELSE 0 END) AS estop_count,
--   sum(CASE WHEN event_type = 'ESTOP' AND next_type = 'ESTOP_RESET' THEN 1 ELSE 0 END) AS paired_resets,
--   avg(CASE WHEN event_type = 'ESTOP' AND next_type = 'ESTOP_RESET'
--            THEN datediff(second, ts, next_ts) END) AS mean_recovery_s
-- FROM paired;


-- =============================================================================
-- 7. Per-joint temperature trend
-- =============================================================================
-- TELEMETRY (and any event) carries j1_temp_c … j5_temp_c. Bucketed by
-- minute so a short lab run still plots.

-- Athena
SELECT
  date_trunc('minute', from_iso8601_timestamp(timestamp)) AS minute,
  avg(j1_temp_c) AS j1_base_c,
  avg(j2_temp_c) AS j2_shoulder_c,
  avg(j3_temp_c) AS j3_elbow_c,
  avg(j4_temp_c) AS j4_wrist_pitch_c,
  avg(j5_temp_c) AS j5_wrist_roll_c
FROM ${GLUE_DATABASE}.telemetry
WHERE event_type = 'TELEMETRY'
GROUP BY 1
ORDER BY 1;

-- Redshift Spectrum
-- SELECT
--   date_trunc('minute', timestamp::timestamptz) AS minute,
--   avg(j1_temp_c) AS j1_base_c,
--   avg(j2_temp_c) AS j2_shoulder_c,
--   avg(j3_temp_c) AS j3_elbow_c,
--   avg(j4_temp_c) AS j4_wrist_pitch_c,
--   avg(j5_temp_c) AS j5_wrist_roll_c
-- FROM dt_spectrum.telemetry
-- WHERE event_type = 'TELEMETRY'
-- GROUP BY 1
-- ORDER BY 1;


-- =============================================================================
-- Digital thread: fault rate by program / firmware version (Lab 3 exercise)
-- =============================================================================
-- Run the simulator (or the real arm) twice, changing --program-version
-- and/or --firmware-version and the fault / grip-fail probabilities, then
-- ask whether the new program is worse.

-- Athena
SELECT
  program_version,
  firmware_version,
  count(*) FILTER (WHERE event_type = 'PLACE_OK') AS places,
  count(*) FILTER (WHERE event_type = 'GRIP_FAIL') AS grip_fails,
  count(*) FILTER (WHERE event_type = 'FAULT') AS faults,
  count(*) FILTER (WHERE event_type = 'ESTOP') AS estops,
  1.0 * count(*) FILTER (WHERE event_type = 'FAULT')
    / nullif(count(*) FILTER (WHERE event_type = 'PLACE_OK'), 0)
    AS faults_per_place
FROM ${GLUE_DATABASE}.telemetry
GROUP BY program_version, firmware_version
ORDER BY program_version, firmware_version;


-- =============================================================================
-- Optional extras (Athena)
-- =============================================================================

-- Trace one item through the thread (replace the id)
-- SELECT timestamp, event_type, state, box_id, order_id, fault_code,
--        program_version, firmware_version, cycle_time_ms,
--        j1_deg, j2_deg, j3_deg, j4_deg, j5_deg, gripper_state
-- FROM ${GLUE_DATABASE}.telemetry
-- WHERE item_id = 'I-000001'
-- ORDER BY timestamp;
