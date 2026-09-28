package providers

import (
	"errors"
	"net"
	"strings"
	"testing"

	"github.com/futureagi/agentcc-gateway/internal/config"
	"github.com/futureagi/agentcc-gateway/internal/tenant"
)

// ---------------------------------------------------------------------------
// resolveOrgConfig: the tenant's path prefix has to survive a provider that is
// also present in config.yaml, which is the path that inherits the YAML prefix.
// ---------------------------------------------------------------------------

func TestResolveOrgConfig_PathPrefix(t *testing.T) {
	ptr := func(s string) *string { return &s }

	tests := []struct {
		name      string
		yaml      *string
		tenantCfg *tenant.ProviderConfig
		want      string
	}{
		{
			name:      "no tenant config keeps the yaml prefix",
			yaml:      ptr("/v1"),
			tenantCfg: nil,
			want:      "/v1",
		},
		{
			name:      "tenant that states nothing keeps the yaml prefix",
			yaml:      ptr("/v1"),
			tenantCfg: &tenant.ProviderConfig{},
			want:      "/v1",
		},
		{
			name:      "explicitly empty tenant prefix overrides the yaml one",
			yaml:      ptr("/v1"),
			tenantCfg: &tenant.ProviderConfig{APIPathPrefix: ptr("")},
			want:      "",
		},
		{
			name:      "non-empty tenant prefix overrides the yaml one",
			yaml:      ptr("/v1"),
			tenantCfg: &tenant.ProviderConfig{APIPathPrefix: ptr("/openai/v1")},
			want:      "/openai/v1",
		},
		{
			name:      "tenant prefix applies where yaml states none",
			yaml:      nil,
			tenantCfg: &tenant.ProviderConfig{APIPathPrefix: ptr("")},
			want:      "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			baseCfg := config.ProviderConfig{
				BaseURL:       "https://api.perplexity.ai",
				APIKey:        "yaml-key",
				APIFormat:     "openai",
				APIPathPrefix: tt.yaml,
			}

			got := resolveOrgConfig(baseCfg, "org-key", tt.tenantCfg)

			if got.EffectiveAPIPathPrefix() != tt.want {
				t.Errorf("EffectiveAPIPathPrefix() = %q, want %q",
					got.EffectiveAPIPathPrefix(), tt.want)
			}
			if got.APIKey != "org-key" {
				t.Errorf("APIKey = %q, want the org's key", got.APIKey)
			}
			if baseCfg.APIPathPrefix != tt.yaml {
				t.Error("resolveOrgConfig mutated the shared base config")
			}
		})
	}
}

// The endpoint the proxy actually builds, so an empty prefix on a YAML-backed
// provider stops sending requests to /v1.
func TestResolveOrgConfig_EmptyPrefixDropsVersionSegment(t *testing.T) {
	empty := ""
	v1 := "/v1"
	baseCfg := config.ProviderConfig{
		BaseURL:       "https://api.perplexity.ai",
		APIFormat:     "openai",
		APIPathPrefix: &v1,
	}

	orgCfg := resolveOrgConfig(baseCfg, "org-key", &tenant.ProviderConfig{APIPathPrefix: &empty})

	const want = "https://api.perplexity.ai/chat/completions"
	if got := orgCfg.EndpointURL("/v1/chat/completions"); got != want {
		t.Errorf("EndpointURL() = %q, want %q", got, want)
	}
}

// ---------------------------------------------------------------------------
// Org base URLs and the private-address opt-in.
// ---------------------------------------------------------------------------

// stubLookupIP makes validateBaseURL see the given addresses for host names.
func stubLookupIP(t *testing.T, answers map[string][]string) {
	t.Helper()
	orig := lookupIP
	lookupIP = func(host string) ([]net.IP, error) {
		addrs, ok := answers[host]
		if !ok {
			return nil, &net.DNSError{Err: "no such host", Name: host, IsNotFound: true}
		}
		ips := make([]net.IP, len(addrs))
		for i, a := range addrs {
			ips[i] = net.ParseIP(a)
		}
		return ips, nil
	}
	t.Cleanup(func() { lookupIP = orig })
}

func TestGetOrCreate_PrivateBaseURLNeedsOptIn(t *testing.T) {
	stubLookupIP(t, map[string][]string{"mock-llm": {"172.20.0.5"}})
	tenantCfg := &tenant.ProviderConfig{APIKey: "org-key", BaseURL: "http://mock-llm:8080", Enabled: true}

	cache := NewOrgProviderCache(nil)
	if _, err := cache.GetOrCreateWithTenantConfig("org-1", "custom", "org-key", tenantCfg); err == nil {
		t.Fatal("private base_url accepted without the opt-in")
	}

	cache.SetAllowPrivateBaseURLs(true)
	if _, err := cache.GetOrCreateWithTenantConfig("org-1", "custom", "org-key", tenantCfg); err != nil {
		t.Fatalf("private base_url refused with the opt-in: %v", err)
	}
	if cache.Count() != 1 {
		t.Fatalf("Count() = %d, want 1", cache.Count())
	}

	// Turning the opt-in off must not leave providers built under it behind.
	cache.SetAllowPrivateBaseURLs(false)
	if cache.Count() != 0 {
		t.Errorf("Count() = %d after SetAllowPrivateBaseURLs(false), want 0", cache.Count())
	}
}

// ---------------------------------------------------------------------------
// validateBaseURL: which org base URLs the gateway will call.
// ---------------------------------------------------------------------------

func TestValidateBaseURL(t *testing.T) {
	stubLookupIP(t, map[string][]string{
		"api.openai.com":       {"104.18.6.192"},
		"mock-llm":             {"172.20.0.5"},
		"host.docker.internal": {"192.168.65.254"},
		"ollama.lan":           {"10.0.0.12"},
		"tailnet-box":          {"100.101.102.103"},
		"ula-box":              {"fd12:3456::1"},
		"localhost":            {"127.0.0.1", "::1"},
		"split-horizon":        {"104.18.6.192", "10.0.0.12"},
		"rebind-to-metadata":   {"169.254.169.254"},
		"empty-answer":         {},
	})

	const pass = -1
	tests := []struct {
		url          string
		denied       BaseURLRejection // or pass
		allowPrivate BaseURLRejection // with the opt-in; or pass
	}{
		{"https://api.openai.com", pass, pass},
		{"http://8.8.8.8:8080", pass, pass},
		{"", pass, pass},

		// Private/LAN: refused by default, allowed with the opt-in.
		{"http://mock-llm:8080", BaseURLPrivate, pass},
		{"http://host.docker.internal:11434", BaseURLPrivate, pass},
		{"http://ollama.lan:11434", BaseURLPrivate, pass},
		{"http://10.1.2.3", BaseURLPrivate, pass},
		{"http://172.31.255.254", BaseURLPrivate, pass},
		{"http://192.168.1.10:8000", BaseURLPrivate, pass},
		{"http://100.64.0.1", BaseURLPrivate, pass},
		{"http://tailnet-box", BaseURLPrivate, pass},
		{"http://[fd12:3456::1]:8000", BaseURLPrivate, pass},
		{"http://ula-box", BaseURLPrivate, pass},
		{"http://split-horizon", BaseURLPrivate, pass},

		// Loopback: never.
		{"http://127.0.0.1:8080", BaseURLLoopback, BaseURLLoopback},
		{"http://127.9.9.9", BaseURLLoopback, BaseURLLoopback},
		{"http://[::1]:8080", BaseURLLoopback, BaseURLLoopback},
		{"http://localhost:11434", BaseURLLoopback, BaseURLLoopback},
		{"http://[::ffff:127.0.0.1]", BaseURLLoopback, BaseURLLoopback},

		// Link-local, metadata, unspecified, multicast: never.
		{"http://169.254.169.254/latest/meta-data", BaseURLForbidden, BaseURLForbidden},
		{"http://169.254.170.2", BaseURLForbidden, BaseURLForbidden},
		{"http://[::ffff:169.254.169.254]", BaseURLForbidden, BaseURLForbidden},
		{"http://[fe80::1]", BaseURLForbidden, BaseURLForbidden},
		{"http://metadata.google.internal/computeMetadata/v1", BaseURLForbidden, BaseURLForbidden},
		{"http://METADATA.google.internal.", BaseURLForbidden, BaseURLForbidden},
		{"http://metadata", BaseURLForbidden, BaseURLForbidden},
		{"http://rebind-to-metadata", BaseURLForbidden, BaseURLForbidden},
		{"http://100.100.100.200", BaseURLForbidden, BaseURLForbidden},
		{"http://[fd00:ec2::254]", BaseURLForbidden, BaseURLForbidden},
		{"http://168.63.129.16", BaseURLForbidden, BaseURLForbidden},
		{"http://0.0.0.0:8080", BaseURLForbidden, BaseURLForbidden},
		{"http://[::]:8080", BaseURLForbidden, BaseURLForbidden},
		{"http://224.0.0.1", BaseURLForbidden, BaseURLForbidden},

		// Not usable at all.
		{"ftp://files.example.com", BaseURLInvalid, BaseURLInvalid},
		{"http://", BaseURLInvalid, BaseURLInvalid},
		{"http://no-such-host.invalid", BaseURLUnresolvable, BaseURLUnresolvable},
		{"http://empty-answer", BaseURLUnresolvable, BaseURLUnresolvable},
	}

	check := func(t *testing.T, url string, allowPrivate bool, want BaseURLRejection) {
		t.Helper()
		err := validateBaseURL(url, allowPrivate)
		if want == pass {
			if err != nil {
				t.Errorf("validateBaseURL(%q, allowPrivate=%v) = %v, want nil", url, allowPrivate, err)
			}
			return
		}
		var urlErr *BaseURLError
		if !errors.As(err, &urlErr) {
			t.Errorf("validateBaseURL(%q, allowPrivate=%v) = %v, want a *BaseURLError", url, allowPrivate, err)
			return
		}
		if urlErr.Reason != want {
			t.Errorf("validateBaseURL(%q, allowPrivate=%v) reason = %d (%v), want %d", url, allowPrivate, urlErr.Reason, err, want)
		}
	}
	for _, tt := range tests {
		t.Run(tt.url, func(t *testing.T) {
			check(t, tt.url, false, tt.denied)
			check(t, tt.url, true, tt.allowPrivate)
		})
	}
}

// The message an API caller sees names the fix for a private address and never
// echoes the resolved IP.
func TestBaseURLError_PublicMessage(t *testing.T) {
	stubLookupIP(t, map[string][]string{"mock-llm": {"172.20.0.5"}})

	var urlErr *BaseURLError
	if !errors.As(validateBaseURL("http://mock-llm:8080", false), &urlErr) {
		t.Fatal("private base_url not refused")
	}
	msg := urlErr.PublicMessage()
	if !strings.Contains(msg, config.EnvAllowPrivateProviderURLs+"=true") {
		t.Errorf("PublicMessage() = %q, want it to name %s", msg, config.EnvAllowPrivateProviderURLs)
	}
	if strings.Contains(msg, "172.20.0.5") {
		t.Errorf("PublicMessage() = %q leaks the resolved address", msg)
	}
	if !strings.Contains(urlErr.Error(), "172.20.0.5") {
		t.Errorf("Error() = %q, want the resolved address for the operator's logs", urlErr.Error())
	}
}
