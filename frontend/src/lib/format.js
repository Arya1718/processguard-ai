// Shared display helpers. Status handling is centralized here so color and
// text always change together -- accessibility rule: never color alone
// (StatusBadge always renders a text label; colors are additional signal).

export const STATUS_META = {
  open: { label: 'OPEN', tone: 'info' },
  investigating: { label: 'INVESTIGATING', tone: 'info' },
  risk_assessed: { label: 'RISK ASSESSED', tone: 'info' },
  recommended: { label: 'RECOMMENDED', tone: 'info' },
  awaiting_approval: { label: 'AWAITING APPROVAL', tone: 'warn' },
  auto_informed: { label: 'AUTO-INFORMED', tone: 'ok' },
  approved: { label: 'APPROVED', tone: 'ok' },
  action_taken: { label: 'ACTION TAKEN', tone: 'ok' },
  action_failed: { label: 'ACTION FAILED', tone: 'danger' },
  rejected: { label: 'REJECTED', tone: 'danger' },
  needs_human_review: { label: 'NEEDS HUMAN REVIEW', tone: 'warn' },
  resolved: { label: 'RESOLVED', tone: 'muted' },
}

export function statusMeta(status) {
  return STATUS_META[status] || { label: String(status || 'UNKNOWN').toUpperCase(), tone: 'muted' }
}

export const TONE_CLASS = {
  info: 'tone-info',
  ok: 'tone-ok',
  warn: 'tone-warn',
  danger: 'tone-danger',
  muted: 'tone-muted',
}

export function timeAgo(iso) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ${minutes % 60}m ago`
  return `${Math.floor(hours / 24)}d ${hours % 24}h ago`
}

export function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString()
}

// The anomaly summary flags sensors as `sensor_type` (e.g. flow_rate); the
// UI shows them as human words. Simple, no dependencies.
export function sensorTitle(sensorType) {
  if (!sensorType) return 'Sensor'
  const words = String(sensorType).replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

export function formatReading(value, unit) {
  if (value === null || value === undefined) return '--'
  const num = Number(value)
  const text = Number.isFinite(num) ? num.toFixed(1) : String(value)
  return unit ? `${text} ${unit}` : text
}

// One equipment is seeded in the demo site; more may exist later.
export function equipmentTitle(equipmentId, incident) {
  if (incident?.equipmentName) return incident.equipmentName
  if (!equipmentId) return 'Unknown equipment'
  return 'Equipment'
}

export function severityTone(severity) {
  switch (String(severity || '').toLowerCase()) {
    case 'high':
      return 'tone-danger'
    case 'medium':
      return 'tone-warn'
    case 'low':
      return 'tone-ok'
    default:
      return 'tone-muted'
  }
}

// Historical-incident symptom patterns arrive in several shapes depending on
// their origin: seeded rows carry a string or a list of strings, learned rows
// (closed via the learning loop) carry { pattern, sensor_count }. Render all
// of them as readable text -- never as a raw object.
export function formatSymptoms(symptoms) {
  if (symptoms == null) return ''
  if (typeof symptoms === 'string') return symptoms
  if (Array.isArray(symptoms)) return symptoms.join(', ')
  if (typeof symptoms === 'object') {
    const parts = []
    if (symptoms.pattern) parts.push(String(symptoms.pattern))
    if (symptoms.sensor_count != null) parts.push(`${symptoms.sensor_count} sensor(s)`)  
    return parts.join(' — ')
  }
  return String(symptoms)
}
