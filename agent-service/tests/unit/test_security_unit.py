"""Unit tests for app/core/security.py (Prompt 7 module, Prompt 10 gap-fill).

The LIVE validation path (real JWKS over HTTP, both layers) is covered by
tests/integration/test_rbac_auth.py. These unit tests cover the parts that
need no network at all:
  * the RBAC policy table loader (the shared config file, both services)
  * role->level / capability checks and the exact 403 detail messages
  * site enforcement (ensure_site: 403 policy refusal vs pass-through)
  * Principal claim extraction (roles array vs scalar, username fallbacks)
  * validate_token's LOCAL rejection semantics -- expired and tampered
    tokens are rejected before any network call matters, proven with a
    locally-generated RSA keypair signed exactly like the provider signs
  * empty/absent bearer -> TokenRejected (never a 500)
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

os.environ.setdefault("PGAI_DATABASE__HOST", "localhost")
os.environ.setdefault("PGAI_DATABASE__PORT", "5432")
os.environ.setdefault("PGAI_DATABASE__NAME", "test")
os.environ.setdefault("PGAI_DATABASE__USER", "test")
os.environ.setdefault("PGAI_DATABASE__PASSWORD", "test")
os.environ.setdefault("PGAI_REDIS__HOST", "localhost")
os.environ.setdefault("PGAI_REDIS__PORT", "6379")
os.environ.setdefault("PGAI_REDIS__PASSWORD", "")

import jwt
from fastapi import HTTPException

import app.core.security as sec
from app.core.security import (
    Principal,
    TokenRejected,
    ensure_approval_level,
    ensure_role,
    ensure_site,
    load_rbac_policy,
    validate_token,
)


@pytest.fixture(scope="module")
def rsa_keypair():
    """A local RSA keypair so tokens are signed exactly like the provider's."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key


def _write_policy(tmp_path, roles=None, **caps) -> str:
    data = {
        "roles": roles
        or {
            "Operator": {"max_approval_level": 0, "description": "view + chat"},
            "MaintenanceEngineer": {"max_approval_level": 2, "description": "level 2 decisions"},
            "PlantManager": {"max_approval_level": 3, "description": "level 3 decisions"},
        },
        "incident_read_roles": caps.get("incident_read_roles", ["Operator", "MaintenanceEngineer", "PlantManager"]),
        "chat_roles": caps.get("chat_roles", ["Operator", "MaintenanceEngineer", "PlantManager"]),
        "resolve_roles": caps.get("resolve_roles", ["MaintenanceEngineer", "PlantManager"]),
        "simulator_roles": caps.get("simulator_roles", ["MaintenanceEngineer", "PlantManager"]),
    }
    path = tmp_path / "rbac-policy.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


class TestPolicyTable:
    def test_loads_shared_policy_file(self, tmp_path):
        policy = load_rbac_policy(_write_policy(tmp_path))
        assert policy.level_for("MaintenanceEngineer") == 2
        assert policy.level_for("PlantManager") == 3
        assert policy.level_for("Operator") == 0
        assert policy.level_for("UnknownRole") == 0  # unknown roles decide nothing

    def test_capability_sets(self, tmp_path):
        policy = load_rbac_policy(
            _write_policy(tmp_path, simulator_roles=["MaintenanceEngineer"])
        )
        assert policy.can_read_incidents("Operator")
        assert policy.can_chat("Operator")
        assert not policy.can_control_simulator("Operator")
        assert policy.can_control_simulator("MaintenanceEngineer")
        assert not policy.can_resolve("Operator")

    def test_missing_policy_file_raises_loudly(self, tmp_path):
        with pytest.raises(RuntimeError, match="RBAC policy file not found"):
            load_rbac_policy(str(tmp_path / "absent.json"))


class TestPrincipal:
    def test_roles_array_claim(self):
        p = Principal(token="t", payload={"roles": ["PlantManager"], "site_id": "s-12"})
        assert p.role == "PlantManager"

    def test_roles_scalar_claim(self):
        p = Principal(token="t", payload={"roles": "Operator", "site_id": "s-12"})
        assert p.role == "Operator"

    def test_username_fallback_chain(self):
        assert Principal("t", {"preferred_username": "a@x"}).username == "a@x"
        assert Principal("t", {"name": "b@x"}).username == "b@x"
        assert Principal("t", {"sub": "c@x"}).username == "c@x"
        assert Principal("t", {}).username == "unknown"


class TestSiteEnforcement:
    def test_same_site_passes(self):
        p = Principal("t", {"site_id": "11111111-1111-1111-1111-111111111111"})
        ensure_site(p, "11111111-1111-1111-1111-111111111111")

    def test_cross_site_is_403_not_404(self):
        p = Principal("t", {"site_id": "22222222-2222-2222-2222-222222222222"})
        with pytest.raises(HTTPException) as exc:
            ensure_site(p, "11111111-1111-1111-1111-111111111111")
        assert exc.value.status_code == 403
        assert "another site" in exc.value.detail

    def test_unknown_site_passes_through_to_404_path(self):
        ensure_site(Principal("t", {"site_id": "s-12"}), None)


class TestRoleGates:
    def test_ensure_role_rejects_with_403_and_named_role(self):
        p = Principal("t", {"roles": ["Operator"]})
        with pytest.raises(HTTPException) as exc:
            ensure_role(p, frozenset({"PlantManager"}), "resolve incidents")
        assert exc.value.status_code == 403
        assert "Operator" in exc.value.detail
        assert "resolve incidents" in exc.value.detail

    def test_ensure_approval_level_second_layer_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sec, "_policy", None)
        monkeypatch.setenv("PGAI_RBAC__POLICYFILE", _write_policy(tmp_path))
        p = Principal("t", {"roles": ["MaintenanceEngineer"]})
        ensure_approval_level(p, 2)
        with pytest.raises(HTTPException) as exc:
            ensure_approval_level(p, 3)
        assert exc.value.status_code == 403
        assert "HITL level 3" in exc.value.detail
        monkeypatch.setattr(sec, "_policy", None)


class TestTokenRejection:
    """Full validation semantics with a LOCAL signing key: the fake JWK
    client returns our own public key, so jwt.decode genuinely verifies the
    signature -- no network, every branch exercised for real."""

    ISSUER = "http://localhost:8090"
    AUD = "processguard-api"

    @pytest.fixture(autouse=True)
    def _local_oidc(self, monkeypatch):
        from app.core import config as config_mod

        monkeypatch.setenv("PGAI_OIDC__VALIDISSUERS", self.ISSUER)
        monkeypatch.setenv("PGAI_OIDC__AUDIENCE", self.AUD)
        monkeypatch.setattr(config_mod, "_settings", None)
        yield
        monkeypatch.setattr(config_mod, "_settings", None)
        monkeypatch.setattr(sec, "_jwk_client", None)

    def _use_local_key(self, monkeypatch, private_key):
        from types import SimpleNamespace

        class _FakeJwkClient:
            def get_signing_key_from_jwt(self, token):
                return SimpleNamespace(key=private_key.public_key())

        monkeypatch.setattr(sec, "_jwk_client", _FakeJwkClient())

    def _signed(self, key, claims: dict) -> str:
        base = {
            "iss": self.ISSUER,
            "aud": self.AUD,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "sub": "operator@site12.demo",
            "roles": ["Operator"],
            "site_id": "11111111-1111-1111-1111-111111111111",
        }
        base.update(claims)
        return jwt.encode(base, key, algorithm="RS256", headers={"kid": "local-test"})

    def test_valid_token_yields_principal(self, rsa_keypair, monkeypatch):
        self._use_local_key(monkeypatch, rsa_keypair)
        principal = validate_token(self._signed(rsa_keypair, {}))
        assert principal.role == "Operator"
        assert principal.site_id == "11111111-1111-1111-1111-111111111111"
        assert principal.username == "operator@site12.demo"

    def test_expired_token_rejected(self, rsa_keypair, monkeypatch):
        self._use_local_key(monkeypatch, rsa_keypair)
        token = self._signed(rsa_keypair, {"exp": int(time.time()) - 10})
        with pytest.raises(TokenRejected, match="expired"):
            validate_token(token)

    def test_wrong_audience_rejected(self, rsa_keypair, monkeypatch):
        self._use_local_key(monkeypatch, rsa_keypair)
        token = self._signed(rsa_keypair, {"aud": "someone-elses-api"})
        with pytest.raises(TokenRejected, match="audience"):
            validate_token(token)

    def test_untrusted_issuer_rejected(self, rsa_keypair, monkeypatch):
        self._use_local_key(monkeypatch, rsa_keypair)
        token = self._signed(rsa_keypair, {"iss": "https://evil.example"})
        with pytest.raises(TokenRejected, match="untrusted issuer"):
            validate_token(token)

    def test_tampered_payload_rejected_by_signature(self, rsa_keypair, monkeypatch):
        """Escalate the role claim without re-signing: the signature check
        must reject it (this is the defense the integration suite proves
        against the live provider's JWKS)."""
        self._use_local_key(monkeypatch, rsa_keypair)
        token = self._signed(rsa_keypair, {})
        header_b64, payload_b64, sig_b64 = token.split(".")
        import base64

        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        payload["roles"] = ["PlantManager"]
        forged = (
            header_b64
            + "."
            + base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
            + "."
            + sig_b64
        )
        with pytest.raises(TokenRejected):
            validate_token(forged)

    def test_missing_claims_rejected(self, rsa_keypair, monkeypatch):
        """A validly signed token without site_id/role claims is useless
        here: no site scope can be established, so it is rejected."""
        self._use_local_key(monkeypatch, rsa_keypair)
        token = self._signed(rsa_keypair, {"roles": ["Operator"], "site_id": None})
        with pytest.raises(TokenRejected, match="site_id/role"):
            validate_token(token)

    def test_empty_bearer_rejected(self):
        with pytest.raises(TokenRejected, match="no bearer token"):
            validate_token("")
        with pytest.raises(TokenRejected):
            validate_token("Bearer ")

    def test_garbage_bearer_is_401_not_500(self):
        """A non-JWT string must map to the clean TokenRejected path (401),
        not leak a DecodeError as a 500 -- the regression this test pins."""
        with pytest.raises(TokenRejected, match="cannot resolve signing key"):
            validate_token("garbage-not-a-jwt")

    def test_bearer_prefix_stripped(self, rsa_keypair, monkeypatch):
        self._use_local_key(monkeypatch, rsa_keypair)
        token = self._signed(rsa_keypair, {"exp": int(time.time()) - 10})
        with pytest.raises(TokenRejected, match="expired"):
            validate_token(f"Bearer {token}")
