package auth

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"
)

// keySyncHTTPClient is a shared, reusable HTTP client for key sync.
var keySyncHTTPClient = &http.Client{Timeout: 15 * time.Second}

// SyncKeysFromControlPlane fetches all active API key hashes from the Django
// control plane and loads them into the KeyStore. A failure leaves the store
// as it was (config.yaml seed keys only, on a fresh start) and is returned for
// the caller to log.
func SyncKeysFromControlPlane(ctx context.Context, baseURL, adminToken string, ks *KeyStore) error {
	if baseURL == "" {
		slog.Info("key sync skipped: no control plane URL configured")
		return nil
	}

	endpoint := baseURL + "/agentcc/api-keys/bulk/"

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return fmt.Errorf("building key sync request: %w", err)
	}
	if adminToken != "" {
		req.Header.Set("Authorization", "Bearer "+adminToken)
	}

	resp, err := keySyncHTTPClient.Do(req)
	if err != nil {
		return fmt.Errorf("key sync unreachable: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return fmt.Errorf("key sync returned status %d from %s: %s", resp.StatusCode, endpoint, body)
	}

	// Django response format: {"status": true, "result": [...]}
	var envelope struct {
		Status bool        `json:"status"`
		Result []SyncedKey `json:"result"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 10<<20)).Decode(&envelope); err != nil {
		return fmt.Errorf("parsing key sync response: %w", err)
	}

	if !envelope.Status {
		return fmt.Errorf("key sync: status=false")
	}

	if len(envelope.Result) == 0 {
		slog.Warn("key sync: control plane returned empty key set",
			"url", endpoint,
		)
	}

	loaded := ks.SyncFromHashes(envelope.Result)
	slog.Info("key sync from control plane completed",
		"keys_received", len(envelope.Result),
		"keys_synced", loaded,
	)
	return nil
}
