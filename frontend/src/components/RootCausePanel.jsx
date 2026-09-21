import EvidenceCitation, { resolvePointCitations } from './EvidenceCitation.jsx'

// Root-cause stage: hypothesis + confidence, with every evidence point
// visibly linked to what it cites. The citation chips carry the source in
// their tooltip and kind label -- the provenance story made clickable.
export default function RootCausePanel({ incident }) {
  const rootCause = incident.rootCause

  if (!rootCause) {
    return <p className="muted">Diagnosis in progress — no root cause recorded yet.</p>
  }

  // Honest insufficiency from the Root-Cause Agent is rendered as its own
  // state, never dressed up as a confident answer.
  if (rootCause.hypothesis && /insufficient evidence/i.test(rootCause.hypothesis)) {
    return (
      <p className="needs-review" data-testid="needs-review">
        {rootCause.hypothesis}
      </p>
    )
  }

  const evidence = incident.retrievedEvidence || {}
  const anomalySummary = incident.anomalySummary || { sensors: [] }

  return (
    <div className="root-cause" data-testid="root-cause">
      <p className="hypothesis">
        <span className="field-label">Most likely cause:</span> {rootCause.hypothesis}{' '}
        <span className="confidence" data-testid="confidence">
          confidence {rootCause.confidence != null ? `${rootCause.confidence}%` : '—'}
        </span>
      </p>
      <ul className="cited-points">
        {(rootCause.citedEvidence || []).map((point, idx) => {
          const chips = resolvePointCitations(point.text, evidence, anomalySummary)
          return (
            <li key={idx} className="cited-point">
              <span className="point-text">{point.text}</span>
              <span className="point-citations">
                {chips.length > 0 ? (
                  chips.map((c, i) => (
                    <EvidenceCitation
                      key={i}
                      type={c.type}
                      source={c.ref}
                      title={c.title}
                      sourceType={c.sourceType}
                      sourceUrl={c.sourceUrl}
                    />
                  ))
                ) : (
                  <EvidenceCitation type={point.type} source={point.ref} />
                )}
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
