import { severityTone } from '../lib/format.js'

// Risk stage: severity, applicable consequences, and the deterministic HITL
// level. Includes the plain-language "why this level" explanation -- the
// safety decision (who gets involved) must be legible to the operator, not
// just shown as a number.
export default function RiskPanel({ incident }) {
  const risk = incident.risk
  if (!risk || !risk.severity) {
    return <p className="muted">Risk has not been assessed yet.</p>
  }

  const consequences = risk.consequences || []

  // Plain-language rendering of the deterministic mapping (docs/hitl-thresholds.md):
  // the level comes from severity AND confidence together, with a safety
  // floor -- low confidence can only raise the required oversight, never
  // lower it.
  const hitlWhy = (() => {
    const level = risk.hitlLevel
    const severity = String(risk.severity).toLowerCase()
    const confidence = incident.rootCause?.confidence
    if (level === 1) return 'Low severity with high confidence: the system informs automatically; no approval is needed.'
    if (level === 3)
      return 'High severity (or low confidence at high severity): a human must control the action and justify any rejection.'
    if (confidence != null && confidence < 70)
      return `Diagnosis confidence is only ${confidence}%, so human review is required regardless of severity.`
    return `Severity is ${severity}, so a human approves before any external action is taken.`
  })()

  return (
    <div className="risk-panel" data-testid="risk-panel">
      <p className="risk-line">
        <span className={`severity ${severityTone(risk.severity)}`}>
          {String(risk.severity).toUpperCase()}
        </span>
        <span className="risk-sep">·</span>
        <span>
          HITL level <strong data-testid="hitl-level">{risk.hitlLevel ?? '—'}</strong>
        </span>
        <span className="risk-sep">·</span>
        <span>assessed {risk.assessedAt ? new Date(risk.assessedAt).toLocaleString() : '—'}</span>
      </p>

      {consequences.length > 0 && (
        <>
          <h4 className="panel-subhead">Potential consequences</h4>
          <ul className="consequences">
            {consequences.map((c, idx) => {
              const name = c.consequence || c.name || (typeof c === 'string' ? c : '?')
              const why = c.basis || c.why
              return (
                <li key={idx}>
                  <strong>{name}</strong>
                  {why ? <span className="consequence-why"> — {why}</span> : null}
                </li>
              )
            })}
          </ul>
        </>
      )}

      <p className="hitl-why">
        <span className="field-label">Why this level:</span> {hitlWhy}
      </p>
    </div>
  )
}
