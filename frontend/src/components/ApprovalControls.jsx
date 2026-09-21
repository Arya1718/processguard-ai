import { useState } from 'react'
import { api } from '../lib/api.js'
import { useAuth } from '../context/AuthContext.jsx'

// Approval stage controls (HITL levels 2-3). Buttons call the Prompt 4
// decision endpoints; once a decision is recorded the buttons disable for
// good and the decision metadata (who/when) shows instead.
export default function ApprovalControls({ incident, onDecided }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [showReject, setShowReject] = useState(false)
  const [justification, setJustification] = useState('')
  const { user } = useAuth()

  const decided = ['approved', 'rejected', 'action_taken', 'action_failed', 'resolved'].includes(
    incident.status,
  )
  const disabled = decided || busy

  // Role-aware UI (Prompt 7): an Operator's token caps at HITL level 0, so
  // the decision controls do not render at all. This is UX, NOT security --
  // the backend enforces the same rule (middleware policy + the agent
  // service's second-layer check) and returns 403 regardless of what the UI
  // shows. An incident whose required level exceeds the user's cap shows a
  // clear explanation instead of buttons.
  const { maxApprovalLevel, role } = useAuth()
  const requiredLevel = incident.hitlLevel ?? incident.requiredApprovalLevel ?? incident.risk?.hitlLevel ?? 2
  const allowedHere = maxApprovalLevel >= requiredLevel && requiredLevel >= 2

  const decide = async (kind) => {
    setBusy(true)
    setError(null)
    try {
      const result =
        kind === 'approve'
          ? await api.approve(incident.id)
          : await api.reject(incident.id, justification.trim() || undefined)
      setShowReject(false)
      setJustification('')
      onDecided?.(result)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (decided) {
    const decision = incident.approvals?.[0]
    return (
      <p className="decision-record" data-testid="decision-record">
        {incident.status === 'rejected' ? 'Rejected' : 'Approved'} by{' '}
        <strong>{decision?.decidedBy || user}</strong>
        {decision?.decidedAt ? ` · ${new Date(decision.decidedAt).toLocaleString()}` : ''}
      </p>
    )
  }

  if (!allowedHere) {
    return (
      <div className="approval-controls" data-testid="approval-blocked">
        <p className="muted" data-testid="approval-blocked-note">
          Your role ({role || 'none'}) cannot decide HITL level {requiredLevel} recommendations.
          Approval requires a Maintenance Engineer (level 2) or Plant Manager (level 3).
        </p>
      </div>
    )
  }

  return (
    <div className="approval-controls" data-testid="approval-controls">
      <div className="approval-buttons">
        <button
          type="button"
          className="btn-approve"
          disabled={disabled}
          onClick={() => decide('approve')}
          data-testid="approve-button"
        >
          {busy ? 'Working…' : 'Approve recommendation'}
        </button>
        <button
          type="button"
          className="btn-reject"
          disabled={disabled}
          onClick={() => setShowReject(true)}
          data-testid="reject-button"
        >
          Reject…
        </button>
      </div>
      <p className="approval-note">
        Approving lets the Action Agent execute these steps on the CMMS. Rejection asks for a
        justification (required at HITL level 3) and stops the pipeline.
      </p>
      {error && <p className="error">{error}</p>}
      {showReject && (
        <div className="reject-box">
          <label htmlFor="justification">Justification (required at HITL level 3)</label>
          <textarea
            id="justification"
            rows={2}
            value={justification}
            onChange={(e) => setJustification(e.target.value)}
            placeholder="Why is this recommendation being rejected?"
          />
          <div className="reject-actions">
            <button
              type="button"
              className="btn-reject"
              disabled={busy}
              onClick={() => decide('reject')}
              data-testid="confirm-reject"
            >
              Confirm rejection
            </button>
            <button type="button" className="btn-secondary" onClick={() => setShowReject(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
