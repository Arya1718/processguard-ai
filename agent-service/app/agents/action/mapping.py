"""Recommendation -> CMMS call mapping (Prompt 5).

EXPLICIT, TYPED, DETERMINISTIC. This is the second safety boundary of the
Action Agent (docs/erp-integration.md): deciding WHICH external write to
perform is plain code over a small closed set of action types -- never an
LLM decision. LLMs reason and explain (Root-Cause, Recommendation agents);
only this mapping decides what gets executed, so it is auditable line by
line and impossible to prompt-inject into writing something unintended.

The Recommendation Agent's output schema (Prompt 4, extended in Prompt 5):
    actions: [{"action": str, "priority": int, "type"?: str, "product_key"?: str,
               "quantity"?: number}]
`type` may be "work_order" | "product_reorder" | "manual". Legacy actions
without a `type` are classified by keyword heuristics (maintenance phrasing
-> work order). Actions that match no type are mapped to None and reported
as skipped -- never guessed into an external write.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The closed vocabulary of external operations the Action Agent can perform.
ACTION_TYPE_WORK_ORDER = "work_order"
ACTION_TYPE_PRODUCT_REORDER = "product_reorder"
ACTION_TYPE_MANUAL = "manual"

_KNOWN_TYPES = {ACTION_TYPE_WORK_ORDER, ACTION_TYPE_PRODUCT_REORDER, ACTION_TYPE_MANUAL}

# Keyword heuristic for legacy actions that carry no explicit type. These
# mirror the phrasing the Recommendation Agent's prompt produces for
# maintenance work; anything else is left unmapped.
_WORK_ORDER_HINTS = (
    "inspect", "repair", "replace", "service", "check", "verify", "schedule",
    "backwash", "clean", "test", "calibrate", "maintain", "maintenance",
)
_REORDER_HINTS = ("reorder", "order", "restock", "chemical", "product")


@dataclass(frozen=True)
class CmmsOperation:
    """One concrete external operation the Action Agent will perform."""

    kind: str                       # ACTION_TYPE_WORK_ORDER | ACTION_TYPE_PRODUCT_REORDER
    priority: int                   # from the recommendation action
    source_action: str              # the original action text (audit trail)

    # work_order operation fields
    description: str = ""
    # reorder operation fields
    product_key: str = ""
    quantity: float = 0.0

    def to_payload(self) -> dict:
        return {
            "kind": self.kind,
            "description": self.description,
            "product_key": self.product_key,
            "quantity": self.quantity,
            "priority": self.priority,
            "source_action": self.source_action,
        }


@dataclass
class MappingResult:
    """Everything the Action Agent needs to execute + record."""

    operations: list[CmmsOperation] = field(default_factory=list)
    unmapped: list[dict] = field(default_factory=list)

    @property
    def has_operations(self) -> bool:
        return bool(self.operations)


def classify_action(action: dict) -> str:
    """Classify one recommendation action into the closed type vocabulary."""
    declared = str(action.get("type") or "").strip().lower()
    if declared in _KNOWN_TYPES:
        return declared
    text = str(action.get("action") or "").lower()
    if any(h in text for h in _WORK_ORDER_HINTS):
        return ACTION_TYPE_WORK_ORDER
    if any(h in text for h in _REORDER_HINTS):
        return ACTION_TYPE_PRODUCT_REORDER
    return ACTION_TYPE_MANUAL


def map_recommendation_to_cmms(recommendation: dict) -> MappingResult:
    """Map a stored recommendation (the `recommendation` jsonb from the
    incident record) to the concrete CMMS operations to execute.

    Never raises for unmapped content: unmapped actions are returned in
    MappingResult.unmapped and reported -- the caller decides whether an
    incident with zero operations still counts as action_taken.
    """
    result = MappingResult()
    actions = recommendation.get("actions") or []
    for action in actions:
        if not isinstance(action, dict):
            action = {"action": str(action), "priority": len(result.operations) + 1}
        kind = classify_action(action)
        if kind == ACTION_TYPE_WORK_ORDER:
            result.operations.append(CmmsOperation(
                kind=ACTION_TYPE_WORK_ORDER,
                priority=int(action.get("priority") or 1),
                source_action=str(action.get("action") or ""),
                description=str(action.get("action") or "").strip(),
            ))
        elif kind == ACTION_TYPE_PRODUCT_REORDER:
            product_key = str(action.get("product_key") or "").strip()
            if not product_key:
                # A reorder without a product cannot be executed; report it.
                result.unmapped.append({**action, "reason": "missing product_key"})
                continue
            result.operations.append(CmmsOperation(
                kind=ACTION_TYPE_PRODUCT_REORDER,
                priority=int(action.get("priority") or 1),
                source_action=str(action.get("action") or ""),
                product_key=product_key,
                quantity=float(action.get("quantity") or 0),
            ))
        else:
            # manual / unknown: never guessed into an external write.
            result.unmapped.append({**action, "reason": f"action type '{kind}' is not executable"})
    return result
