import { useState } from 'react'
import StatusBadge from './StatusBadge.jsx'
import DetectionPanel from './DetectionPanel.jsx'
import EvidencePanel from './EvidencePanel.jsx'
import RootCausePanel from './RootCausePanel.jsx'
import RiskPanel from './RiskPanel.jsx'
import RecommendationPanel from './RecommendationPanel.jsx'
import ApprovalControls from './ApprovalControls.jsx'
import ActionPanel from './ActionPanel.jsx'
import ChatPanel from './ChatPanel.jsx'
import { api } from '../lib/api.js'
import { usePolling } from '../lib/usePolling.js'
import { equipmentTitle, formatTime, statusMeta, timeAgo } from '../lib/format.js'

// Screen 2: the Investigation Timeline -- the core demo screen. Polls the
// incident and its state history so every stage fills in live as the agents
// complete; the Prompt 4 state machine becomes visible, in order, with
// timestamps and the actor for human transitions.
export default function Timeline({ incidentId, onBack }) {
  const { data: incident, error: incidentError } = usePolling(
    () => api.incident(incidentId),
    4000,
    [incidentId],
  )
  const { data: history } = usePolling(() => api.stateHistory(incidentId), 4000, [incidentId])

  if (incidentError) {
    return (
      <section className="timeline">
        <button type="button" className="btn-secondary" onClick={onBack} data-testid="back-button">
          ← Back to alert feed
        </button>
        <p className="error">Failed to load incident: {incidentError.message}</p>
      </section>
    )
  }

  if (!incident) {
    return (
      <section className="timeline">
        <button type="button" className="btn-secondary" onClick={onBack}>← Back to alert feed</button>
        <p className="muted">Loading incident…</p>
      </section>
    )
  }

  const transitions = history?.transitions || []

  return (
    <section className="timeline" data-testid="timeline">
      <button type="button" className="btn-secondary" onClick={onBack} data-testid="back-button">
        ← Back to alert feed
      </button>

      <div className="timeline-header card">
        <div>
          <h2>{equipmentTitle(incident.equipmentId, incident)}</h2>
          <p className="muted">
            Detected {formatTime(incident.detectedAt)} ({timeAgo(incident.detectedAt)})
          </p>
        </div>
        <StatusBadge status={incident.status} />
      </div>

      {/* The state machine, rendered as the chronological spine of the view. */}
      <div className="state-strip card" data-testid="state-strip">
        <h3>Investigation workflow</h3>
        <ol className="state-list">
          {transitions.map((t, idx) => (
            <li key={idx} className="state-entry">
              <span className="state-arrow">→</span>
              <span className="state-to">{statusMeta(t.to).label}</span>
              <span className="state-when">{formatTime(t.changedAt)}</span>
              {t.actor && <span className="state-actor">by {t.actor}</span>}
              {t.reason && <span className="state-reason">({t.reason})</span>}
            </li>
          ))}
          {transitions.length === 0 && (
            <li className="muted">State history not recorded yet.</li>
          )}
        </ol>
      </div>

      <div className="timeline-grid">
        <div className="timeline-main">
          <StageCard title="1 · Detection" subtitle="Which sensors breached, and by how much">
            <DetectionPanel anomalySummary={incident.anomalySummary} />
          </StageCard>

          <StageCard title="2 · Evidence" subtitle="Retrieved SOPs and matched historical incidents">
            <EvidencePanel evidence={incident.retrievedEvidence} />
          </StageCard>

          <StageCard title="3 · Root cause" subtitle="Hypothesis with cited, verifiable evidence">
            <RootCausePanel incident={incident} />
          </StageCard>

          <StageCard title="4 · Risk" subtitle="Severity, consequences, human-in-the-loop level">
            <RiskPanel incident={incident} />
          </StageCard>

          <StageCard title="5 · Recommendation" subtitle="Actions, rationale, and the tool calls behind them">
            <RecommendationPanel incident={incident} />
          </StageCard>

          {(incident.status === 'awaiting_approval' ||
            ['approved', 'rejected', 'action_taken', 'action_failed', 'resolved'].includes(
              incident.status,
            )) && (
            <StageCard
              title="6 · Approval"
              subtitle="Human decision gate (HITL)"
              data-testid="stage-approval"
            >
              <ApprovalControls incident={incident} />
            </StageCard>
          )}

          {(incident.action ||
            ['approved', 'action_taken', 'action_failed', 'resolved'].includes(incident.status)) && (
            <StageCard title="7 · Action" subtitle="Executed on the ERP/CMMS by the Action Agent">
              <ActionPanel incident={incident} />
            </StageCard>
          )}
        </div>

        <aside className="timeline-side">
          <StageCard title="Why?" subtitle="Grounded Q&A on this incident">
            <ChatPanel incident={incident} />
          </StageCard>
        </aside>
      </div>
    </section>
  )
}

function StageCard({ title, subtitle, children, ...rest }) {
  return (
    <div className="card stage-card" {...rest}>
      <h3 className="stage-title">{title}</h3>
      {subtitle && <p className="stage-subtitle muted">{subtitle}</p>}
      {children}
    </div>
  )
}
