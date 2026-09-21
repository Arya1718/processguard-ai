import { sensorTitle } from '../lib/format.js'

// The single citation UI (reused by root cause AND the "Why?" panel -- never
// a second citation style). Type determines the glyph; the full source ref is
// always rendered as text (provenance must be readable, not decorative).
//
// Prompt 8 provenance: sourceType is "public_real" (a real, publicly
// published document -- badge + working link to sourceUrl) or "illustrative"
// (authored for this project). The badge makes the distinction visible in
// every citation; absent sourceType means illustrative (honest default).
export default function EvidenceCitation({ type, source, title, sourceType, sourceUrl }) {
  const glyph = type === 'sop' ? '§' : type === 'history' ? '⌛' : '•'
  const kindLabel =
    type === 'sop' ? 'SOP' : type === 'history' ? 'historical incident' : 'sensor reading'
  const label = source || title || kindLabel
  const provenance = sourceType === 'public_real' ? 'public' : 'illustrative'
  const href = sourceType === 'public_real' && sourceUrl ? sourceUrl : null
  return (
    <span
      className={`citation-chip citation-${type}${href ? ' citation-linked' : ''}`}
      title={title || `${kindLabel}: ${source || ''}`}
      data-testid="citation-chip"
      data-source-type={sourceType || 'illustrative'}
    >
      <span className="citation-glyph" aria-hidden="true">{glyph}</span>
      <span className="citation-ref">
        {href ? (
          <a className="citation-link" href={href} target="_blank" rel="noreferrer">
            {label}
          </a>
        ) : (
          label
        )}
      </span>
      <span className="citation-kind">{kindLabel}</span>
      <span
        className={`provenance-badge provenance-${provenance}`}
        data-testid="provenance-badge"
        title={
          provenance === 'public'
            ? 'public_real: cited from a real, publicly published document (working link)'
            : 'illustrative: authored for this project, not sourced from Buckman'
        }
      >
        {provenance}
      </span>
    </span>
  )
}

// Given a cited point's text (e.g. "flow_rate fell 18% [sensor reading]") and
// the incident's evidence, resolve which citation chips apply to it.
export function resolvePointCitations(point, evidence, anomalySummary) {
  const lowered = String(point || '').toLowerCase()
  const chips = []
  for (const chunk of evidence?.sop_chunks || []) {
    const id = String(chunk.docId || '')
    if (id && lowered.includes(id.toLowerCase())) {
      chips.push({
        type: 'sop',
        ref: id,
        title: `${chunk.title} — ${chunk.section}`,
        sourceType: chunk.sourceType,
        sourceUrl: chunk.sourceUrl,
      })
    }
  }
  for (const h of evidence?.matched_history || []) {
    const source = String(h.source || '')
    if (source && lowered.includes(source.toLowerCase())) {
      chips.push({
        type: 'history',
        ref: source,
        title: `${h.equipmentName} — ${h.rootCause}`,
        sourceType: h.sourceType,
        sourceUrl: h.sourceUrl,
      })
    }
  }
  for (const s of anomalySummary?.sensors || []) {
    const t = String(s.sensor_type || '')
    if (t && (lowered.includes(t.toLowerCase()) || lowered.includes(t.replace('_', ' ')))) {
      chips.push({
        type: 'sensor',
        ref: sensorTitle(t),
        title: `${sensorTitle(t)}: ${s.value}${s.unit || ''} (${s.reason || 'out of range'})`,
      })
    }
  }
  return chips
}
