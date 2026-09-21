import { formatReading, sensorTitle } from '../lib/format.js'

// Static seeded normal bands, used only as a fallback for incidents recorded
// before the Detection Agent started embedding normal_min/normal_max in the
// anomaly summary (and for hand-seeded records). Matches the Sensors seed.
const STATIC_THRESHOLDS = {
  temperature: { min: 29, max: 32 },
  ph: { min: 7.5, max: 8.2 },
  conductivity: { min: 950, max: 1100 },
  flow_rate: { min: 118, max: 132 },
  vibration: { min: 0, max: 2.5 },
}

// Detection stage: which sensors, values vs normal range. Rendered as a
// clear before/after bar (reading position within the normal band) plus the
// margin numbers -- deliberately no charting library, this is information
// density for an operator, not a dashboard toy.
export default function DetectionPanel({ anomalySummary }) {
  const sensors = anomalySummary?.sensors || []
  if (sensors.length === 0) {
    return <p className="muted">Anomaly summary not available yet.</p>
  }

  return (
    <ul className="sensor-list" data-testid="detection-sensors">
      {sensors.map((s) => {
        // Prefer the band recorded with the reading; fall back to the static
        // seeded thresholds for older incidents.
        const fallback = STATIC_THRESHOLDS[s.sensor_type] || {}
        const min = Number.isFinite(Number(s.normal_min)) ? Number(s.normal_min) : Number(fallback.min)
        const max = Number.isFinite(Number(s.normal_max)) ? Number(s.normal_max) : Number(fallback.max)
        const value = Number(s.value)
        const span = Number.isFinite(min) && Number.isFinite(max) && max > min ? max - min : 0
        // Position within the band (can exceed 0-100% when out of range --
        // that is exactly the point being visualized).
        const pct = span > 0 ? Math.round(((value - min) / span) * 100) : null
        const marginPct =
          span > 0 && Number.isFinite(value)
            ? Math.abs(Math.round(((value - (value < min ? min : max)) / span) * 100))
            : null

        return (
          <li key={`${s.sensor_type}-${s.value}`} className="sensor-row">
            <div className="sensor-row-head">
              <strong>{sensorTitle(s.sensor_type)}</strong>
              <span className="sensor-values">
                {formatReading(s.value, s.unit)} · normal {formatReading(min, s.unit)}–
                {formatReading(max, s.unit)}
              </span>
            </div>
            {pct !== null && (
              <div
                className={`sensor-bar ${value < min || value > max ? 'sensor-bar-out' : ''}`}
                title={`${sensorTitle(s.sensor_type)}: ${formatReading(s.value, s.unit)} (normal ${formatReading(min, s.unit)}–${formatReading(max, s.unit)})`}
              >
                <span className="sensor-band" />
                <span
                  className="sensor-marker"
                  style={{ left: `${Math.min(130, Math.max(-30, pct))}%` }}
                />
              </div>
            )}
            <div className="sensor-why">
              {s.reason || 'outside normal range'}
              {marginPct !== null && s.reason && s.reason.includes('static') ? null : null}
              {typeof s.static_margin === 'number' && s.static_margin > 0
                ? ` · ${s.static_margin.toFixed(2)}x static margin`
                : ''}
              {typeof s.drift_z === 'number' && s.drift_z >= 3
                ? ` · drift z=${s.drift_z.toFixed(1)}`
                : ''}
            </div>
          </li>
        )
      })}
    </ul>
  )
}
