package server

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/futureagi/agentcc-gateway/internal/config"
	"github.com/futureagi/agentcc-gateway/internal/models"
	"github.com/futureagi/agentcc-gateway/internal/pipeline"
	"github.com/futureagi/agentcc-gateway/internal/providers"
	"github.com/futureagi/agentcc-gateway/internal/tenant"
)

// newOrgProviderURLTestServer builds a gateway whose config.yaml has an
// "openai" provider pointing at operatorURL, and whose one org ("org-urls")
// has the given providers. The org's key is sk-agentcc-org-urls.
func newOrgProviderURLTestServer(t *testing.T, operatorURL string, orgProviders map[string]*tenant.ProviderConfig) *Server {
	t.Helper()

	cfg := config.DefaultConfig()
	cfg.Auth.Enabled = true
	cfg.Auth.Keys = []config.AuthKeyConfig{{
		Name:     "org-user",
		Key:      "sk-agentcc-org-urls",
		Owner:    "user",
		KeyType:  "byok",
		Metadata: map[string]string{"org_id": "org-urls"},
	}}
	cfg.Providers["openai"] = config.ProviderConfig{
		BaseURL:   operatorURL,
		APIKey:    "operator-key",
		APIFormat: "openai",
		Models:    []string{"gpt-4o"},
	}

	registry, err := providers.NewRegistry(cfg)
	if err != nil {
		t.Fatalf("creating registry: %v", err)
	}

	tenantStore := tenant.NewStore()
	tenantStore.Set("org-urls", &tenant.OrgConfig{Providers: orgProviders})

	srv := New(cfg, "", registry, pipeline.NewEngine(), nil, nil, nil, nil, testModelDBPtr(), tenantStore, nil)
	srv.ready.Store(true)
	return srv
}

func postChat(t *testing.T, srv *Server, model string) (int, models.ErrorDetail) {
	t.Helper()
	body := `{"model":"` + model + `","messages":[{"role":"user","content":"hi"}]}`
	req := httptest.NewRequest("POST", "/v1/chat/completions", bytes.NewBufferString(body))
	req.Header.Set("Authorization", "Bearer sk-agentcc-org-urls")
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	srv.httpServer.Handler.ServeHTTP(w, req)

	var resp models.ErrorResponse
	_ = json.Unmarshal(w.Body.Bytes(), &resp)
	return w.Code, resp.Error
}

// A private base_url is refused with the reason and the opt-in, not the
// generic "not available for this API key".
func TestOrgProviderPrivateBaseURLRefusalExplainsOptIn(t *testing.T) {
	operator := startMockOpenAI(t)
	defer operator.Close()

	srv := newOrgProviderURLTestServer(t, operator.URL, map[string]*tenant.ProviderConfig{
		"custom": {
			APIKey:    "org-key",
			BaseURL:   "http://10.255.255.1:8080",
			APIFormat: "openai",
			Models:    []string{"mock-custom"},
			Enabled:   true,
		},
	})

	status, apiErr := postChat(t, srv, "mock-custom")

	if status != http.StatusForbidden || apiErr.Code != "provider_base_url_blocked" {
		t.Fatalf("status = %d code = %q, want 403 provider_base_url_blocked; message: %s", status, apiErr.Code, apiErr.Message)
	}
	if !strings.Contains(apiErr.Message, config.EnvAllowPrivateProviderURLs+"=true") {
		t.Errorf("message = %q, want it to name %s", apiErr.Message, config.EnvAllowPrivateProviderURLs)
	}
	if strings.Contains(apiErr.Message, "not available for this API key") {
		t.Errorf("message = %q still blames the API key", apiErr.Message)
	}
	if strings.Contains(apiErr.Message, "10.255.255.1") {
		t.Errorf("message = %q echoes the resolved address", apiErr.Message)
	}
}

// A model no org provider lists keeps the plain refusal: org keys are barred
// from config.yaml providers by design.
func TestUnlistedModelStillNotAvailableForOrgKey(t *testing.T) {
	operator := startMockOpenAI(t)
	defer operator.Close()

	srv := newOrgProviderURLTestServer(t, operator.URL, map[string]*tenant.ProviderConfig{
		"custom": {
			APIKey:  "org-key",
			BaseURL: "http://10.255.255.1:8080",
			Models:  []string{"mock-custom"},
			Enabled: true,
		},
	})

	status, apiErr := postChat(t, srv, "gpt-4o")

	if status != http.StatusForbidden || !strings.Contains(apiErr.Message, "not available for this API key") {
		t.Fatalf("status = %d message = %q, want 403 not available for this API key", status, apiErr.Message)
	}
}
