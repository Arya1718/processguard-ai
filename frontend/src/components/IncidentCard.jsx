import StatusBadge from './StatusBadge.jsx'
import { equipmentTitle, severityTone, timeAgo } from '../lib/format.js'

// One incident in the alert feed. Status is always a text label + color;
// severity likewise. Clicking opens the Investigation Timeline.
export default function IncidentCard({ incident, onOpen }) {
  const sensors = incident.anomalySummary?.sensors || []

  return (
    <button
      type="button"
      className="incident-card"
      onClick={() => onOpen(incident.id)}
      data-testid="incident-card"
      data-incident-id={incident.id}
    >
      <div className="incident-card-main">
        <div className="incident-card-title">
          {equipmentTitle(incident.equipmentId, incident)}
          <span className={`severity ${severityTone(incident.severity)}`}>
            {String(incident.severity || 'unknown').toUpperCase()}
          </span>
        </div>
        <div className="incident-card-sub">
          {sensors.length > 0
            ? `${sensors.length} sensor${sensors.length === 1 ? '' : 's'} flagged: ${sensors
                .map((s) => String(s.sensor_type || '').replace('_', ' '))
                .join(', ')}`
            : 'Anomaly detected'}
        </div>
      </div>
      <div className="incident-card-side">
        <StatusBadge status={incident.status} />
        <span className="incident-time" title={incident.detectedAt}>
          detected {timeAgo(incident.detectedAt)}
        </span>
      </div>
    </button>
  )
}
