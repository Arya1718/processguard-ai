import EvidenceCitation from './EvidenceCitation.jsx'
import { formatSymptoms, formatTime } from '../lib/format.js'

// Evidence stage: what the Knowledge Agent retrieved -- SOP chunks with
// source citations and matched historical incidents with their outcomes.
// Reuses EvidenceCitation so SOP/history chips look identical everywhere.
export default function EvidencePanel({ evidence }) {
  if (!evidence) {
    return <p className="muted">Evidence has not been collected yet.</p>
  }
  const chunks = evidence.sop_chunks || []
  const history = evidence.matched_history || []

  return (
    <div className="evidence-panel" data-testid="evidence-panel">
      <h4 className="panel-subhead">SOP excerpts</h4>
      {chunks.length === 0 && <p className="muted">No SOP documents matched.</p>}
      {chunks.map((c) => (
        <div key={`${c.docId}-${c.section}`} className="evidence-item">
          <div className="evidence-item-head">
            <EvidenceCitation
              type="sop"
              source={c.docId}
              title={`${c.title} — ${c.section}`}
              sourceType={c.sourceType}
              sourceUrl={c.sourceUrl}
            />
            <span className="evidence-score" title="retrieval similarity score">
              match {(Number(c.score) * 100 || 0).toFixed(0)}%
            </span>
          </div>
          <blockquote className="evidence-excerpt">{c.excerpt}</blockquote>
        </div>
      ))}

      <h4 className="panel-subhead">Similar past incidents</h4>
      {history.length === 0 && <p className="muted">No matching historical incidents.</p>}
      {history.map((h) => (
        <div key={h.source || h.id} className="evidence-item">
          <div className="evidence-item-head">
            <EvidenceCitation
              type="history"
              source={h.source}
              title={`${h.equipmentName} — ${h.rootCause}`}
              sourceType={h.sourceType}
              sourceUrl={h.sourceUrl}
            />
            <span className="evidence-score">{formatTime(h.occurredAt)}</span>
          </div>
          <div className="evidence-body">
            <div>
              <span className="field-label">Symptoms:</span> {formatSymptoms(h.symptoms)}
            </div>
            <div>
              <span className="field-label">Root cause:</span> {h.rootCause}
            </div>
            <div>
              <span className="field-label">Resolution:</span> {h.resolution}
            </div>
            {h.outcome && (
              <div>
                <span className="field-label">Outcome:</span>{' '}
                <span className={`history-outcome ${h.outcome === 'worked' ? 'ok' : ''}`}>{h.outcome}</span>
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

