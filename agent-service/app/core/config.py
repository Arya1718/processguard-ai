"""Core configuration for the agent service.

All settings come from environment variables (PGAI_*). The service fails
fast at startup if anything required is missing -- it never starts silently
misconfigured.

AZURE NOTE: in staging/prod this same Settings interface is backed by
Azure Key Vault via managed identity instead of a .env file. Code reads
only PGAI_* variables from the process environment, so swapping the
source of those variables does not change any of this code.
"""
from __future__ import annotations

import os


class MissingEnvironmentVariable(Exception):
    """Raised at startup when a required PGAI_* variable is absent."""


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise MissingEnvironmentVariable(
            f"Missing required environment variable '{name}'. "
            "Copy .env.example to .env at the repo root and try again. "
            "(In staging/prod these values come from Azure Key Vault via managed identity.)"
        )
    return value


def _optional(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


class Settings:
    """Snapshot of process configuration, loaded once at startup."""

    def __init__(self) -> None:
        self.service_name: str = _optional("PGAI_SERVICE_NAME", "processguard-agent-service")
        self.log_level: str = _optional("PGAI_LOG_LEVEL", "INFO").upper()
        self.correlation_header: str = _optional("PGAI_CORRELATION_HEADER", "X-Correlation-Id")

        # Postgres (local) -- stands in for Azure SQL
        self.db_host: str = _required("PGAI_DATABASE__HOST")
        self.db_port: int = int(_optional("PGAI_DATABASE__PORT", "5432"))
        self.db_name: str = _required("PGAI_DATABASE__NAME")
        self.db_user: str = _required("PGAI_DATABASE__USER")
        self.db_password: str = _required("PGAI_DATABASE__PASSWORD")

        # Redis -- pub/sub stand-in for Azure Service Bus (and cache later)
        self.redis_host: str = _required("PGAI_REDIS__HOST")
        self.redis_port: int = int(_optional("PGAI_REDIS__PORT", "6379"))
        self.redis_password: str = _optional("PGAI_REDIS__PASSWORD", "")

        # Detection agent (Prompt 2) -- anomaly scoring / correlation knobs
        self.detection_static_z: float = float(_optional("PGAI_DETECTION__STATIC_Z", "0.05"))
        self.detection_drift_z: float = float(_optional("PGAI_DETECTION__DRIFT_Z", "3.0"))
        self.detection_min_samples: int = int(_optional("PGAI_DETECTION__MIN_SAMPLES", "4"))
        self.detection_correlation_window_seconds: float = float(
            _optional("PGAI_DETECTION__CORRELATION_WINDOW_SECONDS", "30")
        )
        self.detection_max_gap_seconds: float = float(
            _optional("PGAI_DETECTION__MAX_GAP_SECONDS", "90")
        )
        self.detection_window_size: int = int(_optional("PGAI_DETECTION__WINDOW_SIZE", "40"))
        self.detection_db_retry_attempts: int = int(_optional("PGAI_DETECTION__DB_RETRY_ATTEMPTS", "3"))

        # Sensor simulator (Prompt 2)
        self.simulator_interval_min_seconds: float = float(
            _optional("PGAI_SIMULATOR__INTERVAL_MIN_SECONDS", "2")
        )
        self.simulator_interval_max_seconds: float = float(
            _optional("PGAI_SIMULATOR__INTERVAL_MAX_SECONDS", "5")
        )
        self.simulator_ramp_seconds: float = float(_optional("PGAI_SIMULATOR__RAMP_SECONDS", "30"))
        self.simulator_stream: str = _optional("PGAI_SIMULATOR__STREAM", "sensor-readings")
        self.simulator_stream_maxlen: int = int(_optional("PGAI_SIMULATOR__STREAM_MAXLEN", "100000"))
        self.simulator_control_poll_seconds: float = float(
            _optional("PGAI_SIMULATOR__CONTROL_POLL_SECONDS", "1")
        )
        # true only when the simulator runs embedded in the agent-service
        # process (tests / single-process mode). Compose runs it separately.
        self.simulator_embedded: bool = _optional(
            "PGAI_SIMULATOR__EMBEDDED", "false"
        ).lower() == "true"

        # Prompt 8: real-data mode. synthetic (default) = the random-walk
        # simulator; tep_replay = real Tennessee Eastman Process data
        # (Rieth et al., DOI 10.7910/DVN/6C3JR1) streamed through the same
        # pipeline. Everything downstream requires zero changes -- that is
        # the point of the stream abstraction from Prompt 2.
        self.sensor_data_source: str = _optional("SENSOR_DATA_SOURCE", "synthetic").lower()
        if self.sensor_data_source not in ("synthetic", "tep_replay"):
            raise RuntimeError(
                f"SENSOR_DATA_SOURCE must be 'synthetic' or 'tep_replay', "
                f"got {self.sensor_data_source!r}"
            )
        self.tep_tick_seconds: float = float(_optional("PGAI_TEP__TICK_SECONDS", "3"))
        self.tep_skip_fraction: float = float(_optional("PGAI_TEP__SKIP_FRACTION", "0.5"))
        self.tep_data_dir: str = _optional("PGAI_TEP__DATA_DIR", "data/tep")

        # LLM (Prompt 3) -- Groq stand-in for Azure OpenAI.
        # The API key is REQUIRED unless fake mode is enabled explicitly.
        self.llm_api_key: str = os.environ.get("GROQ_API_KEY", "")
        self.llm_model: str = _optional("LLM_MODEL", "llama-3.3-70b-versatile")
        self.llm_base_url: str = _optional("PGAI_LLM__BASE_URL", "https://api.groq.com/openai/v1")
        self.llm_timeout_seconds: float = float(_optional("PGAI_LLM__TIMEOUT_SECONDS", "30"))
        # Deterministic scripted client for offline dev/CI/demo rehearsal.
        self.llm_fake_mode: bool = _optional("PGAI_LLM__FAKEMODE", "false").lower() == "true"

        # Mock ERP/CMMS (Prompt 5) -- stand-in for Buckman's real ERP/CMMS.
        # WRITE-ACCESS BOUNDARY: these are SEPARATE credentials from every
        # other system this service touches. The CMMS API key is the only
        # credential in the process that can cause an external side effect
        # (a work order / reorder), and it is used exclusively by the Action
        # Agent via app.core.cmms_client (see docs/security-model.md).
        # Optional here (the simulator container shares this Settings class
        # and must not require CMMS config); get_cmms_client() fails fast
        # when the Action Agent actually needs it and it is absent.
        self.cmms_base_url: str = _optional("PGAI_CMMS__BASE_URL", "")
        self.cmms_api_key: str = _optional("PGAI_CMMS__API_KEY", "")
        self.cmms_timeout_seconds: float = float(_optional("PGAI_CMMS__TIMEOUT_SECONDS", "10"))
        self.cmms_retry_attempts: int = int(_optional("PGAI_CMMS__RETRY_ATTEMPTS", "4"))
        self.cmms_retry_backoff_seconds: float = float(_optional("PGAI_CMMS__RETRY_BACKOFF_SECONDS", "2"))

        # Prompt 7: OIDC token validation. The agent service independently
        # re-validates tokens forwarded by the .NET middleware (signature via
        # the provider's JWKS, issuer, audience, lifetime) -- "internal" never
        # means "trusted blindly" (docs/auth-flow.md). The issuer may present
        # as either URL (host-facing or in-network); both are accepted.
        self.oidc_issuer: str = _optional("PGAI_OIDC__ISSUER", "http://localhost:8090")
        self.oidc_internal_issuer: str = _optional(
            "PGAI_OIDC__INTERNALISSUER", self.oidc_issuer
        )
        self.oidc_audience: str = _optional("PGAI_OIDC__AUDIENCE", "processguard-frontend")
        self.oidc_jwks_url: str = _optional("PGAI_OIDC__JWKS_URL", "")

        # Prompt 7: the shared role->capability table (same file the .NET
        # middleware loads -- one policy, one source of truth).
        self.rbac_policy_file: str = _optional("PGAI_RBAC__POLICYFILE", "config/rbac-policy.json")

    @property
    def oidc_valid_issuers(self) -> list[str]:
        return [self.oidc_issuer, self.oidc_internal_issuer]

    def db_dsn(self) -> str:
        return (
            f"postgres://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/0"


_settings: "Settings | None" = None


def get_settings() -> Settings:
    """Load settings once and cache; raises MissingEnvironmentVariable on
    first use if anything required is absent (fail fast at startup)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
