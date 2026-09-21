import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ApprovalControls from '../components/ApprovalControls.jsx'

vi.mock('../lib/api.js', () => ({
  api: {
    approve: vi.fn(),
    reject: vi.fn(),
  },
}))

// Role-aware auth mock (Prompt 7). Each test sets the session shape the
// AuthContext produces from validated OIDC claims: user, role, and the
// role's HITL approval ceiling (Operator 0 / MaintenanceEngineer 2 /
// PlantManager 3 -- see config/rbac-policy.json).
const authMock = { user: 'engineer@site12.demo', role: 'MaintenanceEngineer', maxApprovalLevel: 2 }

vi.mock('../context/AuthContext.jsx', () => ({
  useAuth: () => authMock,
}))

import { api } from '../lib/api.js'

const PENDING_L2 = { id: 'inc-1', status: 'awaiting_approval', hitlLevel: 2 }
const PENDING_L3 = { id: 'inc-1', status: 'awaiting_approval', hitlLevel: 3 }

beforeEach(() => {
  vi.clearAllMocks()
  authMock.user = 'engineer@site12.demo'
  authMock.role = 'MaintenanceEngineer'
  authMock.maxApprovalLevel = 2
})

describe('ApprovalControls', () => {
  it('calls POST /approve and reports the decision', async () => {
    api.approve.mockResolvedValue({
      incidentId: 'inc-1', decision: 'approved', status: 'approved', decidedBy: 'engineer@site12.demo',
    })
    const onDecided = vi.fn()

    render(<ApprovalControls incident={PENDING_L2} onDecided={onDecided} />)

    await userEvent.click(screen.getByTestId('approve-button'))

    await waitFor(() => expect(api.approve).toHaveBeenCalledWith('inc-1'))
    expect(onDecided).toHaveBeenCalled()
  })

  it('disables both buttons once a decision has been recorded', async () => {
    const decided = { id: 'inc-1', status: 'approved', approvals: [{ decidedBy: 'manager@site12.demo' }] }

    render(<ApprovalControls incident={decided} />)

    // Buttons are gone entirely; the decision record is shown instead.
    expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument()
    expect(screen.queryByTestId('reject-button')).not.toBeInTheDocument()
    expect(screen.getByTestId('decision-record')).toHaveTextContent('Approved by')
    expect(screen.getByTestId('decision-record')).toHaveTextContent('manager@site12.demo')
  })

  it('sends the justification on rejection', async () => {
    api.reject.mockResolvedValue({ incidentId: 'inc-1', decision: 'rejected', status: 'rejected' })

    render(<ApprovalControls incident={PENDING_L2} />)

    await userEvent.click(screen.getByTestId('reject-button'))
    await userEvent.type(screen.getByLabelText(/Justification/i), 'pump was serviced yesterday')
    await userEvent.click(screen.getByTestId('confirm-reject'))

    // The component passes the justification string; the api layer wraps it
    // into { justification } for the wire.
    await waitFor(() =>
      expect(api.reject).toHaveBeenCalledWith('inc-1', 'pump was serviced yesterday'),
    )
  })

  it('surfaces the API error when the decision is refused (e.g. 409)', async () => {
    api.approve.mockRejectedValue(new Error("incident status is 'action_taken'; only 'awaiting_approval' can be approved"))

    render(<ApprovalControls incident={PENDING_L2} />)

    await userEvent.click(screen.getByTestId('approve-button'))
    await waitFor(() =>
      expect(screen.getByText(/only 'awaiting_approval' can be approved/)).toBeInTheDocument(),
    )
  })

  it('hides the buttons from an Operator (maxApprovalLevel 0) and explains why', () => {
    authMock.role = 'Operator'
    authMock.maxApprovalLevel = 0

    render(<ApprovalControls incident={PENDING_L2} />)

    // UX only -- the backend is the real boundary and would 403 anyway.
    expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument()
    expect(screen.queryByTestId('reject-button')).not.toBeInTheDocument()
    expect(screen.getByTestId('approval-blocked-note')).toHaveTextContent('Operator')
    expect(screen.getByTestId('approval-blocked-note')).toHaveTextContent('HITL level 2')
  })

  it('blocks a MaintenanceEngineer from a HITL level 3 (Controlled Action) decision', () => {
    authMock.role = 'MaintenanceEngineer'
    authMock.maxApprovalLevel = 2

    render(<ApprovalControls incident={PENDING_L3} />)

    expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument()
    expect(screen.getByTestId('approval-blocked-note')).toHaveTextContent('HITL level 3')
  })

  it('lets a PlantManager decide a HITL level 3 recommendation', () => {
    authMock.user = 'manager@site12.demo'
    authMock.role = 'PlantManager'
    authMock.maxApprovalLevel = 3

    render(<ApprovalControls incident={PENDING_L3} />)

    expect(screen.getByTestId('approve-button')).toBeEnabled()
    expect(screen.getByTestId('reject-button')).toBeEnabled()
  })
})
