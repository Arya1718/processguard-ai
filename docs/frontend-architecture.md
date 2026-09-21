# Frontend Architecture (Prompt 6)

The React surface: four screens/panels, deliberately decomposed, state
management boring on purpose. Everything talks to the .NET middleware — the
frontend never calls the Python agent-service or the mock CMMS directly.

## Component tree

```
main.jsx
└── AuthProvider (context: token, user, site)
    └── App.jsx                      — auth gate + feed/timeline view switch
        ├── LoginForm                — OIDC sign-in (auth-code + PKCE via lib/oidc.js)
        ├── AuthCallback             — /auth/callback: code exchange, then feed
        └── AppHeader                — user, role, site, logout (persistent)
        └── AlertFeed                — screen 1 (default landing after login)
        │   ├── TriggerBar           — trigger-scenario / reset (dev controls)
        │   └── IncidentCard[]       — status badge, severity, time since
        └── Timeline                 — screen 2 (per-incident)
            ├── StateHistoryStrip    — the state machine, chronological
            ├── DetectionPanel       — sensors vs normal range (CSS bars)
            ├── EvidencePanel        — SOPs + historical matches
            │   └── EvidenceCitation — shared citation chip (one style)
            ├── RootCausePanel       — hypothesis + confidence + cited points
            ├── RiskPanel            — severity, consequences, HITL + why
            ├── RecommendationPanel  — actions, rationale, tool-call log
            ├── ApprovalControls     — approve/reject (HITL gate)
            ├── ActionPanel          — CMMS ops, resolve control
            └── ChatPanel            — "Why?" grounded Q&A
```

Decomposition rule: one panel per pipeline stage, so each future agent
(Prompt 7's RBAC changes, new stages) touches one component.

## State management

Deliberately no Redux/Zustand/React Query — this surface is small enough
that the simple tools are the *right* tools:

- **AuthContext** (`src/context/AuthContext.jsx`) holds the JWT, the
  username, and the demo site. Every fetch reads the token from here via
  the api layer.
- **Everything else** is local `useState`/`useEffect` per view, fed by a
  single shared polling hook (`src/lib/usePolling.js`).

## Data flow and polling strategy

All requests go through `src/lib/api.js`:

- attaches `Authorization: Bearer <token>` from AuthContext,
- attaches/propagates `X-Correlation-Id` so a browser-originated request is
  traceable through middleware → agent-service → CMMS (the Prompt 1 chain),
- on a 401 it clears the session and the auth gate renders the login screen
  — token expiry is a redirect, never a blank screen or a silent failure.

Polling (per the prompt: no WebSockets/SSE this prompt):

| What | Endpoint | Interval |
|---|---|---|
| Alert feed | `GET /api/v1/incidents` | 4s (feed view only) |
| Incident detail + state history | `GET .../incidents/{id}` (detail carries action/approvals; `/state-history` for the strip) | 4s (timeline view only) |

Both pollers stop when the view unmounts. The e2e relies on this: the
incident appears and every stage panel populates with no user interaction
beyond clicking.

**If this became WebSocket/SSE later** (a fair judge question): the api
layer already centralizes auth, correlation IDs, and error handling, so the
change is localized to the *transport* of the two read paths — swap the
`usePolling` hook for an `EventSource`/WS subscription that applies the same
`setIncidents`/`setIncident` state updates. Backend side, the agent-service
would push on the same events the Detection/Orchestrator agents already
publish on the EventBus (the Service-Bus stand-in), so no agent logic
changes. Nothing in the component tree needs to know.

## Status model (one source of truth)

`src/lib/format.js` maps every incident status to `{ label, className }`;
`StatusBadge` renders both a color class and an explicit text label — color
is never the only signal (accessibility requirement). Statuses:
`open`, `investigating`, `risk_assessed`, `recommended`,
`awaiting_approval`, `approved`, `rejected`, `auto_informed`,
`action_taken`, `action_failed`, `resolved`.

## The "Why?" panel's grounding contract

`ChatPanel` posts to `POST /api/v1/incidents/{id}/ask`, which the
agent-service answers **only** from the incident's already-recorded data
(anomaly summary, evidence, root cause, risk, recommendation) and returns a
structured answer; the panel reuses `EvidenceCitation` so chat citations are
pixel-identical to root-cause citations. Questions outside the recorded
evidence get an explicit "I don't have evidence for that" — the same
citation-enforcement contract as the Root-Cause Agent, enforced server-side.

## Tests

- Component tests (Vitest + Testing Library, `src/test/`): AlertFeed renders
  incidents correctly by status; ApprovalControls calls the right endpoint
  and disables itself after a decision; EvidenceCitation displays the source
  correctly. `npm test`
- E2E (Playwright, `frontend/e2e/full-demo.spec.js`): login → trigger from
  the UI → incident appears via polling → full timeline populates → approve
  → CMMS reference appears → grounded chat answer → resolve → resolved.
  Runs against the real compose stack; `npm run e2e` (see
  `docs/demo-script.md`, which is derived from this file).
  Browsers install to `D:` via `PLAYWRIGHT_BROWSERS_PATH` per the local
  storage convention.
