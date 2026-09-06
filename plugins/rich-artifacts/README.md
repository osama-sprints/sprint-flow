# SprintFlow Rich Artifacts

A Mattermost plugin (Team Edition 11.7, plugin id `com.sprintflow.mermaid` kept for
compatibility) that renders agent-authored artifacts in the chat stream: Mermaid
diagrams, Vega-Lite charts, sandboxed generated-React interfaces, generated images
as native attachments, and inline playback of bare video links. It also corrects
Arabic/RTL direction on ordinary posts.

Two components ship in one archive:

```
plugins/rich-artifacts/
├── plugin.json                  # webapp bundle + server executable + settings
├── Makefile                     # deps · build · server · dist · deploy
├── webapp/src/
│   ├── index.tsx                # registers both post types, the video embed, bidi
│   ├── types.ts                 # the versioned envelope contract + runtime validation
│   ├── bidi/                    # per-block direction for ordinary posts
│   ├── components/              # cards, Markdown body, mermaid, charts, React sandbox host, video
│   ├── api/artifacts.ts         # authenticated fetch of large artifacts by id
│   └── utils/                   # theme detection, lazy mermaid loader
└── server/                      # Go component (built in a container)
    ├── plugin.go                # artifact reads (viewer-authorised), lazy chunks, sandbox runtime
    └── assets/runtime.html      # the isolated React runtime + design system
```

## Build and install

```bash
make deps      # npm ci from the committed lockfile
make server    # compiles the Go component in a golang container (no host Go)
make dist      # single-chunk webapp bundle + server binary -> dist/*.tar.gz
make deploy    # uploads and enables it on the running server
```

`react` and `react-dom` are webpack `externals` provided by the webapp. Mattermost
injects exactly one file per plugin (`main.js`, ~180 KB / 51 KB gzip), so the heavy
libraries are **lazy chunks served by the plugin's own Go component** at
`/plugins/com.sprintflow.mermaid/api/v1/assets/…` — the same origin, which is
`'self'` under Mattermost's stock CSP. A channel of ordinary posts loads nothing
but `main.js`; a Mermaid post fetches the mermaid chunk (120 KB gzip), a chart the
vega chunk (266 KB gzip). Babel is not in the host page at all: generated React is
compiled inside the sandbox runtime, which embeds React and Babel (~3.1 MB, cached).

### Configuration

| Setting | Where | Why |
| --- | --- | --- |
| `PluginSettings.EnableUploads: true` | Mattermost `config.json`, then restart | Required to upload a plugin. The config **API accepts and silently ignores** this key; the installer detects that and prints the `config.json` procedure. |
| `RICH_MEDIA_PLUGIN_TOKEN` | `.env` (ai-core) | Shared secret for artifact reads; ai-core refuses them when unset. |
| `SPRINTFLOW_AICORE_URL`, `SPRINTFLOW_PLUGIN_TOKEN` | `docker-compose.yml` (mattermost) | Read by the Go component, which inherits the server's environment. Plugin settings override them. |
| `IMAGE_GENERATION_ENABLED`, `IMAGE_MODEL` | `.env` (ai-core) | Nano Banana through LiteLLM (`/chat/completions`, not `/images/generations`). Off by default. |
| `RICH_MEDIA_MAX_IMAGES_PER_TURN`, `RICH_MEDIA_MAX_CHART_ROWS`, `RICH_MEDIA_MAX_REACT_CHARS` | `.env` (ai-core) | Per-reply ceilings; the renderer enforces the same limits client-side. |

### Sandbox limits

Generated code runs in an iframe sandboxed **without** `allow-same-origin`: no
cookies, storage or parent access (each throws `SecurityError`). That is an
*origin* boundary, not a *CPU* boundary. Chrome keeps a same-site sandboxed frame
on the parent's main thread, and a runaway loop froze the whole chat page in
testing. The runtime compiles every `for`/`while`/`do` with a per-loop budget of
1,000,000 iterations and throws past it, which turned that test from a frozen tab
into an error card.

**This is a partial safeguard, not isolation.** Unbounded recursion, a long
synchronous computation that contains no loop, or a loop that does expensive work
inside its million iterations still runs on the page's thread until it finishes,
and nothing in the plugin can interrupt it. Do not describe the sandbox as
protecting the page from freezes; describe it as protecting the page's data and
session from generated code.

## The post type

`custom_interactive_mermaid`. The `custom_` prefix is not cosmetic: the server
rejects post types under the `system_` prefix from non-system users, and the
webapp only routes a post to a plugin component when the plugin registered that
exact type string.

## API payload

The bot creates the post with an ordinary `POST /api/v4/posts`, authenticated
with its Personal Access Token.

```http
POST /api/v4/posts HTTP/1.1
Host: mattermost:8065
Authorization: Bearer <BOT_PERSONAL_ACCESS_TOKEN>
Content-Type: application/json
```

```json
{
  "channel_id": "8f3j1x9c7ib5jbwmqz8w9x6h4o",
  "root_id": "",
  "message": "Sprint 3 delivery flow (diagram requires the SprintFlow Mermaid plugin)",
  "type": "custom_interactive_mermaid",
  "props": {
    "mermaid_definition": "graph TD;\n  A[Backlog] --> B{Refined?};\n  B -- yes --> C[Sprint];\n  B -- no --> A;\n  C --> D[Review];",
    "title": "Sprint 3 delivery flow",
    "caption": "Generated from the current sprint board."
  }
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `channel_id` | yes | Bot must be a channel member. |
| `type` | yes | Exactly `custom_interactive_mermaid`. |
| `props.mermaid_definition` | yes | Raw Mermaid source. Newlines are real `\n` inside the JSON string. |
| `props.title` | no | Header text; defaults to "Diagram". |
| `props.caption` | no | Small text under the diagram. |
| `message` | recommended | The fallback body. Mobile clients, search results, email and push notifications do not run webapp plugins and will show only this. |
| `root_id` | no | Thread root to reply under. Must be a root post, not a reply. |

Response is `201` with the created post; `props` come back verbatim.

Curl equivalent:

```bash
curl -sS -X POST "$MATTERMOST_URL/api/v4/posts" \
  -H "Authorization: Bearer $MATTERMOST_BOT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"channel_id":"'"$CHANNEL_ID"'","message":"Sprint 3 delivery flow","type":"custom_interactive_mermaid","props":{"mermaid_definition":"graph TD;\n  A-->B;","title":"Sprint 3 delivery flow"}}'
```

From `ai-core`, the same call through the existing client
(`app/services/mattermost.py`) — the client's `create_post` does not carry
`type`/`props`, so a diagram post uses `_request` directly:

```python
async def create_mermaid_post(
    self,
    channel_id: str,
    definition: str,
    title: str,
    fallback: str,
    root_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Post a diagram rendered by the SprintFlow Mermaid webapp plugin."""
    payload: Dict[str, Any] = {
        "channel_id": channel_id,
        "message": fallback,
        "type": "custom_interactive_mermaid",
        "props": {"mermaid_definition": definition, "title": title},
    }
    if root_id:
        payload["root_id"] = root_id
    return await self._request("POST", "/posts", json=payload)
```

## Constraints worth knowing

- **Props are a size-limited JSON map.** The server caps user-set post props
  (400k runes); keep definitions to diagrams, not data dumps.
- **Interaction is local.** The view/source toggle and collapse live in React
  state only. Nothing calls `PATCH /posts/{id}`, so one member's toggling is
  invisible to everyone else and the stored post is never rewritten. Persisting
  a toggle would require updating `props` — a different, deliberate design.
- **The definition is untrusted input.** Mermaid runs with
  `securityLevel: 'strict'` and `htmlLabels: false`, so labels are sanitized and
  click bindings are inert before the SVG reaches the DOM.
- **Render failures are contained.** A bad definition shows an inline error with
  the source instead of Mermaid's stray red overlay, which is removed on catch.

## Video

A bare video link (`.mp4`, `.webm`, `.m4v`, `.ogv`) in any post — the bot's included —
plays inline. Mattermost records such a link as a plain `link` embed and would show
only the link; the plugin registers a component through the supported
`registerPostWillRenderEmbedComponent` hook (argument order `match, component,
toggleable`) that renders a native `<video>` with controls, **streamed from the
original URL with range requests**. Nothing is downloaded or re-uploaded, the link
in the message stays clickable, an "Open original" link sits under the player, and
a playback error falls back to a link. Mattermost's own collapse control applies.

Verified on Mattermost 11.7.10 with a 1920×1080 H.264/AAC MP4 (242 s, `moov` at
the end of the file), with default browser settings and mouse clicks on the
native controls, in the centre timeline (a DM reply) and the thread panel: playback
advances, pause holds, a scrubber click seeks, resume continues, audio decodes, and
every media request goes to the CDN host as a `206` range response with no CSP
violation. A plain `<video>` element needs no CORS for this.

Files that must live under Mattermost's own access control can still be attached
natively (`file_ids`); Mattermost's preview player handles those.
