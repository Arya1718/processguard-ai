# Failure Modes (Prompt 2)

What happens when things break. Each section names the trigger, the behavior,
and how to verify it.

## Dropped reading

* **Trigger:** malformed stream entry (missing/invalid field), or a reading
  for a sensor_id not in the `Sensors` table (e.g. DB reset while the stream
  still has entries).
* **Behavior:** the Detection Agent logs the drop with the stream entry ID and
  the reason, **acks** the message, and moves on. One bad reading never blocks
  the stream; the baseline keeps the last good window.
* **Verify:** `docker compose logs agent-service | grep -E "Dropping malformed|Unknown sensor"`.

## Postgres write failure

* **Trigger:** DB restart, transient connectivity loss, migration in progress.
* **Behavior:** the incident upsert retries up to
  `PGAI_DETECTION__DB_RETRY_ATTEMPTS` (3) times with linear backoff
  (0.5 s, 1 s, 1.5 s). If all attempts fail, the anomaly context (equipment,
  severity, sensor list, full summary JSON) goes to the **dead-letter log
  channel** — `DEAD-LETTER [anomaly dropped]: ...` at ERROR level with the
  triggering exception. The stream message is acked either way so the pipeline
  does not wedge on one poison row.
* **Data loss:** the incident is lost from Postgres (by design we log rather
  than silently drop; a later prompt can move this to a durable DLQ queue).
  Subsequent readings of the same ongoing anomaly re-attempt persistence
  naturally, so a healthy DB self-heals within one correlation window.
* **Verify:** stop postgres, trigger the scenario, watch for the DEAD-LETTER
  line, start postgres, confirm the incident appears on the next detection.

## Event publish failure

* **Trigger:** Redis pub/sub unavailable at publish time (the stream is a
  different Redis data structure — the stream may still work during a pub/sub
  client fault, and vice versa).
* **Behavior:** same retry-then-dead-letter path as the DB failure. The
  incident row is already committed, so the incident exists with evidence even
  if the downstream fan-out missed the event; consumers of
  `pgai.anomalies` should reconcile via `GET /api/v1/incidents` if they
  detect a gap (documented contract for Prompt 3).

## Duplicate anomaly (idempotency)

* **Trigger:** the same underlying anomaly keeps producing anomalous readings
  (e.g. scenario holds for minutes), or the stream redelivers an entry after a
  consumer restart before it was acked.
* **Behavior:** the upsert finds the still-`open` incident for the equipment
  (`SELECT ... FOR UPDATE`) and **updates** its `AnomalySummary`/`Severity`
  instead of inserting. Exactly one open incident per equipment persists.
  After an incident is `resolved`, the next genuinely new anomaly opens a new
  incident.
* **Verify:** trigger the scenario twice in a row without reset — the second
  trigger must not create a second incident row.

## Simulator restart mid-stream

* **Trigger:** `docker compose restart simulator`, crash, or reschedule.
* **Behavior:** stateless producer — it re-loads sensors from Postgres and
  continues emitting into the stream. The scenario mode is lost (control state
  lives in the process + short-TTL Redis key), so it resumes in **normal**
  mode. Baselines in Redis survive (TTL 1 h) and absorb the reading gap; the
  max-gap correlation rule prevents a pre-restart stale cluster from mixing
  with post-restart readings.
* **Verify:** restart the simulator mid-scenario; confirm normal-mode readings
  resume and no new incident appears.

## Detection-agent (consumer) restart mid-stream

* **Trigger:** `docker compose restart agent-service` while readings flow.
* **Behavior:** the Redis consumer group `detection-agent` persists its last
  delivered ID. On restart the agent resumes from the group cursor — entries
  already acked are not reprocessed; entries read-but-not-acked (crash between
  read and ack) are redelivered, and the idempotent upsert plus in-Redis
  baselines make reprocessing harmless. The in-memory correlation buffer
  starts empty, so an anomaly spanning the restart re-clusters from the next
  anomalous reading within one correlation window.
* **Verify:** trigger scenario, restart agent-service mid-ramp, confirm still
  exactly one open incident after values settle.

## Reset while anomaly active

* **Trigger:** `POST /api/v1/simulator/reset` before anyone resolves the
  incident.
* **Behavior:** readings return to normal within seconds; the existing
  incident stays `open` (resolution is a human/agent action in a later
  prompt, not an automatic reset side effect). Re-triggering later refreshes
  the same open incident (idempotency) rather than duplicating it.
