#!/usr/bin/env python3
"""Verify that the authenticated Mattermost admin maps to a DB administrator.

This is read-only. It catches the deployment failure where Mattermost supplies
one user ID but SprintFlow stores a stale or hardcoded ID for the same person.
Run through ``scripts/verify_authorisation.sh --live`` so .env is loaded first.
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
API = f"http://localhost:{os.environ.get('MATTERMOST_HOST_PORT', '8065')}/api/v4"
RESULT_PREFIX = "ADMIN_MAPPING_RESULT="


def request(method: str, path: str, body: object | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read() or b"{}"), dict(response.headers)


def main() -> int:
    required = ("MM_ADMIN_USERNAME", "MM_ADMIN_PASSWORD", "ADMIN_EMAILS")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print(f"FAIL: missing environment variables: {', '.join(missing)}")
        return 1

    _, headers = request(
        "POST",
        "/users/login",
        {
            "login_id": os.environ["MM_ADMIN_USERNAME"],
            "password": os.environ["MM_ADMIN_PASSWORD"],
        },
    )
    token = headers.get("Token")
    if not token:
        print("FAIL: Mattermost login returned no session token.")
        return 1

    requester, _ = request("GET", "/users/me", token=token)
    requester_id = str(requester.get("id", ""))
    email = str(requester.get("email", "")).strip().lower()
    allowed = {
        value.strip().lower()
        for value in os.environ["ADMIN_EMAILS"].split(",")
        if value.strip()
    }

    code = """
import json
import os
from app.services.database import database_service

requester_id = os.environ["VERIFY_REQUESTER_ID"]
roles = database_service.get_user_roles(requester_id=requester_id)
print("ADMIN_MAPPING_RESULT=" + json.dumps(roles, sort_keys=True))
"""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "-e",
            f"VERIFY_REQUESTER_ID={requester_id}",
            "ai-core",
            "/app/.venv/bin/python",
            "-c",
            code,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    marker = next(
        (line for line in result.stdout.splitlines() if line.startswith(RESULT_PREFIX)),
        "",
    )
    if result.returncode != 0 or not marker:
        detail = result.stderr.strip() or result.stdout.strip()
        print(f"FAIL: ai-core role lookup failed: {detail[-500:]}")
        return 1

    roles = json.loads(marker.removeprefix(RESULT_PREFIX))
    checks = [
        ("Mattermost returned an authenticated user ID", bool(requester_id)),
        ("authenticated email is present in ADMIN_EMAILS", email in allowed),
        ("SprintFlow database maps that Mattermost ID", bool(roles.get("global") or roles.get("cohort_roles"))),
        ("mapped user has the global admin role", "admin" in roles.get("global", [])),
    ]

    print("=" * 76)
    for label, passed in checks:
        print(f"  {label:55} {'PASS' if passed else 'FAIL'}")
    print("=" * 76)
    ok = all(passed for _, passed in checks)
    print("ADMIN IDENTITY MAPPING OK" if ok else "ADMIN IDENTITY MAPPING FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
