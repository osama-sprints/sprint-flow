// Package main implements the SprintFlow Rich Artifacts server plugin.
//
// It exists for one reason: the browser cannot reach ai-core. ai-core lives on
// the internal Docker network and the page holds no credential for it, so the
// only same-origin, already-authenticated door available is the plugin's own
// HTTP handler.
//
// The security model is a deliberate split. Mattermost authenticates the VIEWER
// and hands this handler their user id in the Mattermost-User-Id header — a
// header Mattermost sets itself and strips from inbound requests, which is why
// it can be trusted here and a user id in a request body never can. This
// handler then checks that THIS viewer may read the channel and post the
// artifact was published in, and only then fetches the content from ai-core
// with a shared secret. The bot's own visibility is never used as proof: the
// bot is a member of channels many of its readers are not.
package main

import (
	"embed"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/mattermost/mattermost/server/public/model"
	"github.com/mattermost/mattermost/server/public/plugin"
)

const (
	artifactPrefix = "/api/v1/artifacts/"
	assetPrefix    = "/api/v1/assets/"
	runtimePath    = "/api/v1/runtime"
	requestTimeout = 10 * time.Second
)

// The isolated runtime document and the React build it inlines. Embedding them
// means the sandbox fetches nothing at all: no CDN, no second origin, and
// nothing that a network policy has to allow.
//
//go:embed assets/runtime.html
var runtimeHTML string

//go:embed assets/react.js
var reactJS string

//go:embed assets/react-dom.js
var reactDOMJS string

// Babel compiles generated components INSIDE the sandbox. Embedding it here
// keeps ~1.4 MB out of the bundle every reader downloads for every channel.
//
//go:embed assets/babel.js
var babelJS string

// Lazily loaded webapp chunks (mermaid, vega). Served from this plugin so the
// host page fetches them from 'self' under Mattermost's stock CSP.
//
//go:embed assets/chunks
var chunkFiles embed.FS

// runtimeDocument is the runtime with React inlined, built once at activation.
var runtimeDocument string

// runtimeCSP applies to the runtime document ALONE, as a response header on
// that resource. Mattermost's own page keeps the policy it ships with; this
// does not widen it. The document runs on an opaque origin (the iframe is
// sandboxed without allow-same-origin), where 'self' would not resolve, so the
// allowances are spelled out and everything else is denied.
const runtimeCSP = "default-src 'none'; " +
	"script-src 'unsafe-inline' 'unsafe-eval'; " +
	"style-src 'unsafe-inline'; " +
	"img-src data:; " +
	"connect-src 'none'; " +
	"form-action 'none'; " +
	"base-uri 'none'"

// configuration holds the settings this plugin needs to reach ai-core.
type configuration struct {
	AiCoreURL   string
	SharedToken string
}

// Plugin is the server component.
type Plugin struct {
	plugin.MattermostPlugin

	mu         sync.RWMutex
	config     configuration
	httpClient *http.Client
}

// artifact is the subset of ai-core's response this plugin reasons about.
// Content is kept raw so a new artifact kind needs no change here.
type artifact struct {
	ID        string          `json:"id"`
	Kind      string          `json:"kind"`
	Status    string          `json:"status"`
	Revision  int             `json:"revision"`
	Title     string          `json:"title"`
	Content   json.RawMessage `json:"content"`
	ChannelID string          `json:"channel_id"`
	PostID    string          `json:"post_id"`
}

// OnActivate prepares the HTTP client, inlines React into the runtime document
// and loads configuration.
func (p *Plugin) OnActivate() error {
	p.httpClient = &http.Client{Timeout: requestTimeout}

	document := strings.Replace(runtimeHTML, "/*__REACT__*/", reactJS, 1)
	document = strings.Replace(document, "/*__REACT_DOM__*/", reactDOMJS, 1)
	document = strings.Replace(document, "/*__BABEL__*/", babelJS, 1)
	runtimeDocument = document

	return p.OnConfigurationChange()
}

// OnConfigurationChange reloads settings. Plugin settings win; environment
// variables are the fallback so a compose-managed deployment can configure the
// plugin without a System Console visit.
func (p *Plugin) OnConfigurationChange() error {
	var loaded configuration
	if p.API != nil {
		// A missing or malformed configuration is not fatal: the environment
		// may carry everything this plugin needs.
		_ = p.API.LoadPluginConfiguration(&loaded)
	}

	if loaded.AiCoreURL == "" {
		loaded.AiCoreURL = os.Getenv("SPRINTFLOW_AICORE_URL")
	}
	if loaded.SharedToken == "" {
		loaded.SharedToken = os.Getenv("SPRINTFLOW_PLUGIN_TOKEN")
	}

	p.mu.Lock()
	p.config = loaded
	p.mu.Unlock()
	return nil
}

// ServeHTTP routes the plugin's own API.
func (p *Plugin) ServeHTTP(c *plugin.Context, w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.NotFound(w, r)
		return
	}

	if r.URL.Path == runtimePath {
		p.serveRuntime(w, r)
		return
	}

	if strings.HasPrefix(r.URL.Path, assetPrefix) {
		p.serveAsset(w, r)
		return
	}

	if !strings.HasPrefix(r.URL.Path, artifactPrefix) {
		http.NotFound(w, r)
		return
	}

	// Set by Mattermost for an authenticated session and stripped from inbound
	// requests. Its absence means the caller has no session at all.
	userID := r.Header.Get("Mattermost-User-Id")
	if userID == "" {
		http.Error(w, `{"error":"not authenticated"}`, http.StatusUnauthorized)
		return
	}

	artifactID := strings.TrimPrefix(r.URL.Path, artifactPrefix)
	if artifactID == "" || strings.Contains(artifactID, "/") {
		http.Error(w, `{"error":"bad artifact id"}`, http.StatusBadRequest)
		return
	}

	p.mu.RLock()
	config := p.config
	p.mu.RUnlock()

	if config.AiCoreURL == "" || config.SharedToken == "" {
		p.API.LogError("sprintflow artifact access is not configured (AiCoreURL / SharedToken)")
		http.Error(w, `{"error":"artifact access not configured"}`, http.StatusServiceUnavailable)
		return
	}

	found, status, err := p.fetchArtifact(config, artifactID)
	if err != nil {
		p.API.LogError("sprintflow artifact fetch failed", "artifact_id", artifactID, "error", err.Error())
		http.Error(w, `{"error":"artifact unavailable"}`, status)
		return
	}

	if !p.viewerMayRead(userID, found) {
		// Deliberately the same answer as "no such artifact": telling an
		// unauthorised caller that an id exists is itself a disclosure.
		p.API.LogWarn("sprintflow artifact access denied", "artifact_id", artifactID, "user_id", userID)
		http.Error(w, `{"error":"not found"}`, http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "private, no-store")

	// Never as an executable document on this origin: generated React source is
	// returned as JSON data and compiled and run inside a sandboxed iframe.
	w.Header().Set("X-Content-Type-Options", "nosniff")

	if err := json.NewEncoder(w).Encode(found); err != nil {
		p.API.LogError("sprintflow artifact encode failed", "error", err.Error())
	}
}

// serveRuntime returns the sandboxed runtime document.
//
// It is deliberately NOT authenticated: it contains no artifact content and no
// data of any kind — only React and a message listener. The component's code
// arrives later by postMessage from the host page, which IS authenticated. That
// keeps this from being "generated HTML served as an executable document on the
// authenticated origin": the executable part is a fixed, reviewed shell.
func (p *Plugin) serveRuntime(w http.ResponseWriter, r *http.Request) {
	userID := r.Header.Get("Mattermost-User-Id")
	if userID == "" {
		http.Error(w, `{"error":"not authenticated"}`, http.StatusUnauthorized)
		return
	}

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Security-Policy", runtimeCSP)
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Cache-Control", "private, max-age=300")
	if _, err := w.Write([]byte(runtimeDocument)); err != nil {
		p.API.LogError("sprintflow runtime write failed", "error", err.Error())
	}
}

// serveAsset returns one lazily loaded webapp chunk.
//
// Chunk names carry a content hash, so they are immutable and cached for a
// year; a rebuild produces new names. The viewer must have a session — these
// are part of the application, not public files — and the name is checked
// against the embedded set rather than the filesystem, so there is no path to
// traverse.
func (p *Plugin) serveAsset(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Mattermost-User-Id") == "" {
		http.Error(w, `{"error":"not authenticated"}`, http.StatusUnauthorized)
		return
	}

	name := strings.TrimPrefix(r.URL.Path, assetPrefix)
	if name == "" || strings.ContainsAny(name, "/\\") || !strings.HasSuffix(name, ".js") {
		http.NotFound(w, r)
		return
	}

	data, err := chunkFiles.ReadFile("assets/chunks/" + name)
	if err != nil {
		http.NotFound(w, r)
		return
	}

	w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Cache-Control", "private, max-age=31536000, immutable")
	if _, err := w.Write(data); err != nil {
		p.API.LogError("sprintflow asset write failed", "asset", name, "error", err.Error())
	}
}

// fetchArtifact reads one artifact from ai-core with the shared secret.
func (p *Plugin) fetchArtifact(config configuration, artifactID string) (*artifact, int, error) {
	url := fmt.Sprintf("%s/api/v1/artifacts/%s", strings.TrimRight(config.AiCoreURL, "/"), artifactID)
	request, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, http.StatusInternalServerError, err
	}
	request.Header.Set("X-SprintFlow-Plugin-Token", config.SharedToken)

	response, err := p.httpClient.Do(request)
	if err != nil {
		return nil, http.StatusBadGateway, err
	}
	defer response.Body.Close()

	if response.StatusCode == http.StatusNotFound {
		return nil, http.StatusNotFound, fmt.Errorf("artifact not found")
	}
	if response.StatusCode != http.StatusOK {
		return nil, http.StatusBadGateway, fmt.Errorf("ai-core returned %d", response.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(response.Body, 8<<20))
	if err != nil {
		return nil, http.StatusBadGateway, err
	}

	var found artifact
	if err := json.Unmarshal(body, &found); err != nil {
		return nil, http.StatusBadGateway, err
	}
	return &found, http.StatusOK, nil
}

// viewerMayRead answers whether this user may see this artifact.
//
// The check is against the POST the artifact was published in wherever one
// exists, because a post can be deleted or moved after the artifact was
// written; the stored channel is the fallback for an artifact whose reply has
// not been published yet.
func (p *Plugin) viewerMayRead(userID string, found *artifact) bool {
	channelID := found.ChannelID

	if found.PostID != "" {
		post, appErr := p.API.GetPost(found.PostID)
		if appErr != nil || post == nil {
			return false
		}
		if post.DeleteAt != 0 {
			return false
		}
		channelID = post.ChannelId
	}

	if channelID == "" {
		return false
	}
	return p.API.HasPermissionToChannel(userID, channelID, model.PermissionReadChannel)
}

func main() {
	plugin.ClientMain(&Plugin{})
}
