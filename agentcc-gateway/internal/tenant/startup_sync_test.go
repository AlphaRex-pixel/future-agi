package tenant

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/futureagi/agentcc-gateway/internal/auth"
	"github.com/futureagi/agentcc-gateway/internal/config"
)

// logRecorder captures slog records so a test can check their levels.
type logRecorder struct {
	mu      sync.Mutex
	records []slog.Record
}

func (r *logRecorder) Enabled(context.Context, slog.Level) bool { return true }
func (r *logRecorder) Handle(_ context.Context, rec slog.Record) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.records = append(r.records, rec)
	return nil
}
func (r *logRecorder) WithAttrs([]slog.Attr) slog.Handler { return r }
func (r *logRecorder) WithGroup(string) slog.Handler      { return r }

// count reports how many records were logged at level, and how many of those
// had message msg (any message when msg is empty).
func (r *logRecorder) count(level slog.Level, msg string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := 0
	for _, rec := range r.records {
		if rec.Level == level && (msg == "" || rec.Message == msg) {
			n++
		}
	}
	return n
}

func recordLogs(t *testing.T) *logRecorder {
	t.Helper()
	rec := &logRecorder{}
	prev := slog.Default()
	slog.SetDefault(slog.New(rec))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return rec
}

// shortenStartupSync runs the startup sync on a test-sized schedule.
func shortenStartupSync(t *testing.T, firstRetry, maxRetry, warnAfter, warnEvery time.Duration) {
	t.Helper()
	saved := []time.Duration{startupSyncFirstRetry, startupSyncMaxRetry, startupSyncWarnAfter, startupSyncWarnEvery}
	startupSyncFirstRetry, startupSyncMaxRetry, startupSyncWarnAfter, startupSyncWarnEvery = firstRetry, maxRetry, warnAfter, warnEvery
	t.Cleanup(func() {
		startupSyncFirstRetry, startupSyncMaxRetry, startupSyncWarnAfter, startupSyncWarnEvery = saved[0], saved[1], saved[2], saved[3]
	})
}

// fakeControlPlane answers 503 on both bulk endpoints while down reports true,
// then serves one org and one API key (sk-agentcc-ui-key, as key_7).
type fakeControlPlane struct {
	*httptest.Server
	orgRequests atomic.Int32
	keyRequests atomic.Int32
}

func newFakeControlPlane(t *testing.T, down func() bool) *fakeControlPlane {
	t.Helper()
	cp := &fakeControlPlane{}
	cp.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/agentcc/org-configs/bulk/":
			cp.orgRequests.Add(1)
			if down() {
				w.WriteHeader(http.StatusServiceUnavailable)
				return
			}
			json.NewEncoder(w).Encode(map[string]any{
				"status": true,
				"result": map[string]any{"org-1": map[string]any{}},
			})
		case "/agentcc/api-keys/bulk/":
			cp.keyRequests.Add(1)
			if down() {
				w.WriteHeader(http.StatusServiceUnavailable)
				return
			}
			json.NewEncoder(w).Encode(map[string]any{
				"status": true,
				"result": []map[string]any{{
					"id": "key_7", "name": "ui-key", "key_hash": auth.HashKey("sk-agentcc-ui-key"),
					"metadata": map[string]string{"org_id": "org-1"},
				}},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(cp.Close)
	return cp
}

// downFor is a control plane that fails its first n requests per endpoint.
func downFor(n int32, cp **fakeControlPlane) func() bool {
	return func() bool { return (*cp).orgRequests.Load() <= n && (*cp).keyRequests.Load() <= n }
}

func assertSynced(t *testing.T, store *Store, ks *auth.KeyStore) {
	t.Helper()
	if store.Get("org-1") == nil {
		t.Error("org-1 not loaded from the control plane")
	}
	if k := ks.Authenticate("sk-agentcc-ui-key"); k == nil || k.ID != "key_7" {
		t.Errorf("UI key after sync = %+v, want key_7", k)
	}
}

// A control plane that is still starting when the gateway boots is retried
// quietly: INFO per retry, no WARN, and keys and orgs load once it answers.
func TestSyncOnStartup_RetriesQuietlyWhileTheControlPlaneStarts(t *testing.T) {
	logs := recordLogs(t)
	shortenStartupSync(t, time.Millisecond, 4*time.Millisecond, time.Hour, time.Hour)
	var cp *fakeControlPlane
	cp = newFakeControlPlane(t, downFor(3, &cp))
	store, ks := NewStore(), auth.NewKeyStore(config.AuthConfig{})

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if !SyncOnStartup(ctx, cp.URL, "token", store, ks) {
		t.Fatal("SyncOnStartup gave up")
	}

	assertSynced(t, store, ks)
	if n := logs.count(slog.LevelWarn, "") + logs.count(slog.LevelError, ""); n != 0 {
		t.Errorf("logged %d WARN/ERROR records while the control plane was starting, want 0", n)
	}
	if n := logs.count(slog.LevelInfo, "control plane not ready yet, retrying startup sync"); n != 3 {
		t.Errorf("logged %d INFO retries, want 3", n)
	}
	if n := logs.count(slog.LevelInfo, "control plane startup sync succeeded"); n != 1 {
		t.Errorf("logged %d success records, want 1", n)
	}
}

// The startup sync used to stop after 60 attempts (2 minutes), leaving a slow
// first boot with no keys until the gateway restarted.
func TestSyncOnStartup_DoesNotGiveUp(t *testing.T) {
	recordLogs(t)
	shortenStartupSync(t, time.Millisecond, time.Millisecond, time.Hour, time.Hour)
	var cp *fakeControlPlane
	cp = newFakeControlPlane(t, downFor(75, &cp))
	store, ks := NewStore(), auth.NewKeyStore(config.AuthConfig{})

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if !SyncOnStartup(ctx, cp.URL, "token", store, ks) {
		t.Fatal("SyncOnStartup gave up")
	}
	assertSynced(t, store, ks)
}

// A long outage is one WARN (then at most one per startupSyncWarnEvery), and
// the sync still completes when the control plane comes back.
func TestSyncOnStartup_WarnsOnceWhenTheOutageIsLong(t *testing.T) {
	logs := recordLogs(t)
	shortenStartupSync(t, time.Millisecond, 5*time.Millisecond, 50*time.Millisecond, time.Hour)
	var down atomic.Bool
	down.Store(true)
	cp := newFakeControlPlane(t, down.Load)
	store, ks := NewStore(), auth.NewKeyStore(config.AuthConfig{})

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	done := make(chan bool, 1)
	go func() { done <- SyncOnStartup(ctx, cp.URL, "token", store, ks) }()

	// Keep failing well past the first WARN.
	deadline := time.Now().Add(5 * time.Second)
	for logs.count(slog.LevelWarn, "") == 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	time.Sleep(100 * time.Millisecond)
	down.Store(false)

	if !<-done {
		t.Fatal("SyncOnStartup gave up")
	}
	assertSynced(t, store, ks)
	if n := logs.count(slog.LevelWarn, ""); n != 1 {
		t.Errorf("logged %d WARN records, want 1", n)
	}
	if n := logs.count(slog.LevelError, ""); n != 0 {
		t.Errorf("logged %d ERROR records, want 0", n)
	}
	if n := logs.count(slog.LevelInfo, "control plane not ready yet, retrying startup sync"); n < 5 {
		t.Errorf("logged %d INFO retries, want the retries around the WARN at INFO", n)
	}
}

func TestSyncOnStartup_StopsWhenTheContextEnds(t *testing.T) {
	recordLogs(t)
	shortenStartupSync(t, time.Hour, time.Hour, time.Hour, time.Hour)
	cp := newFakeControlPlane(t, func() bool { return true })

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan bool, 1)
	go func() { done <- SyncOnStartup(ctx, cp.URL, "token", NewStore(), auth.NewKeyStore(config.AuthConfig{})) }()
	for cp.orgRequests.Load() == 0 {
		time.Sleep(time.Millisecond)
	}
	cancel()

	select {
	case ok := <-done:
		if ok {
			t.Fatal("SyncOnStartup reported success against a control plane that never answered")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("SyncOnStartup still waiting after its context ended")
	}
}

// With an interval, the periodic re-sync takes over once the startup sync has
// loaded everything, and not before: a starting control plane is polled by
// one loop, which does not warn.
func TestRunControlPlaneSync_PeriodicSyncFollowsTheStartupSync(t *testing.T) {
	logs := recordLogs(t)
	shortenStartupSync(t, 20*time.Millisecond, 20*time.Millisecond, time.Hour, time.Hour)
	var cp *fakeControlPlane
	cp = newFakeControlPlane(t, downFor(2, &cp))
	store, ks := NewStore(), auth.NewKeyStore(config.AuthConfig{})

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		RunControlPlaneSync(ctx, true, 5*time.Millisecond, cp.URL, "token", store, ks)
		close(done)
	}()

	// Two failures and one success from the startup sync, then periodic ticks.
	deadline := time.Now().Add(5 * time.Second)
	for cp.orgRequests.Load() < 6 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	// Counted before cancelling: a periodic sync cut short by shutdown warns.
	warns := logs.count(slog.LevelWarn, "")
	cancel()
	<-done

	if n := cp.orgRequests.Load(); n < 6 {
		t.Fatalf("control plane got %d org sync requests, want the periodic sync running after startup", n)
	}
	assertSynced(t, store, ks)
	if warns != 0 {
		t.Errorf("logged %d WARN records: the periodic sync ran while the control plane was starting", warns)
	}
}
