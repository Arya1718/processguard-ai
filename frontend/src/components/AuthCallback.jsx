import { useEffect, useRef, useState } from 'react'
import { useAuth } from '../context/AuthContext.jsx'
// NOTE: aliased -- the AuthContext ALSO exposes a no-arg completeLogin() that
// merely re-reads the session; the exchange function here is oidc.js's.
import { completeLogin as exchangeCodeForTokens } from '../lib/oidc.js'
import { OIDC } from '../lib/config.js'

// /auth/callback: the identity provider redirects here with ?code=...
// after sign-in. The code is exchanged (with the PKCE verifier) for tokens;
// AuthContext picks up the session and the app renders the authenticated UI.
export default function AuthCallback() {
  const { completeLogin: finishLogin } = useAuth()
  const [error, setError] = useState(null)
  const ran = useRef(false)

  useEffect(() => {
    if (ran.current) return // StrictMode double-invoke guard
    ran.current = true
    exchangeCodeForTokens({ authority: OIDC.authority, clientId: OIDC.clientId })
      .then((returnTo) => {
        // Replace the callback URL, then TELL React the path changed:
        // a bare history.replaceState does not re-render anything (no
        // popstate fires), which would leave "Completing sign-in…"
        // on screen forever even after the session is stored.
        window.history.replaceState({}, '', returnTo || '/')
        window.dispatchEvent(new PopStateEvent('popstate'))
        finishLogin()
      })
      .catch((err) => setError(err.message || 'sign-in failed'))
  }, [finishLogin])

  return (
    <div className="login-screen">
      <div className="login-brand">
        <span className="brand-mark brand-mark-large" aria-hidden="true">PG</span>
        <h1>ProcessGuard AI</h1>
        {error ? (
          <>
            <p className="error" data-testid="callback-error">{error}</p>
            <a className="btn-secondary" href="/">Back to sign in</a>
          </>
        ) : (
          <p className="subtitle" data-testid="callback-working">Completing sign-in…</p>
        )}
      </div>
    </div>
  )
}
