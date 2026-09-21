import { statusMeta, TONE_CLASS } from '../lib/format.js'

// Status is conveyed by a text label AND color -- never color alone, which is
// the legitimate accessibility point called out in the prompt.
export default function StatusBadge({ status }) {
  const meta = statusMeta(status)
  return (
    <span className={`badge ${TONE_CLASS[meta.tone]}`} data-testid={`status-${status}`}>
      {meta.label}
    </span>
)
}
