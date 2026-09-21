import { useState } from 'react'
import { api } from '../lib/api.js'
import EvidenceCitation, { resolvePointCitations } from './EvidenceCitation.jsx'

// The "Why?" panel (screen 3): a small chat attached to the incident detail
// view. Questions go to POST /api/v1/incidents/{id}/ask, which answers ONLY
// from the incident's recorded evidence (grounded Q&A endpoint, Prompt 6).
// Answers render with the SAME citation chips as the root-cause section --
// one citation UI everywhere, never a second style.
export default function ChatPanel({ incident }) {
  const [messages, setMessages] = useState([])
  const [question, setQuestion] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const send = async (event) => {
    event.preventDefault()
    const text = question.trim()
    if (!text || busy) return
    setBusy(true)
    setError(null)
    setQuestion('')
    setMessages((prev) => [...prev, { role: 'user', text }])
    try {
      const result = await api.ask(incident.id, text)
      setMessages((prev) => [...prev, { role: 'assistant', ...result }])
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const evidence = incident.retrievedEvidence || {}
  const anomalySummary = incident.anomalySummary || { sensors: [] }

  return (
    <div className="chat-panel" data-testid="chat-panel">
      {messages.length === 0 && (
        <p className="muted chat-empty">
          Ask why something happened. Answers come only from this incident's recorded evidence —
          if the record doesn't support an answer, the assistant says so.
        </p>
      )}

      <div className="chat-messages">
        {messages.map((m, idx) =>
          m.role === 'assistant' ? (
            <div key={idx} className="chat-message chat-assistant">
              <div className="chat-answer">{m.answer}</div>
              {(m.citations?.length > 0 ||
                (m.answer && !/i don't have evidence/i.test(m.answer))) && (
                <div className="chat-citations">
                  {(m.citations?.length > 0
                    ? m.citations
                    : resolvePointCitations(m.answer, evidence, anomalySummary)
                  ).map((c, i) => (
                    <EvidenceCitation key={i} type={c.type} source={c.ref} title={c.title} sourceType={c.sourceType} sourceUrl={c.sourceUrl} />
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div key={idx} className="chat-message chat-user">
              <div className="chat-question">{m.text}</div>
            </div>
          ),
        )}
        {busy && (
          <div className="chat-message chat-assistant muted">Checking the incident record…</div>
        )}
      </div>

      <form className="chat-form" onSubmit={send}>
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Why did the temperature rise?"
          aria-label="Ask a question about this incident"
          data-testid="chat-input"
        />
        <button type="submit" disabled={busy || !question.trim()} data-testid="chat-send">
          Ask
        </button>
      </form>
      {error && <p className="error">{error}</p>}
    </div>
  )
}
