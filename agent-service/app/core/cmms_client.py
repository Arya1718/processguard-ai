"""HTTP client for the mock ERP/CMMS service (Prompt 5) -- stand-in for
Buckman's real ERP/CMMS.

SECURITY BOUNDARY (docs/security-model.md): this module is the ONLY code in
the entire ProcessGuard AI codebase allowed to perform an EXTERNAL WRITE.
Every other agent reads sensors, reads the knowledge base and writes only to
our own incident tables; if a future feature needs to touch an external
system it must go through this client and the Action Agent, not grow its own
HTTP calls. The client authenticates with the dedicated PGAI_CMMS__API_KEY --
separate from the database password, Redis, and the LLM key -- so CMMS
credentials can be rotated or revoked independently (and in production would
live in Azure Key Vault under their own secret name, scoped to the workload
identity the Action Agent runs as).

Construction is lazy (get_cmms_client) and fails fast with a clear message
if the CMMS configuration is missing, mirroring the config loader pattern
from Prompt 1 -- never start silently misconfigured.
"""
from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.core.logging_config import correlation_id_var, get_logger

logger = get_logger(__name__)

_client: "CmmsClient | None" = None


def get_cmms_client() -> "CmmsClient":
    """Build (once) the process-wide CMMS client. Fails fast when the
    CMMS_* env vars are absent -- only the Action Agent calls this, and it
    cannot run correctly without them."""
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.cmms_base_url or not settings.cmms_api_key:
            raise RuntimeError(
                "Mock ERP/CMMS is not configured: set PGAI_CMMS__BASE_URL and "
                "PGAI_CMMS__API_KEY (separate credentials -- see "
                "docs/security-model.md). In staging/prod these come from "
                "Azure Key Vault via managed identity."
            )
        _client = CmmsClient(
            base_url=settings.cmms_base_url,
            api_key=settings.cmms_api_key,
            timeout_seconds=settings.cmms_timeout_seconds,
        )
    return _client


def reset_cmms_client() -> None:
    """Test hook: drop the cached client so the next get_cmms_client()
    re-reads configuration."""
    global _client
    _client = None


class CmmsError(Exception):
    """A CMMS request failed after exhausting retries (network, timeout,
    non-2xx). Carries the method, path, and the last response body if any."""

    def __init__(self, method: str, path: str, last_error: str) -> None:
        super().__init__(f"CMMS {method} {path} failed: {last_error}")
        self.method = method
        self.path = path
        self.last_error = last_error


class CmmsClient:
    """Thin async wrapper over the mock CMMS REST API with bounded
    retry/backoff. Deliberately minimal: create/list/get work orders,
    inventory lookup + reorder, and resolve -- exactly the operations the
    Action Agent's typed mapping performs, nothing more."""

    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 10.0,
                 retry_attempts: int = 4, retry_backoff_seconds: float = 2.0,
                 transport=None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._retry_attempts = max(1, int(retry_attempts))
        self._backoff = retry_backoff_seconds
        # Optional httpx transport -- test hook only (lets unit tests inject
        # a mock transport to prove the bounded retry/backoff behavior).
        self._transport = transport

    async def create_work_order(self, equipment_id: str, description: str, priority: str,
                                requested_by: str, source_incident_id: str,
                                correlation_id: str | None = None) -> dict:
        """POST /work-orders -> the created order incl. its CMMS id."""
        return await self._request(
            "POST", "/work-orders",
            json={
                "equipment_id": equipment_id,
                "description": description,
                "priority": priority,
                "requested_by": requested_by,
                "source_incident_id": source_incident_id,
            },
            ok_codes=(200, 201),
            correlation_id=correlation_id,
        )

    async def get_work_order(self, work_order_id: str) -> dict:
        return await self._request("GET", f"/work-orders/{work_order_id}")

    async def list_work_orders(self, equipment_id: str | None = None,
                               status: str | None = None) -> list[dict]:
        params: dict[str, str] = {}
        if equipment_id:
            params["equipment_id"] = equipment_id
        if status:
            params["status"] = status
        return await self._request("GET", "/work-orders", params=params)

    async def resolve_work_order(self, work_order_id: str, resolution_note: str) -> dict:
        """POST /work-orders/{id}/resolve -> the resolved order."""
        return await self._request(
            "POST", f"/work-orders/{work_order_id}/resolve",
            json={"resolution_note": resolution_note},
            ok_codes=(200, 201),
        )

    async def get_inventory(self, product_key: str) -> dict:
        return await self._request("GET", f"/inventory/{product_key}")

    async def request_reorder(self, product_key: str, quantity: float,
                              source_incident_id: str, requested_by: str,
                              correlation_id: str | None = None) -> dict:
        """POST /inventory/{product}/reorder -> the reorder request record."""
        return await self._request(
            "POST", f"/inventory/{product_key}/reorder",
            json={
                "quantity": quantity,
                "source_incident_id": source_incident_id,
                "requested_by": requested_by,
            },
            ok_codes=(200, 201),
            correlation_id=correlation_id,
        )

    # -- internals -----------------------------------------------------------

    async def _request(self, method: str, path: str, json: dict | None = None,
                       params: dict[str, str] | None = None,
                       ok_codes: tuple[int, ...] = (200,),
                       correlation_id: str | None = None) -> Any:
        """One request with bounded retry/backoff. 4xx responses are NOT
        retried (a rejected work order will not get better by resending);
        network errors and 5xx are retried, then raised as CmmsError."""
        import asyncio

        import httpx

        url = f"{self._base_url}{path}"
        # Correlation ID: an explicit id (e.g. propagated from the approval
        # request through the bus event) wins; otherwise fall back to the
        # ambient request context, if any. The mock CMMS logs this id, so a
        # single investigation is traceable into the CMMS's own logs.
        cid = correlation_id or (correlation_id_var.get(None) or "")
        headers = {"X-Api-Key": self._api_key, "Content-Type": "application/json"}
        if cid:
            headers["X-Correlation-Id"] = cid
        last_error = ""
        for attempt in range(1, self._retry_attempts + 1):
            try:
                client_kwargs = {"timeout": self._timeout}
                if self._transport is not None:  # test hook only
                    client_kwargs["transport"] = self._transport
                async with httpx.AsyncClient(**client_kwargs) as http:
                    response = await http.request(
                        method, url, json=json, params=params, headers=headers)
                if response.status_code in ok_codes:
                    return response.json()
                if 400 <= response.status_code < 500:
                    # Client error: deterministic rejection -- do not retry.
                    raise CmmsError(method, path,
                                    f"HTTP {response.status_code}: {response.text[:300]}")
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            except CmmsError:
                raise
            except Exception as exc:  # network / timeout
                last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("CMMS %s %s attempt %d/%d failed: %s",
                           method, path, attempt, self._retry_attempts, last_error)
            if attempt < self._retry_attempts:
                await asyncio.sleep(self._backoff * attempt)
        raise CmmsError(method, path, last_error or "exhausted retries")
