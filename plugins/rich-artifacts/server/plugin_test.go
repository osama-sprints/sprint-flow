package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/mattermost/mattermost/server/public/model"
	"github.com/mattermost/mattermost/server/public/plugin/plugintest"
	"github.com/stretchr/testify/mock"
)

// newPlugin returns a plugin wired to a mock API and an ai-core stand-in.
func newPlugin(t *testing.T, aiCore http.HandlerFunc) (*Plugin, *plugintest.API, *httptest.Server) {
	t.Helper()
	server := httptest.NewServer(aiCore)
	t.Cleanup(server.Close)

	api := &plugintest.API{}
	api.On("LogError", mock.Anything, mock.Anything, mock.Anything, mock.Anything, mock.Anything).Maybe()
	api.On("LogWarn", mock.Anything, mock.Anything, mock.Anything, mock.Anything, mock.Anything).Maybe()

	p := &Plugin{httpClient: server.Client()}
	p.SetAPI(api)
	p.config = configuration{AiCoreURL: server.URL, SharedToken: "secret"}
	runtimeDocument = "<html>runtime</html>"
	return p, api, server
}

func serve(p *Plugin, method, path, body string, userID string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if userID != "" {
		req.Header.Set("Mattermost-User-Id", userID)
	}
	rec := httptest.NewRecorder()
	p.ServeHTTP(nil, rec, req)
	return rec
}

func aiCoreArtifact(channelID, postID string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-SprintFlow-Plugin-Token") != "secret" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"id": "art-1", "kind": "mermaid", "status": "ready", "revision": 1,
			"content":    map[string]string{"definition": "graph TD; SECRET-->B;"},
			"channel_id": channelID, "post_id": postID,
		})
	}
}

func TestArtifactRequiresASession(t *testing.T) {
	p, _, _ := newPlugin(t, aiCoreArtifact("chan", "post"))
	rec := serve(p, http.MethodGet, "/api/v1/artifacts/art-1", "", "")
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("anonymous read: want 401, got %d", rec.Code)
	}
}

func TestArtifactIsHiddenFromAViewerWithoutChannelAccess(t *testing.T) {
	p, api, _ := newPlugin(t, aiCoreArtifact("chan", "post"))
	api.On("GetPost", "post").Return(&model.Post{Id: "post", ChannelId: "chan"}, nil)
	api.On("HasPermissionToChannel", "outsider", "chan", model.PermissionReadChannel).Return(false)

	rec := serve(p, http.MethodGet, "/api/v1/artifacts/art-1", "", "outsider")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("outsider read: want 404 (not 403 — existence must not leak), got %d", rec.Code)
	}
	if strings.Contains(rec.Body.String(), "SECRET") {
		t.Fatal("content leaked to an unauthorised viewer")
	}
}

func TestArtifactIsServedToAChannelMember(t *testing.T) {
	p, api, _ := newPlugin(t, aiCoreArtifact("chan", "post"))
	api.On("GetPost", "post").Return(&model.Post{Id: "post", ChannelId: "chan"}, nil)
	api.On("HasPermissionToChannel", "member", "chan", model.PermissionReadChannel).Return(true)

	rec := serve(p, http.MethodGet, "/api/v1/artifacts/art-1", "", "member")
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "SECRET") {
		t.Fatalf("member read: want 200 with content, got %d %s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Fatal("artifact JSON must be served nosniff")
	}
}

func TestDeletedPostHidesItsArtifact(t *testing.T) {
	p, api, _ := newPlugin(t, aiCoreArtifact("chan", "post"))
	api.On("GetPost", "post").Return(&model.Post{Id: "post", ChannelId: "chan", DeleteAt: 1}, nil)

	rec := serve(p, http.MethodGet, "/api/v1/artifacts/art-1", "", "member")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("deleted post: want 404, got %d", rec.Code)
	}
}

func TestRuntimeAndAssetsRequireASessionAndRejectTraversal(t *testing.T) {
	p, _, _ := newPlugin(t, func(w http.ResponseWriter, r *http.Request) {})
	if rec := serve(p, http.MethodGet, "/api/v1/runtime", "", ""); rec.Code != http.StatusUnauthorized {
		t.Fatalf("anonymous runtime: want 401, got %d", rec.Code)
	}
	rec := serve(p, http.MethodGet, "/api/v1/runtime", "", "member")
	if rec.Code != http.StatusOK || rec.Header().Get("Content-Security-Policy") == "" {
		t.Fatalf("runtime must be served with its own CSP, got %d", rec.Code)
	}
	if rec := serve(p, http.MethodGet, "/api/v1/assets/..%2Fplugin.go", "", "member"); rec.Code != http.StatusNotFound {
		t.Fatalf("traversal: want 404, got %d", rec.Code)
	}
	if rec := serve(p, http.MethodGet, "/api/v1/assets/nope.js", "", "member"); rec.Code != http.StatusNotFound {
		t.Fatalf("unknown asset: want 404, got %d", rec.Code)
	}
}
