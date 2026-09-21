import { useAuth, useSiteName } from '../context/AuthContext.jsx'

// Persistent header (Prompt 6/7): the signed-in user's identity AND claims --
// role and site render as labeled badges so a reviewer can see exactly who is
// logged in and what the policy says they may do. The site comes from the
// token's site_name claim (falling back to the demo constant for tokens
// issued before Prompt 7).
const ROLE_LABELS = {
  Operator: 'Operator',
  MaintenanceEngineer: 'Maintenance Engineer',
  PlantManager: 'Plant Manager',
}

export default function AppHeader() {
  const { user, role, logout } = useAuth()
  const claimedSite = useSiteName()

  return (
    <header className="app-header">
      <div className="app-header-brand">
        <span className="brand-mark" aria-hidden="true">PG</span>
        <div>
          <div className="brand-title">ProcessGuard AI</div>
          <div className="brand-sub">Ackumen-powered anomaly investigation</div>
        </div>
      </div>
      <div className="app-header-site" title="Site assigned to your account">
        <span className="site-label">Site</span>
        <span className="site-name" data-testid="header-site">{claimedSite}</span>
      </div>
      <div className="app-header-user">
        <span className="user-label">Signed in as</span>
        <span className="user-name" data-testid="header-user">{user}</span>
        {role && (
          <span className="role-badge" data-testid="header-role" title="Role claim on your token">
            {ROLE_LABELS[role] || role}
          </span>
        )}
        <button type="button" className="btn-secondary" onClick={logout} data-testid="logout">
          Log out
        </button>
      </div>
    </header>
  )
}
