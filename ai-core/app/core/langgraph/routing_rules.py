"""Rule-based routing table for the Sprint 1 supervisor.

This module is the single source of truth for how a message's text (plus the
requester's admin status) maps to a Specialisation. `supervisor.py` calls
`classify_text()` rather than re-implementing the matching loop, so there is
never a second copy of this logic to drift out of sync.
"""

import re
from typing import Any, Dict, List, NamedTuple

from app.schemas.graph import Specialisation

ROUTING_RULES: List[Dict[str, Any]] = [
    {
        "name": "admin_operations",
        "specialisation": Specialisation.LEARNER_SUPPORT,
        "admin_target": Specialisation.ADMIN_OPS,
        "requires_admin": True,
        "patterns": [
            r"\badd\s+member\b",
            r"\bcreate\s+team\b",
            r"\bremove\s+user\b",
            r"\bdelete\s+channel\b",
            r"\bgrant\s+permission\b",
        ],
    },
    {
        "name": "learner_support",
        "specialisation": Specialisation.LEARNER_SUPPORT,
        "requires_admin": False,
        "patterns": [
            r"\bassignment\b",
            r"\bgrade\b",
            r"\bdeadline\b",
            r"\bsubmission\b",
            r"\bquiz\b",
            r"\bcourse\b",
            r"\blecture\b",
        ],
    },
]


class RoutingResult(NamedTuple):
    """A routing decision: which specialisation, how confident, and why."""

    specialisation: Specialisation
    matched_rule: str
    confidence: float


def classify_text(text: str, is_admin: bool = False) -> RoutingResult:
    """Classify a message into a Specialisation using the rule table above.

    An admin-only rule that matches for a non-admin requester does not fall
    back to the rule's ordinary specialisation — it is explicitly denied and
    routed to general_fallback with a low confidence and a distinct
    matched_rule, so the fallback handler can tell the requester the action
    needs admin rights instead of silently mis-routing them.
    """
    text_lower = text.lower()

    for rule in ROUTING_RULES:
        for pattern in rule["patterns"]:
            if not re.search(pattern, text_lower):
                continue

            if rule.get("requires_admin"):
                if is_admin:
                    return RoutingResult(
                        specialisation=rule.get("admin_target", Specialisation.ADMIN_OPS),
                        matched_rule=rule["name"],
                        confidence=0.95,
                    )
                return RoutingResult(
                    specialisation=Specialisation.GENERAL_FALLBACK,
                    matched_rule="admin_rule_denied_role",
                    confidence=0.10,
                )

            return RoutingResult(
                specialisation=rule["specialisation"],
                matched_rule=rule["name"],
                confidence=0.90,
            )

    return RoutingResult(
        specialisation=Specialisation.GENERAL_FALLBACK,
        matched_rule="general_fallback",
        confidence=0.0,
    )


if __name__ == "__main__":
    test_cases = [
        ("How can I submit my assignment before the deadline?", False, Specialisation.LEARNER_SUPPORT),
        ("Please add member to the project team", True, Specialisation.ADMIN_OPS),
        ("Please add member to the project team", False, Specialisation.GENERAL_FALLBACK),
        ("What is the grade for the last quiz?", False, Specialisation.LEARNER_SUPPORT),
        ("Can we create team for the new cohort?", True, Specialisation.ADMIN_OPS),
        ("Hello, what is the weather today?", False, Specialisation.GENERAL_FALLBACK),
    ]

    print("--- Running Routing Rules Unit Tests ---")
    all_passed = True
    for text, is_admin, expected in test_cases:
        result = classify_text(text, is_admin=is_admin)
        status = "PASS" if result.specialisation == expected else "FAIL"
        if status == "FAIL":
            all_passed = False
        print(
            f"[{status}] Text: '{text}' | Admin: {is_admin} -> "
            f"Result: {result.specialisation.value} (Expected: {expected.value}) "
            f"| rule={result.matched_rule} confidence={result.confidence}"
        )

    if all_passed:
        print("\nAll routing rule tests passed successfully!")
    else:
        raise SystemExit(1)
