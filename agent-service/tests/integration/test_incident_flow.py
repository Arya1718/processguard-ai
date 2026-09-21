"""Integration test against the LIVE compose stack.

Prompt 2 proof: trigger -> Detection Agent correlates 5 drifting sensors ->
ONE open incident on Site 12 / CP-04 -> idempotent on re-trigger.
Prompt 3 extension: the Knowledge Agent attaches SOP + historical evidence
and the Root-Cause Agent records a citation-checked hypothesis with a match
to the 17-days-ago historical incident.

Requires: `docker compose up -d` with the stack healthy.
Run:  cd agent-service && python -m pytest tests/test_incident_flow.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import httpx
import pytest

MIDDLEWARE = os.environ.get("PGAI_TEST_MIDDLEWARE", "http://localhost:8080/api/v1")

import json as _json

def _loads(text: str):
    return _json.loads(text)

# Integration marker: requires the live compose stack (see pytest.ini).
pytestmark = pytest.mark.integration


def _token() -> str:
    """Prompt 7: real OIDC -- mint from the provider's test endpoint (same
    RS256 keys/claims as the browser flow). Dev-token is gone by design."""
    from oidc_helpers import MAINTENANCE_S12, mint

    return mint(MAINTENANCE_S12)


@pytest.fixture(scope="module")
def headers() -> dict:
    return {"Authorization": f"Bearer {_token()}"}


@pytest.fixture(scope="module", autouse=True)
def _clean_stale_incidents():
    """Mark incidents from previous test runs as resolved so each run
    exercises a FRESH detection + diagnosis cycle (the Detection Agent's
    upsert would otherwise fold the new anomaly into the old incident --
    correct production behavior, wrong test setup). Uses the same psql
    container the compose stack runs; dev database, demo data only."""
    subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "pgai",
         "-d", "processguard", "-c",
         'UPDATE "Incidents" SET "Status"=\'resolved\', "ResolvedAt"=NOW() '
         'WHERE "Status" != \'resolved\'; '
         'DELETE FROM "HistoricalIncidents" WHERE "Source" LIKE \'HIST-AUTO%\';'],
        check=False, capture_output=True, timeout=60,
    )
    yield


def _wait_for_incident(headers, timeout_s: float) -> dict | None:
    """Poll until the correlated incident carries ALL 5 drifting sensors.

    With the Orchestrator (Prompt 4) the WHOLE chain -- detection through
    awaiting_approval -- can complete in a few seconds, faster than one
    poll interval, so this helper accepts ANY active (non-resolved) status:
    the incident having the full 5-sensor signature is the real assertion.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = httpx.get(f"{MIDDLEWARE}/incidents?limit=10", headers=headers, timeout=10)
        response.raise_for_status()
        items = [i for i in response.json()["items"] if i["status"] != "resolved"]
        if items and (items[0].get("anomalySummary") or {}).get("sensor_count") == 5:
            return items[0]
        time.sleep(1.0)
    return None


def test_scenario_end_to_end(headers) -> None:
    """Trigger -> one open incident with 5 sensors -> diagnosed with citations."""
    # Clean slate: reset the simulator first.
    reset = httpx.post(f"{MIDDLEWARE}/simulator/reset", headers=headers, timeout=10)
    reset.raise_for_status()
    time.sleep(2)

    # Trigger the reference scenario (passthrough via middleware).
    trigger = httpx.post(
        f"{MIDDLEWARE}/simulator/trigger-scenario/cooling-tower-incident",
        headers=headers, timeout=10,
    )
    trigger.raise_for_status()
    assert trigger.json()["triggered"] is True

    # The Detection Agent should correlate the drift into ONE incident.
    incident = _wait_for_incident(headers, timeout_s=90)
    assert incident is not None, "no open incident appeared after scenario trigger"

    summary = incident["anomalySummary"]
    assert summary["sensor_count"] == 5
    assert summary["equipment_id"] == incident["equipmentId"]
    flagged = {s["sensor_type"] for s in summary["sensors"]}
    assert flagged == {"temperature", "ph", "flow_rate", "vibration", "conductivity"}

    # Severity computed from ramp magnitude.
    assert incident["severity"] in ("medium", "high")

    # ------------------------------------------------------------------
    # Prompt 3: wait for the diagnosis pipeline (Knowledge then Root-Cause).
    # ------------------------------------------------------------------
    deadline = time.monotonic() + 120  # retrieval + one LLM round trip
    root_cause = None
    evidence = None
    while time.monotonic() < deadline:
        detail = httpx.get(f"{MIDDLEWARE}/incidents/{incident['id']}", headers=headers, timeout=10)
        detail.raise_for_status()
        body = detail.json()
        root_cause = body.get("rootCause")
        evidence = body.get("retrievedEvidence")
        if root_cause and root_cause.get("hypothesis"):
            break
        time.sleep(2.0)

    # --- Knowledge Agent evidence -------------------------------------------
    assert evidence, "Knowledge Agent wrote no evidence onto the incident"
    sop_chunks = evidence.get("sop_chunks") or []
    assert sop_chunks, "no SOP chunks retrieved"
    assert any(c["docId"] == "SOP-COOL-014" for c in sop_chunks), \
        "SOP-COOL-014 must be among the retrieved chunks"
    history = evidence.get("matched_history") or []
    assert history, "no historical incidents matched"
    assert any("HIST-2026-0141" in (h.get("source") or "") for h in history), \
        "the 17-days-ago CP-04 incident must be matched"

    # Focused evidence endpoint agrees with the incident detail.
    ev_endpoint = httpx.get(f"{MIDDLEWARE}/incidents/{incident['id']}/evidence", headers=headers, timeout=10)
    ev_endpoint.raise_for_status()
    assert ev_endpoint.json()["evidence"]["sop_chunks"], "evidence endpoint returned no SOP chunks"

    # --- Root-Cause Agent conclusion ----------------------------------------
    assert root_cause and root_cause.get("hypothesis"), "no root-cause hypothesis recorded"
    hypothesis = root_cause["hypothesis"].lower()
    assert "pump degradation" in hypothesis, f"hypothesis should mention pump degradation: {hypothesis}"
    confidence = root_cause.get("confidence")
    assert isinstance(confidence, int) and 0 <= confidence <= 100
    cited = root_cause.get("citedEvidence") or []
    assert cited, "every claim must carry a citation"
    assert any(c["type"] == "sop" and "SOP-COOL-014" in c["ref"] for c in cited), \
        "at least one citation must point at SOP-COOL-014"
    assert any(c["type"] == "history" and "HIST-2026-0141" in c["ref"] for c in cited), \
        "at least one citation must point at the 17-days-ago incident"

    # Status left 'open': the diagnosis pipeline (investigating) and the
    # orchestrator's stages (risk_assessed -> recommended -> HITL landing)
    # may ALL have completed by the time we poll, so assert progress, not
    # one specific intermediate value.
    detail2 = httpx.get(f"{MIDDLEWARE}/incidents/{incident['id']}", headers=headers, timeout=10)
    detail2.raise_for_status()
    assert detail2.json()["status"] != "open"

    # Latest-readings endpoint still works for the incident's site.
    latest = httpx.get(f"{MIDDLEWARE}/sites/{incident['siteId']}/sensors/latest", headers=headers, timeout=10)
    latest.raise_for_status()
    readings = latest.json()["sensors"]
    assert len(readings) == 5
    assert all(r["value"] is not None for r in readings)

    # Correlation IDs: middleware echoes one on every response.
    assert detail2.headers.get("X-Correlation-Id")

    # ------------------------------------------------------------------
    # Idempotency: retrigger while the incident is still open -> still
    # exactly ONE open incident (the same one), not a duplicate, and the
    # diagnosis is not duplicated or overwritten by a second LLM pass.
    # ------------------------------------------------------------------
    retrigger = httpx.post(
        f"{MIDDLEWARE}/simulator/trigger-scenario/cooling-tower-incident",
        headers=headers, timeout=10,
    )
    retrigger.raise_for_status()
    time.sleep(12)  # let several detection passes run

    response = httpx.get(f"{MIDDLEWARE}/incidents?limit=50", headers=headers, timeout=10)
    response.raise_for_status()
    # "Active" = not resolved: the diagnosis may legitimately have moved the
    # incident to investigating (or needs_human_review) by this point.
    active = [i for i in response.json()["items"] if i["status"] != "resolved"]
    assert len(active) == 1, f"expected 1 active incident, got {len(active)}"
    assert active[0]["id"] == incident["id"], "incident id changed (duplicate created)"

    detail3 = httpx.get(f"{MIDDLEWARE}/incidents/{incident['id']}", headers=headers, timeout=10)
    detail3.raise_for_status()
    assert detail3.json()["rootCause"]["hypothesis"] == root_cause["hypothesis"], \
        "re-trigger must not re-run the LLM diagnosis on an already-diagnosed incident"

    # ------------------------------------------------------------------
    # Prompt 4: the Orchestrator drives risk -> recommendation -> HITL
    # gate. For this scenario severity is HIGH and the fake diagnosis
    # confidence is 87, so the config table yields EXACTLY level 2
    # (awaiting_approval). Asserting the specific level, not "some level".
    # ------------------------------------------------------------------
    deadline = time.monotonic() + 90
    final_status = None
    while time.monotonic() < deadline:
        detail4 = httpx.get(f"{MIDDLEWARE}/incidents/{incident['id']}", headers=headers, timeout=10)
        detail4.raise_for_status()
        final_status = detail4.json()["status"]
        if final_status == "awaiting_approval":
            break
        time.sleep(2.0)
    assert final_status == "awaiting_approval", \
        f"expected the HITL gate to land on awaiting_approval, got {final_status}"

    # Risk endpoint: the SPECIFIC HITL level for this scenario is 2
    # (severity medium-or-high x fake confidence 87 -> level 2, deterministically).
    # Severity itself depends on when the Risk Agent's snapshot lands during
    # the ~30-60s ramp: the anomaly summary is legitimately still developing
    # (medium) or fully developed (high) -- both are correct behavior.
    risk = httpx.get(f"{MIDDLEWARE}/incidents/{incident['id']}/risk", headers=headers, timeout=10)
    risk.raise_for_status()
    risk_body = risk.json()
    assert risk_body["severity"] in ("medium", "high")
    assert risk_body["hitlLevel"] == 2, f"cooling-tower scenario must gate at level 2: {risk_body}"
    consequence_names = {c["consequence"] for c in risk_body["consequences"]}
    assert "equipment damage" in consequence_names  # pump-degradation signature
    assert "chemical imbalance" in consequence_names  # the scenario also drifts pH/conductivity
    assert "unplanned shutdown" not in consequence_names  # no failure/trip signature asserted

    # Recommendation endpoint: actions + operator rationale + VISIBLE tool log.
    rec = httpx.get(f"{MIDDLEWARE}/incidents/{incident['id']}/recommendation", headers=headers, timeout=10)
    rec.raise_for_status()
    rec_body = rec.json()
    assert rec_body["actions"], "no recommended actions recorded"
    assert rec_body["rationale"] and rec_body["rationale"].startswith("[HITL Level 2 applies")
    tool_names = [e["tool"] for e in rec_body["toolCallLog"]]
    assert "get_equipment_maintenance_status" in tool_names, "planner must consult maintenance status"
    assert "get_similar_incident_outcomes" in tool_names, "planner must consult past outcomes"
    assert all("result" in e and "at" in e for e in rec_body["toolCallLog"])

    # State history: complete ordered transition chain for the demo narrative.
    hist = httpx.get(f"{MIDDLEWARE}/incidents/{incident['id']}/state-history", headers=headers, timeout=10)
    hist.raise_for_status()
    transitions = hist.json()["transitions"]
    to_statuses = [t["to"] for t in transitions]
    assert to_statuses == ["investigating", "risk_assessed", "recommended", "awaiting_approval"], \
        f"unexpected state-machine path: {to_statuses}"
    stamps = [t["changedAt"] for t in transitions]
    assert stamps == sorted(stamps), "state history must be chronologically ordered"

    # Approval flow: approve -> 'approved' + recorded decision; double-approve -> 409.
    # The approver identity is taken from the JWT (the dev token was issued
    # for the JWT identity), never from the request body.
    approve = httpx.post(
        f"{MIDDLEWARE}/incidents/{incident['id']}/approve",
        headers=headers, json={"decidedBy": "spoofed-identity"}, timeout=10,
    )
    approve.raise_for_status()
    approved = approve.json()
    assert approved["decision"] == "approved"
    assert approved["decidedBy"] == "maintenance@site12.demo", \
        "approver identity must come from the JWT, not the body"

    detail5 = httpx.get(f"{MIDDLEWARE}/incidents/{incident['id']}", headers=headers, timeout=10)
    detail5.raise_for_status()
    assert detail5.json()["status"] == "approved"

    conflict = httpx.post(
        f"{MIDDLEWARE}/incidents/{incident['id']}/approve",
        headers=headers, json={"decidedBy": "demo-supervisor"}, timeout=10,
    )
    assert conflict.status_code == 409, "approving twice must conflict"

    # Audit trail: the full decision chain is in AuditLog (unsanitized).
    audit = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "pgai",
         "-d", "processguard", "-tAc",
         f"SELECT COUNT(*) FROM \"AuditLog\" WHERE \"IncidentId\"='{incident['id']}' "
         "AND \"Action\"='hitl_approved';"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    assert audit == "1", f"expected one full audit chain entry, got {audit}"
    approval_rows = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "pgai",
         "-d", "processguard", "-tAc",
         f"SELECT \"DecidedBy\" FROM \"Approvals\" WHERE \"IncidentId\"='{incident['id']}' "
         "AND \"Decision\"='approved';"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    assert approval_rows == "maintenance@site12.demo", \
        f"Approvals table must record the JWT identity, got {approval_rows!r}"

    # ------------------------------------------------------------------
    # Prompt 5: the Action Agent executes the approved recommendation on
    # the mock ERP/CMMS. The incident must reach action_taken with a REAL
    # work-order id that exists on the CMMS -- no manual DB edits.
    # ------------------------------------------------------------------
    deadline = time.monotonic() + 60
    action_body = None
    while time.monotonic() < deadline:
        action = httpx.get(f"{MIDDLEWARE}/incidents/{incident['id']}/action",
                           headers=headers, timeout=10)
        action.raise_for_status()
        action_body = action.json()
        succeeded = [op for op in action_body["operations"] if op["outcome"] == "succeeded"]
        if action_body["status"] == "action_taken" and succeeded:
            break
        time.sleep(2.0)
    assert action_body["status"] == "action_taken", \
        f"expected action_taken, got {action_body['status']}"
    succeeded = [op for op in action_body["operations"] if op["outcome"] == "succeeded"]
    work_orders = [op for op in succeeded if op["operation"] == "work_order"]
    assert work_orders, "no work order was created on the CMMS"
    work_order_id = work_orders[0]["reference"]
    assert work_order_id, "the succeeded work_order row carries no CMMS reference"

    # The work order exists on the (deliberately frontend-unreachable) CMMS
    # and names this incident as its source.
    wo = _cmms_get(work_order_id)
    assert wo["status"] == "open"
    assert wo["sourceIncidentId"] == incident["id"]

    # The external-write audit chain exists (approved_by -> executed -> ref).
    audit_action = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "pgai",
         "-d", "processguard", "-tAc",
         f"SELECT COUNT(*) FROM \"AuditLog\" WHERE \"IncidentId\"='{incident['id']}' "
         "AND \"Action\"='action_executed';"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    assert audit_action == "1", f"expected one action_executed audit row, got {audit_action}"

    # ------------------------------------------------------------------
    # Resolve the work order on the CMMS (the real fulfillment path):
    # the Action Agent's resolution poll must close the loop -- incident
    # -> resolved AND a new HistoricalIncidents row (episodic memory).
    # ------------------------------------------------------------------
    resolve = _cmms_post(f"/work-orders/{work_order_id}/resolve",
                         {"resolution_note": "Suction strainer cleared; pump vibration back within range."})
    assert resolve["status"] == "resolved"

    deadline = time.monotonic() + 45
    learned = None
    while time.monotonic() < deadline:
        detail6 = httpx.get(f"{MIDDLEWARE}/incidents/{incident['id']}", headers=headers, timeout=10)
        detail6.raise_for_status()
        if detail6.json()["status"] == "resolved":
            break
        time.sleep(3.0)  # the poll runs every RESOLVE_POLL_SECONDS
    assert detail6.json()["status"] == "resolved", \
        "incident was not resolved after the CMMS work order was resolved"

    hist_rows = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "pgai",
         "-d", "processguard", "-tAc",
         f"SELECT COUNT(*) FROM \"HistoricalIncidents\" WHERE \"Source\" LIKE "
         f"'%{incident['id'][:8]}%';"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    assert hist_rows == "1", \
        f"expected exactly one learned historical row for this incident, got {hist_rows}"

    # The learned row is retrievable by the NEXT diagnosis: symptom text must
    # include the flow/vibration signature (Knowledge Agent's match criteria).
    learned = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "pgai",
         "-d", "processguard", "-tAc",
         f"SELECT \"ActionTaken\" FROM \"HistoricalIncidents\" WHERE \"Source\" LIKE "
         f"'%{incident['id'][:8]}%';"],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    assert "inspect" in learned.lower(), \
        f"learned record must carry the action taken: {learned!r}"

    # ------------------------------------------------------------------
    # Idempotency at the action layer: re-approving is impossible (409),
    # and the state history ends with the documented execution tail.
    # ------------------------------------------------------------------
    hist5 = httpx.get(f"{MIDDLEWARE}/incidents/{incident['id']}/state-history",
                      headers=headers, timeout=10)
    hist5.raise_for_status()
    to_statuses = [t["to"] for t in hist5.json()["transitions"]]
    assert to_statuses[-4:] == ["awaiting_approval", "approved", "action_taken", "resolved"], \
        f"state machine must end awaiting_approval -> approved -> action_taken -> resolved: {to_statuses}"


def _cmms_get(path_or_id: str) -> dict:
    """Read the mock CMMS from the test host via the agent-service container:
    the CMMS is on an isolated network with no published ports, so tests go
    through the one container that may reach it (which doubles as proof of
    the network isolation)."""
    path = path_or_id if path_or_id.startswith("/") else f"/work-orders/{path_or_id}"
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "agent-service", "python", "-c",
         "import sys,httpx;"
         "r=httpx.get('http://mock-erp-cmms:8100'+sys.argv[1],"
         "headers={'X-Api-Key':'cmms-dev-key-change-me'},timeout=10);"
         "sys.stdout.write(r.text)",
         path],
        check=True, capture_output=True, text=True, timeout=60,
    )
    return _loads(out.stdout)


def _cmms_post(path: str, body: dict) -> dict:
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "agent-service", "python", "-c",
         "import sys,json,httpx;"
         "r=httpx.post('http://mock-erp-cmms:8100'+sys.argv[1],"
         "json=json.loads(sys.argv[2]),"
         "headers={'X-Api-Key':'cmms-dev-key-change-me'},timeout=10);"
         "sys.stdout.write(r.text)",
         path, json.dumps(body)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    return _loads(out.stdout)


def test_reset_returns_simulator_to_normal(headers) -> None:
    response = httpx.post(f"{MIDDLEWARE}/simulator/reset", headers=headers, timeout=10)
    response.raise_for_status()
    assert response.json()["reset"] is True
