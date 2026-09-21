import { useState } from 'react'
import IncidentCard from './IncidentCard.jsx'
import { api } from '../lib/api.js'
import { usePolling } from '../lib/usePolling.js'
import { useAuth, useSiteName } from '../context/AuthContext.jsx'

const POLL_MS = 4000

// Screen 1: Site Overview / Alert Feed. Polls GET /api/v1/incidents every
// few seconds so new incidents and status changes appear without a manual
// refresh. The demo controls (trigger + reset) render only for roles with
// simulator capability (RBAC table: MaintenanceEngineer + PlantManager) --
// UX convenience only; the backend returns 403 for anyone else.
export default function AlertFeed({ onOpenIncident }) {
  const { maxApprovalLevel } = useAuth()
  const canControlSimulator = maxApprovalLevel >= 2
  const [triggering, setTriggering] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [actionError, setActionError] = useState(null)
  const [actionNote, setActionNote] = useState(null)
  const siteName = useSiteName()

  const { data, error, loading } = usePolling(() => api.incidents({ limit: 50 }), POLL_MS)

  const runDemoAction = async (kind) => {
    setActionError(null)
    setActionNote(null)
    const busy = kind === 'trigger' ? setTriggering : setResetting
    busy(true)
    try {
      const result = kind === 'trigger' ? await api.triggerScenario() : await api.resetSimulator()
      setActionNote(
        kind === 'trigger'
          ? 'Scenario triggered — anomalous readings ramping now; an incident will appear here shortly.'
          : 'Simulator reset — readings returned to normal.',
      )
      return result
    } catch (err) {
      setActionError(err.message)
      return null
    } finally {
      busy(false)
    }
  }

  const incidents = data?.items || []
  const openCount = incidents.filter((i) => !['resolved', 'rejected'].includes(i.status)).length

  return (
    <section className="alert-feed" aria-label="Alert feed">
      <div className="feed-header">
        <div>
          <h2>Alert feed</h2>
          <p className="feed-sub">
            {siteName} · {openCount} active · {incidents.length} total
          </p>
        </div>
        <div className="feed-controls">
          {canControlSimulator && (
            <>
              <button
                type="button"
                className="btn-danger"
                onClick={() => runDemoAction('trigger')}
                disabled={triggering}
                data-testid="trigger-scenario"
              >
                {triggering ? 'Triggering…' : 'Trigger demo scenario'}
              </button>
              <button
                type="button"
                className="btn-secondary"
                onClick={() => runDemoAction('reset')}
                disabled={resetting}
                data-testid="reset-simulator"
              >
                {resetting ? 'Resetting…' : 'Reset simulator'}
              </button>
            </>
          )}
        </div>
      </div>

      {actionNote && <p className="action-note">{actionNote}</p>}
      {actionError && <p className="error">{actionError}</p>}
      {error && <p className="error">Live feed error: {error.message}</p>}
      {!error && loading && <p className="muted">Connecting to live feed…</p>}

      {incidents.length === 0 && !loading && !error && (
        <div className="card feed-empty">
          <p>
            No incidents. The site is running normally — use{' '}
            <strong>Trigger demo scenario</strong> to simulate the reference
            cooling-tower anomaly.
          </p>
        </div>
      )}

      <div className="incident-list">
        {incidents.map((incident) => (
          <IncidentCard key={incident.id} incident={incident} onOpen={onOpenIncident} />
        ))}
      </div>
    </section>
  )
}
