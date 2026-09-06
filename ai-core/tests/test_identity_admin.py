"""Superadmin derivation from the allowlist plus Mattermost trust signals (review fix, 2026-09-03)."""

import pytest

from app.core.config import settings
from app.services.identity import is_allowlisted_admin


@pytest.fixture(autouse=True)
def allowlist(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ADMIN_EMAILS", ["admin@sprints.ai"])


def test_not_allowlisted_is_never_admin():
    assert is_allowlisted_admin("someone@sprints.ai", {"email_verified": True, "roles": "system_admin"}) is False
    assert is_allowlisted_admin(None, {"email_verified": True}) is False


def test_allowlisted_and_verified_email_is_admin():
    assert is_allowlisted_admin("admin@sprints.ai", {"email_verified": True, "roles": "system_user"}) is True
    assert is_allowlisted_admin("admin@sprints.ai", {"email_verified": "true", "roles": ""}) is True


def test_allowlisted_system_admin_is_admin_even_if_unverified():
    assert (
        is_allowlisted_admin("admin@sprints.ai", {"email_verified": False, "roles": "system_admin system_user"})
        is True
    )


def test_allowlisted_but_untrusted_account_is_not_admin():
    assert is_allowlisted_admin("admin@sprints.ai", {"email_verified": False, "roles": "system_user"}) is False
    assert is_allowlisted_admin("admin@sprints.ai", None) is False
