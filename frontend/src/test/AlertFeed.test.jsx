import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AlertFeed from '../components/AlertFeed.jsx'

vi.mock('../lib/api.js', () => ({
  api: {
    incidents: vi.fn(),
    triggerScenario: vi.fn(),
    resetSimulator: vi.fn(),
  },
}))

vi.mock('../context/AuthContext.jsx', () => ({
  useSiteName: () => 'Site 12 — Paper Mill Cooling Tower',
  // PlantManager by default: the demo controls render for this role.
  useAuth: vi.fn(() => ({ user: 'manager@site12.demo', role: 'PlantManager', maxApprovalLevel: 3 })),
}))

import { api } from '../lib/api.js'

const BASE_INCIDENT = {
  id: 'inc-1',
  equipmentId: 'eq-1',
  equipmentName: 'Cooling Water Pump CP-04',
  severity: 'high',
  detectedAt: new Date(Date.now() - 30_000).toISOString(),
  anomalySummary: {
    sensors: [
      { sensor_type: 'temperature', value: 38.1, unit: '°C', normal_min: 29, normal_max: 32 },
      { sensor_type: 'flow_rate', value: 6.2, unit: 'm³/h', normal_min: 7.5, normal_max: 9.5 },
    ],
  },
}

function listResponse(items) {
  return { items, total: items.length, limit: 50, offset: 0 }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('AlertFeed', () => {
  it('renders incidents with distinct, text-labeled statuses', async () => {
    api.incidents.mockResolvedValue(
      listResponse([
        { ...BASE_INCIDENT, id: 'a', status: 'awaiting_approval' },
        { ...BASE_INCIDENT, id: 'b', status: 'action_taken' },
        { ...BASE_INCIDENT, id: 'c', status: 'resolved', anomalySummary: { sensors: [] } },
        { ...BASE_INCIDENT, id: 'd', status: 'action_failed' },
      ]),
    )

    render(<AlertFeed onOpenIncident={vi.fn()} />)

    // Status must be readable as TEXT (accessibility: never color alone).
    await waitFor(() =>
      expect(screen.getByTestId('status-awaiting_approval')).toHaveTextContent('AWAITING APPROVAL'),
    )
    expect(screen.getByTestId('status-action_taken')).toHaveTextContent('ACTION TAKEN')
    expect(screen.getByTestId('status-resolved')).toHaveTextContent('RESOLVED')
    expect(screen.getByTestId('status-action_failed')).toHaveTextContent('ACTION FAILED')
    expect(screen.getAllByTestId('incident-card')).toHaveLength(4)
    // Equipment + severity are visible on the card.
    expect(screen.getAllByText('Cooling Water Pump CP-04').length).toBeGreaterThan(0)
    expect(screen.getAllByText('HIGH').length).toBeGreaterThan(0)
    // Active count excludes resolved incidents.
    expect(screen.getByText(/3 active · 4 total/)).toBeInTheDocument()
  })

  it('shows the empty state and enabled demo controls when the site is normal', async () => {
    api.incidents.mockResolvedValue(listResponse([]))

    render(<AlertFeed onOpenIncident={vi.fn()} />)

    await waitFor(() => expect(screen.getByText(/No incidents/)).toBeInTheDocument())
    expect(screen.getByTestId('trigger-scenario')).toBeEnabled()
    expect(screen.getByTestId('reset-simulator')).toBeEnabled()
  })

  it('hides the simulator controls from an Operator (no simulator capability)', async () => {
    const { useAuth } = await import('../context/AuthContext.jsx')
    useAuth.mockReturnValue({
      user: 'operator@site12.demo',
      role: 'Operator',
      maxApprovalLevel: 0,
    })
    api.incidents.mockResolvedValue(listResponse([]))

    render(<AlertFeed onOpenIncident={vi.fn()} />)

    await waitFor(() => expect(screen.getByText(/No incidents/)).toBeInTheDocument())
    expect(screen.queryByTestId('trigger-scenario')).not.toBeInTheDocument()
    expect(screen.queryByTestId('reset-simulator')).not.toBeInTheDocument()
  })

  it('opens the investigation timeline when an incident card is clicked', async () => {
    api.incidents.mockResolvedValue(listResponse([{ ...BASE_INCIDENT, status: 'open' }]))
    const onOpen = vi.fn()

    render(<AlertFeed onOpenIncident={onOpen} />)
    await waitFor(() => expect(screen.getAllByTestId('incident-card')[0]).toBeInTheDocument())

    await userEvent.click(screen.getAllByTestId('incident-card')[0])
    expect(onOpen).toHaveBeenCalledWith('inc-1')
  })

  it('reflects a status change on a later poll without any manual refresh', async () => {
    api.incidents
      .mockResolvedValueOnce(listResponse([{ ...BASE_INCIDENT, status: 'open' }]))
      .mockResolvedValue(listResponse([{ ...BASE_INCIDENT, status: 'awaiting_approval' }]))

    render(<AlertFeed onOpenIncident={vi.fn()} />)
    await waitFor(() => expect(screen.getByTestId('status-open')).toBeInTheDocument())

    // The poller fires every 4s; wait past the next tick.
    await waitFor(
      () => expect(screen.getByTestId('status-awaiting_approval')).toBeInTheDocument(),
      { timeout: 6000 },
    )
  })
})
