package auth

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/futureagi/agentcc-gateway/internal/config"
)

// A restarted gateway pointed at the control plane gets every active key back
// under the ID Django stores it by, next to its config.yaml keys.
func TestSyncKeysFromControlPlane_RestoresKeysAfterRestart(t *testing.T) {
	var gotAuth string
	controlPlane := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/agentcc/api-keys/bulk/" {
			http.NotFound(w, r)
			return
		}
		gotAuth = r.Header.Get("Authorization")
		json.NewEncoder(w).Encode(map[string]any{
			"status": true,
			"result": []map[string]any{
				{"id": "key_2", "name": "ui-key", "key_hash": HashKey("sk-agentcc-ui-key"), "key_prefix": "sk-agentcc-u...",
					"metadata": map[string]string{"org_id": "org-1"}},
				{"id": "key_5c0ffee", "name": "newer", "key_hash": HashKey("sk-agentcc-newer")},
			},
		})
	}))
	defer controlPlane.Close()

	// Fresh process: only the env-seeded internal key, which takes key_1.
	ks := NewKeyStore(authCfg(config.AuthKeyConfig{Name: "internal-backend", Key: "sk-agentcc-internal", KeyType: "internal"}))

	if err := SyncKeysFromControlPlane(context.Background(), controlPlane.URL, "shared-admin-token", ks); err != nil {
		t.Fatalf("SyncKeysFromControlPlane: %v", err)
	}

	if gotAuth != "Bearer shared-admin-token" {
		t.Errorf("Authorization = %q, want the control-plane token", gotAuth)
	}
	if k := ks.Authenticate("sk-agentcc-ui-key"); k == nil || k.ID != "key_2" || k.Metadata["org_id"] != "org-1" {
		t.Fatalf("UI key after restart = %+v, want key_2 for org-1", k)
	}
	if k := ks.Authenticate("sk-agentcc-newer"); k == nil || k.ID != "key_5c0ffee" {
		t.Fatalf("second key after restart = %+v", k)
	}
	if k := ks.Authenticate("sk-agentcc-internal"); k == nil || k.ID != "key_1" {
		t.Fatalf("internal key after sync = %+v, want untouched key_1", k)
	}
}
