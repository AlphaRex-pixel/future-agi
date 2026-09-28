package tenant

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/futureagi/agentcc-gateway/internal/auth"
)

// Startup sync schedule. The control plane is usually still starting when the
// gateway boots (a first boot can spend minutes on migrations), so failed
// attempts are expected: they log at INFO, and only an outage longer than
// startupSyncWarnAfter logs a WARN, repeated at most every
// startupSyncWarnEvery. Variables so tests can shorten them.
var (
	startupSyncFirstRetry = 2 * time.Second
	startupSyncMaxRetry   = 30 * time.Second
	startupSyncWarnAfter  = 2 * time.Minute
	startupSyncWarnEvery  = 5 * time.Minute
	startupSyncTimeout    = 10 * time.Second // per request
)

// SyncOnStartup loads org configs and API keys from the control plane,
// retrying with capped exponential backoff until both have loaded or ctx ends.
// It does not give up on its own: until it succeeds, the gateway serves only
// its config.yaml keys. Run it in a goroutine so startup never waits on it.
// Reports whether the sync completed.
func SyncOnStartup(ctx context.Context, baseURL, adminToken string, store *Store, keyStore *auth.KeyStore) bool {
	start := time.Now()
	var lastWarn time.Time
	wait := startupSyncFirstRetry
	orgsSynced := false
	keysSynced := keyStore == nil

	for attempt := 1; ; attempt++ {
		var errs []error
		if !orgsSynced {
			reqCtx, cancel := context.WithTimeout(ctx, startupSyncTimeout)
			if err := SyncFromControlPlane(reqCtx, baseURL, adminToken, store); err != nil {
				errs = append(errs, err)
			} else {
				orgsSynced = true
			}
			cancel()
		}
		if !keysSynced {
			reqCtx, cancel := context.WithTimeout(ctx, startupSyncTimeout)
			if err := auth.SyncKeysFromControlPlane(reqCtx, baseURL, adminToken, keyStore); err != nil {
				errs = append(errs, err)
			} else {
				keysSynced = true
			}
			cancel()
		}

		if orgsSynced && keysSynced {
			keyCount := 0
			if keyStore != nil {
				keyCount = keyStore.Count()
			}
			slog.Info("control plane startup sync succeeded",
				"attempts", attempt, "after", time.Since(start).Round(time.Second).String(),
				"orgs", store.Count(), "keys", keyCount)
			return true
		}
		if ctx.Err() != nil {
			return false
		}

		err := errors.Join(errs...)
		failingFor := time.Since(start)
		if failingFor >= startupSyncWarnAfter && (lastWarn.IsZero() || time.Since(lastWarn) >= startupSyncWarnEvery) {
			lastWarn = time.Now()
			slog.Warn("control plane sync still failing: the gateway serves only its config.yaml keys until keys and org settings load from the app; still retrying",
				"failing_for", failingFor.Round(time.Second).String(), "attempts", attempt, "error", err)
		} else {
			slog.Info("control plane not ready yet, retrying startup sync",
				"attempt", attempt, "retry_in", wait.String(),
				"need_orgs", !orgsSynced, "need_keys", !keysSynced, "error", err)
		}

		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			timer.Stop()
			return false
		case <-timer.C:
		}
		wait = min(wait*2, startupSyncMaxRetry)
	}
}

// RunControlPlaneSync runs the startup sync when startup is set, then the
// periodic re-sync every interval (0 = none) until ctx ends. One loop at a
// time, so a control plane that is still starting is not polled twice. Blocks;
// run it in a goroutine.
func RunControlPlaneSync(ctx context.Context, startup bool, interval time.Duration, baseURL, adminToken string, store *Store, keyStore *auth.KeyStore) {
	if startup && !SyncOnStartup(ctx, baseURL, adminToken, store, keyStore) {
		return // ctx ended
	}
	StartPeriodicSync(ctx, interval, baseURL, adminToken, store, keyStore)
}
