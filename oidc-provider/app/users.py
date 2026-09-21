"""Seeded demo users, roles, sites, and passwords for the local OIDC provider.

Four demo identities (docs/auth-flow.md):

    operator@site12.demo         role: Operator              -> Site 12
    maintenance@site12.demo      role: MaintenanceEngineer   -> Site 12
    manager@site12.demo          role: PlantManager          -> Site 12
    operator@site07.demo         role: Operator              -> Site 07 (isolation proof)

Passwords are local-demo ONLY (scrypt-hashed at startup, never stored in
clear text). Real Entra ID handles credentials upstream; this module exists
so the login screen works end to end locally.
"""
from __future__ import annotations

import hashlib
import hmac
import os

# Canonical site UUIDs -- mirrored by the demo seed in the agent-service
# (app/core/db.py:CANONICAL_SITES) so tokens' site_id claims match real rows.
SITE_12_ID = "11111111-1111-1111-1111-111111111111"
SITE_07_ID = "22222222-2222-2222-2222-222222222222"

# (email, password, role, site_id, display_name)
USERS: list[tuple[str, str, str, str, str]] = [
    ("operator@site12.demo", "operator-pass", "Operator", SITE_12_ID, "Site 12 Operator"),
    ("maintenance@site12.demo", "maintenance-pass", "MaintenanceEngineer", SITE_12_ID, "Site 12 Maintenance Engineer"),
    ("manager@site12.demo", "manager-pass", "PlantManager", SITE_12_ID, "Site 12 Plant Manager"),
    ("operator@site07.demo", "operator07-pass", "Operator", SITE_07_ID, "Site 07 Operator"),
]

SITES: dict[str, str] = {
    SITE_12_ID: "Site 12 - Paper Mill Cooling Tower",
    SITE_07_ID: "Site 07 - Leather Tannery Effluent Line",
}

_INDEX = {u[0]: u for u in USERS}


def find_user(email: str) -> tuple[str, str, str, str, str] | None:
    return _INDEX.get((email or "").strip().lower())


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$", 2)
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1, dklen=32
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False
