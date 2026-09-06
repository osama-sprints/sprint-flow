"""Install (or upgrade) a Mattermost plugin bundle on the running server.

Uploading a plugin requires ``PluginSettings.EnableUploads``. This script turns
it on through the config API when it is off, rather than editing config.json by
hand, and reports what it changed so the change is never silent.

Usage: python3 scripts/install_plugin.py <bundle.tar.gz>
"""

import json
import mimetypes
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = "http://localhost:8065/api/v4"


def env() -> dict[str, str]:
    """Read the repository .env into a dict."""
    values: dict[str, str] = {}
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


def login(values: dict[str, str]) -> str:
    """Log in as the system admin and return the session token."""
    request = urllib.request.Request(
        f"{BASE}/users/login",
        data=json.dumps({"login_id": values["MM_ADMIN_USERNAME"], "password": values["MM_ADMIN_PASSWORD"]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        token = response.headers.get("Token")
    if not token:
        raise SystemExit("login did not return a session token")
    return token


def call(method: str, path: str, token: str, payload: object | None = None) -> object:
    """Make an authenticated JSON API call."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        body = response.read()
    return json.loads(body) if body else {}


REMEDY = """
Plugin uploads are disabled and the config API cannot turn them on: a
PUT /api/v4/config returns 200 and silently leaves PluginSettings.EnableUploads
false. Set it in the server's config.json and restart, then run this again:

  python3 - <<'EOF' > /tmp/mm-config.json
  import json, pathlib
  c = json.loads(pathlib.Path('mattermost/volumes/app/mattermost/config/config.json').read_text())
  c['PluginSettings']['EnableUploads'] = True
  print(json.dumps(c, indent=4))
  EOF
  docker cp /tmp/mm-config.json sprintflow-mattermost:/mattermost/config/config.json
  docker run --rm -v "$PWD/mattermost/volumes/app/mattermost/config:/c" alpine:3 \\
      chown 2000:2000 /c/config.json
  docker compose restart mattermost

The chown matters: the server runs as uid 2000 and refuses to start when it
cannot write its own config file.
"""


def ensure_uploads_enabled(token: str) -> bool:
    """Turn on plugin uploads if they are off.

    The API accepts the write and does not apply it, so the result is read back
    rather than assumed. Returning success on an unapplied write would turn a
    clear failure here into a confusing 501 on the upload that follows.

    Args:
        token: Admin session token.

    Returns:
        bool: True when this call actually changed the setting.

    Raises:
        SystemExit: When uploads remain disabled after the attempt.
    """
    config = call("GET", "/config", token)
    assert isinstance(config, dict)
    if config["PluginSettings"].get("EnableUploads"):
        return False

    config["PluginSettings"]["EnableUploads"] = True
    call("PUT", "/config", token, config)

    verified = call("GET", "/config", token)
    assert isinstance(verified, dict)
    if not verified["PluginSettings"].get("EnableUploads"):
        raise SystemExit(REMEDY)
    return True


def upload(bundle: Path, token: str) -> dict:
    """Upload the bundle with force=true so an existing version is replaced."""
    boundary = uuid.uuid4().hex
    content_type = mimetypes.guess_type(bundle.name)[0] or "application/gzip"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="force"\r\n\r\ntrue\r\n',
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="plugin"; filename="{bundle.name}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            bundle.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    request = urllib.request.Request(
        f"{BASE}/plugins",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def main() -> int:
    """Upload and enable the bundle named on the command line."""
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    bundle = Path(sys.argv[1])
    if not bundle.is_absolute():
        bundle = (Path.cwd() / bundle).resolve()
    if not bundle.exists():
        raise SystemExit(f"bundle not found: {bundle}")

    values = env()
    token = login(values)

    if ensure_uploads_enabled(token):
        print("changed PluginSettings.EnableUploads: false -> true")

    manifest = upload(bundle, token)
    plugin_id = manifest["id"]
    print(f"uploaded {plugin_id} version {manifest['version']}")

    try:
        call("POST", f"/plugins/{plugin_id}/enable", token)
        print(f"enabled {plugin_id}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"enable failed: {e.read().decode()[:300]}")

    statuses = call("GET", "/plugins", token)
    assert isinstance(statuses, dict)
    active = [p["id"] for p in statuses.get("active", [])]
    print("active plugins:", active)
    return 0 if plugin_id in active else 1


if __name__ == "__main__":
    sys.exit(main())
