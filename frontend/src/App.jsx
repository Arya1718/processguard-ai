import { useEffect, useState } from 'react'
import { AuthProvider, useAuth } from './context/AuthContext.jsx'
import AppHeader from './components/AppHeader.jsx'
import AlertFeed from './components/AlertFeed.jsx'
import Timeline from './components/Timeline.jsx'
import LoginForm from './components/LoginForm.jsx'
import AuthCallback from './components/AuthCallback.jsx'

// Prompt 6 product surface + Prompt 7 real auth: the session comes from the
// OIDC flow (see lib/oidc.js); any 401 from the api layer clears it and lands
// back on the sign-in screen (graceful expiry handling).

function AuthenticatedApp() {
  // `null` = alert feed; an incident id = the investigation timeline.
  const [openIncidentId, setOpenIncidentId] = useState(null)

  return (
    <div className="app">
      <AppHeader />
      <main className="app-main">
        {openIncidentId ? (
          <Timeline incidentId={openIncidentId} onBack={() => setOpenIncidentId(null)} />
        ) : (
          <AlertFeed onOpenIncident={setOpenIncidentId} />
        )}
      </main>
    </div>
  )
}

function Shell() {
  const { token } = useAuth()
  const [path, setPath] = useState(window.location.pathname)

  // Track path changes (the callback route) without adding a router dep.
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname)
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  if (path === '/auth/callback') {
    return <AuthCallback />
  }
  return token ? <AuthenticatedApp /> : <LoginScreen />
}

function LoginScreen() {
  return (
    <div className="login-screen">
      <div className="login-brand">
        <span className="brand-mark brand-mark-large" aria-hidden="true">PG</span>
        <h1>ProcessGuard AI</h1>
        <p className="subtitle">Industrial anomaly investigation on the Ackumen platform</p>
      </div>
      <LoginForm />
    </div>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <Shell />
    </AuthProvider>
  )
}
