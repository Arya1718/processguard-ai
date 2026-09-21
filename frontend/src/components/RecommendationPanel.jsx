import { formatTime } from '../lib/format.js'

// Recommendation stage: specific actions, operator-readable rationale, and —
// deliberately prominent — the tool-call log. Showing "the AI checked
// maintenance status and similar past incidents before recommending this"
// is what proves the planner is real, so it renders expanded by default.
export default function RecommendationPanel({ incident }) {
  const rec = incident.recommendation
  if (!rec || (!rec.actions?.length && !rec.rationale)) {
    return <p className="muted">No recommendation generated yet.</p>
  }

  const toolLog = rec.toolCallLog || []

  return (
    <div className="recommendation-panel" data-testid="recommendation-panel">
      {rec.actions?.length > 0 && (
        <ul className="recommended-actions">
          {rec.actions.map((a, idx) => {
            const description =
              typeof a === 'string' ? a : a.description || a.action || a.summary || JSON.stringify(a)
            const detail = typeof a === 'object' && a ? a.detail || a.parameters || null : null
            return (
              <li key={idx}>
                <span className="action-text">{description}</span>
                {detail && (
                  <code className="action-detail">{JSON.stringify(detail)}</code>
                )}
              </li>
            )
          })}
        </ul>
      )}

      {rec.rationale && (
        <p className="rationale">
          <span className="field-label">Rationale:</span> {rec.rationale}
        </p>
      )}

      {toolLog.length > 0 && (
        <details className="tool-log" open data-testid="tool-log">
          <summary>
            Tool calls the agent made before recommending ({toolLog.length})
          </summary>
          <ol className="tool-log-list">
            {toolLog.map((call, idx) => (
              <li key={idx} className="tool-log-entry">
                <div className="tool-log-head">
                  <code className="tool-name">{call.tool || call.name}</code>
                  <span className="tool-when">{formatTime(call.at || call.timestamp)}</span>
                </div>
                <div className="tool-log-io">
                  <div>
                    <span className="field-label">args:</span>{' '}
                    <code>{JSON.stringify(call.arguments ?? call.args ?? {})}</code>
                  </div>
                  <div>
                    <span className="field-label">result:</span>{' '}
                    <code className="tool-result">
                      {truncate(JSON.stringify(call.result ?? call.response ?? {}))}
                    </code>
                  </div>
                </div>
              </li>
            ))}
          </ol>
        </details>
      )}

      {rec.hitlLevel && (
        <p className="muted">
          Recommendation is subject to HITL level {rec.hitlLevel} (set by the Risk Agent).
        </p>
      )}
      {rec.recommendedAt && (
        <p className="muted small">Recommended {formatTime(rec.recommendedAt)}</p>
      )}
    </div>
  )
}

function truncate(text, max = 220) {
  if (!text) return ''
  return text.length > max ? `${text.slice(0, max)}…` : text
}
