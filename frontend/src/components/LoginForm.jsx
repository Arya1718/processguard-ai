import { useEffect, useState } from 'react'
import { useAuth } from '../context/AuthContext.jsx'
import { loginRedirect } from '../lib/oidc.js'
import { OIDC } from '../lib/config.js'

// Sign-in screen (Prompt 7): a real "Sign in" button that redirects through
// the OIDC authorization-code + PKCE flow. The demo account picker only
// pre-fills the provider's login page -- the provider still authenticates
// the user; the UI never handles passwords.
const DEMO_ACCOUNTS = [
  { email: 'operator@site12.demo', label: 'Operator', site: 'Site 12' },
  { email: 'maintenance@site12.demo', label: 'Maintenance Engineer', site: 'Site 12' },
  { email: 'manager@site12.demo', label: 'Plant Manager', site: 'Site 12' },
  { email: 'operator@site07.demo', label: 'Operator', site: 'Site 07' },
]

export default function LoginForm() {
  const { user } = useAuth()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [preselect, setPreselect] = useState(null)

  // Pre-selecting an account deep-links into the provider's login page with
  // the email pre-filled (?email= on /authorize), so one click signs in.
  useEffect(() => {
    if (!preselect) return
    setBusy(true)
    loginRedirect({ authority: OIDC.authority, clientId: OIDC.clientId, email: preselect }).catch((err) => {
      setBusy(false)
      setError(err.message)
    })
  }, [preselect])

  const signIn = () => {
    setBusy(true)
    setError(null)
    loginRedirect({ authority: OIDC.authority, clientId: OIDC.clientId }).catch((err) => {
      setBusy(false)
      setError(err.message)
    })
  }

  return (
    <div className="card login-form" data-testid="login-card">
      <h2>Sign in</h2>
      <p className="muted">
        Authentication runs through the identity provider (OIDC). Your role and site
        determine what you can see and approve.
      </p>

      <button
        type="button"
        onClick={signIn}
        disabled={busy}
        data-testid="login-submit"
        style={{ width: '100%' }}
      >
        {busy ? 'Redirecting…' : 'Sign in'}
      </button>

      <p className="muted" style={{ marginTop: 18, fontSize: 12 }}>
        Demo accounts (click to pre-fill the provider login):
      </p>
      <ul className="demo-accounts" style={{ listStyle: 'none', padding: 0, margin: 0 }}>
        {DEMO_ACCOUNTS.map((account) => (
          <li key={account.email} style={{ marginBottom: 6 }}>
            <button
              type="button"
              className="btn-secondary"
              style={{ width: '100%', textAlign: 'left' }}
              disabled={busy}
              onClick={() => setPreselect(account.email)}
              data-testid={`login-as-${account.email}`}
            >
              <strong>{account.label}</strong> · {account.site} · {account.email}
            </button>
          </li>
        ))}
      </ul>

      {user && <p className="muted">Signed in as {user}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  )
}
