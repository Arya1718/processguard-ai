"""Shared pytest configuration for the ProcessGuard AI test suite (Prompt 10).

Test layers (docs/testing-strategy.md):
  tests/unit/         -- hermetic unit tests (no external deps)
  tests/integration/  -- requires live `docker compose up` stack
  tests/contract/     -- JSON schema contracts (no live stack needed)
  tests/regression/   -- LLM behavioral regression set (needs GROQ_API_KEY)

Run unit tests only:   pytest tests/unit/ -v
Run integration tests: pytest tests/integration/ -v
Run contracts:         pytest tests/contract/ -v
Run everything hermetic: pytest tests -m "not integration" -v
Full suite (stack up): pytest tests --cov=app --cov-report=term --cov-fail-under=75
Load tests are NOT pytest: scripts/run_load_tests.py (Locust) -- never in CI.
"""
import os
import sys

# Make the app package importable from the tests.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Config requires these; tests never touch real infrastructure.
os.environ.setdefault("PGAI_DATABASE__HOST", "localhost")
os.environ.setdefault("PGAI_DATABASE__PORT", "5432")
os.environ.setdefault("PGAI_DATABASE__NAME", "test")
os.environ.setdefault("PGAI_DATABASE__USER", "test")
os.environ.setdefault("PGAI_DATABASE__PASSWORD", "test")
os.environ.setdefault("PGAI_REDIS__HOST", "localhost")
os.environ.setdefault("PGAI_REDIS__PORT", "6379")
os.environ.setdefault("PGAI_REDIS__PASSWORD", "")
