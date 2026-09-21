import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import EvidenceCitation, { resolvePointCitations } from '../components/EvidenceCitation.jsx'

describe('EvidenceCitation', () => {
  it('renders the source reference as visible text (not just color/icon)', () => {
    render(<EvidenceCitation type="sop" source="SOP-COOL-014" title="Cooling Water Flow and Vibration Response — Response" />)

    const chip = screen.getByTestId('citation-chip')
    // The source id AND its kind are both readable text on the chip.
    expect(chip).toHaveTextContent('SOP-COOL-014')
    expect(chip).toHaveTextContent('SOP')
  })

  it('renders historical-incident chips with the incident source', () => {
    render(<EvidenceCitation type="history" source="HIST-2026-0903" title="CP-04 — pump degradation" />)

    const chip = screen.getByTestId('citation-chip')
    expect(chip).toHaveTextContent('HIST-2026-0903')
    expect(chip).toHaveTextContent('historical incident')
  })

  it('falls back to the kind label when no ref is provided', () => {
    render(<EvidenceCitation type="sensor" source="Flow rate" />)

    expect(screen.getByTestId('citation-chip')).toHaveTextContent('Flow rate')
  })

  // Prompt 8 provenance: the public_real vs illustrative distinction must be
  // VISIBLE on every citation -- badge + working link for public sources.
  it('shows the public badge and a working link for a public_real citation', () => {
    render(
      <EvidenceCitation
        type="history"
        source="csb-gov:2004-9-I-GA-MFG"
        title="MFG Chemical reactor overheated"
        sourceType="public_real"
        sourceUrl="https://www.csb.gov/mfg-chemical-inc-toxic-gas-release/"
      />,
    )

    const chip = screen.getByTestId('citation-chip')
    expect(chip.getAttribute('data-source-type')).toBe('public_real')
    const badge = screen.getByTestId('provenance-badge')
    expect(badge).toHaveTextContent('public')
    const link = screen.getByRole('link')
    expect(link.getAttribute('href')).toBe('https://www.csb.gov/mfg-chemical-inc-toxic-gas-release/')
    expect(link).toHaveTextContent('csb-gov:2004-9-I-GA-MFG')
  })

  it('shows the illustrative badge without a link for authored SOPs', () => {
    render(<EvidenceCitation type="sop" source="SOP-COOL-014" sourceType="illustrative" />)

    expect(screen.getByTestId('citation-chip').getAttribute('data-source-type')).toBe('illustrative')
    expect(screen.getByTestId('provenance-badge')).toHaveTextContent('illustrative')
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('defaults to the honest illustrative provenance when sourceType is absent', () => {
    render(<EvidenceCitation type="sop" source="SOP-COOL-014" />)

    expect(screen.getByTestId('citation-chip').getAttribute('data-source-type')).toBe('illustrative')
    expect(screen.getByTestId('provenance-badge')).toHaveTextContent('illustrative')
  })

  it('propagates provenance through resolvePointCitations', () => {
    const evidence = {
      sop_chunks: [
        { docId: 'PUB-DOE-PUMP-SOURCEBOOK', title: 'DOE Pump Sourcebook', section: 'PUMP-MAINTENANCE', score: 0.6, sourceType: 'public_real', sourceUrl: 'https://www.energy.gov/sites/prod/files/2014/05/f16/pump.pdf' },
      ],
      matched_history: [
        { source: 'csb-gov:2004-9-I-GA-MFG', equipmentName: 'Process Reactor Tank', rootCause: 'cooling capacity lost', sourceType: 'public_real', sourceUrl: 'https://www.csb.gov/mfg-chemical-inc-toxic-gas-release/' },
      ],
    }
    const chips = resolvePointCitations(
      'Per PUB-DOE-PUMP-SOURCEBOOK maintenance guidance, matching csb-gov:2004-9-I-GA-MFG [SOP citation PUB-DOE-PUMP-SOURCEBOOK]',
      evidence,
      { sensors: [] },
    )
    const sop = chips.find((c) => c.type === 'sop')
    const hist = chips.find((c) => c.type === 'history')
    expect(sop.sourceType).toBe('public_real')
    expect(sop.sourceUrl).toContain('energy.gov')
    expect(hist.sourceType).toBe('public_real')
    expect(hist.sourceUrl).toContain('csb.gov')
  })
})

describe('resolvePointCitations', () => {
  const evidence = {
    sop_chunks: [
      { docId: 'SOP-COOL-014', title: 'Cooling Water Flow and Vibration Response', section: 'Response', score: 0.71 },
      { docId: 'SOP-CHEM-201', title: 'Cooling-Loop Chemistry Excursion', section: 'Correction', score: 0.41 },
    ],
    matched_history: [
      { source: 'HIST-2026-0903', equipmentName: 'Cooling Water Pump CP-04', rootCause: 'pump degradation', outcome: 'worked' },
    ],
  }
  const anomalySummary = {
    sensors: [
      { sensor_type: 'flow_rate', value: 6.2, unit: 'm³/h' },
      { sensor_type: 'vibration', value: 9.1, unit: 'mm/s' },
    ],
  }

  it('links an SOP-cited point to the SOP chip', () => {
    const chips = resolvePointCitations(
      'Inspect suction per SOP-COOL-014 filter procedure [SOP citation SOP-COOL-014]',
      evidence,
      anomalySummary,
    )
    expect(chips.some((c) => c.type === 'sop' && c.ref === 'SOP-COOL-014')).toBe(true)
    expect(chips.some((c) => c.type === 'sop' && c.ref === 'SOP-CHEM-201')).toBe(false)
  })

  it('links a sensor point to the flagged sensor', () => {
    const chips = resolvePointCitations(
      'flow_rate fell to 6.2 m³/h [sensor reading]',
      evidence,
      anomalySummary,
    )
    expect(chips.some((c) => c.type === 'sensor' && c.ref === 'Flow rate')).toBe(true)
  })

  it('links a history point to the matched incident', () => {
    const chips = resolvePointCitations(
      'Same signature as HIST-2026-0903 which was resolved by servicing the pump [historical incident HIST-2026-0903]',
      evidence,
      anomalySummary,
    )
    expect(chips.some((c) => c.type === 'history' && c.ref === 'HIST-2026-0903')).toBe(true)
  })

  it('returns nothing for a point citing unknown sources (no fabrication)', () => {
    const chips = resolvePointCitations(
      'Bearing failed per SOP-FAKE-999 [SOP citation SOP-FAKE-999]',
      evidence,
      anomalySummary,
    )
    expect(chips).toHaveLength(0)
  })
})
