import { useState } from 'react'
import { api } from '../lib/api.js'
import { formatTime } from '../lib/format.js'

// Action stage (Prompt 5): what the Action Agent executed on the mock
// ERP/CMMS. Shows the work-order reference (the moment the loop visibly
// closes), per-operation outcomes, and the resolve control when the work is
// still open. A resolve writes a new HistoricalIncidents row -- the learning
// loop made visible.
export default function ActionPanel({ incident, onResolved }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [note, setNote] = useState('')
  const [showResolve, setShowResolve] = useState(false)

  if (!incident.action) {
    if (incident.status === 'approved') {
      return <p className="muted">Action Agent executing — work order being created on the CMMS…</p>
    }
    return <p className="muted">No action executed yet.</p>
  }

  const action = incident.action
  const openWorkOrder = (action.operations || []).find(
    (op) =>
      op.operation === 'work_order' &&
      (op.outcome === 'succeeded' || op.outcome === 'ok') &&
      op.reference &&
      !action.resolvedAt,
  )

  const resolve = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.resolve(incident.id, note.trim() || undefined)
      setShowResolve(false)
      setNote('')
      onResolved?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="action-panel" data-testid="action-panel">
      <p className="action-line">
        Executed {formatTime(action.actionExecutedAt)} on the mock ERP/CMMS
        {action.resolvedAt ? ` · resolved ${formatTime(action.resolvedAt)}` : ''}
      </p>

      {(action.operations || []).length === 0 && <p className="muted">No operations recorded.</p>}
      <ul className="action-ops">
        {(action.operations || []).map((op, idx) => (
          <li key={idx} className={`action-op op-${op.outcome}`}>
            <div className="action-op-head">
              <code className="op-kind">{op.operation}</code>
              <span className={`op-outcome ${op.outcome === 'succeeded' || op.outcome === 'ok' ? 'ok' : 'failed'}`}>
                {op.outcome === 'succeeded' || op.outcome === 'ok'
                  ? 'SUCCESS'
                  : op.outcome === 'pending'
                    ? 'PENDING'
                    : 'FAILED'}
              </span>
            </div>
            <div>{op.description}</div>
            {op.reference && (
              <div className="op-reference">
                CMMS reference: <code data-testid="cmms-reference">{op.reference}</code>
              </div>
            )}
            {op.detail && (
              <div className="op-detail">
                <code>{JSON.stringify(op.detail)}</code>
              </div>
            )}
            <div className="muted small">
              attempted {formatTime(op.attemptedAt)}
              {op.completedAt ? ` · completed ${formatTime(op.completedAt)}` : ''}
            </div>
          </li>
        ))}
      </ul>

      {incident.status === 'action_failed' && (
        <p className="error">
          One or more CMMS operations failed after bounded retries; the incident is held at
          action_failed for human attention (nothing executed silently).
        </p>
      )}

      {openWorkOrder && (
        <div className="resolve-box">
          {!showResolve ? (
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setShowResolve(true)}
              data-testid="resolve-button"
            >
              Resolve work order…
            </button>
          ) : (
            <>
              <label htmlFor="resolve-note">Resolution note (recorded to site memory)</label>
              <textarea
                id="resolve-note"
                rows={2}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="What fixed it? This becomes the next incident's historical match."
              />
              <div className="reject-actions">
                <button
                  type="button"
                  className="btn-approve"
                  disabled={busy}
                  onClick={resolve}
                  data-testid="confirm-resolve"
                >
                  {busy ? 'Resolving…' : 'Confirm resolution'}
                </button>
                <button type="button" className="btn-secondary" onClick={() => setShowResolve(false)}>
                  Cancel
                </button>
              </div>
            </>
          )}
          {error && <p className="error">{error}</p>}
        </div>
      )}
    </div>
  )
}
