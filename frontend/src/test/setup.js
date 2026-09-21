import '@testing-library/jest-dom/vitest'

// jsdom lacks crypto.randomUUID (Node 20+ has it globally, but be safe).
if (typeof crypto !== 'undefined' && !crypto.randomUUID) {
  crypto.randomUUID = () =>
    'test-uuid-0000-0000-0000-000000000000'.replace(/0/g, () =>
      Math.floor(Math.random() * 16).toString(16),
    )
}
