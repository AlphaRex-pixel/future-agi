package tenant

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
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

// messages returns the messages of the records logged at level.
func (r *logRecorder) messages(level slog.Level) []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	var msgs []string
	for _, rec := range r.records {
		if rec.Level == level {
			msgs = append(msgs, rec.Message)
		}
	}
	return msgs
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
// then serves one org and one API key (sk-agentcc-ui-key, as key_7). Setting
// orgsBroken or keysBroken makes that endpoint answer 500 regardless.
type fakeControlPlane struct {
	*httptest.Server
	orgRequests atomic.Int32
	keyRequests atomic.Int32
	orgsBroken  atomic.Bool
	keysBroken  atomic.Bool
}

func newFakeControlPlane(t *testing.T, down func() bool) *fakeControlPlane {
	t.Helper()
	cp := &fakeControlPlane{}
	cp.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/agentcc/org-configs/bulk/":
			cp.orgRequests.Add(1)
			if cp.orgsBroken.Load() {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
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
			if cp.keysBroken.Load() {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
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

// The WARN for a long outage names what is still missing: keys that have
// loaded are not reported as missing.
func TestSyncOnStartup_WarningNamesWhatIsMissing(t *testing.T) {
	for _, tc := range []struct {
		name       string
		breakOrgs  bool
		breakKeys  bool
		wantPhrase string
		notPhrase  string
	}{
		{"orgs failing", true, false, "org settings", "keys"},
		{"keys failing", false, true, "config.yaml keys", "org settings"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			logs := recordLogs(t)
			shortenStartupSync(t, time.Millisecond, time.Millisecond, 0, time.Hour)
			cp := newFakeControlPlane(t, func() bool { return false })
			cp.orgsBroken.Store(tc.breakOrgs)
			cp.keysBroken.Store(tc.breakKeys)

			ctx, cancel := context.WithCancel(context.Background())
			done := make(chan bool, 1)
			go func() { done <- SyncOnStartup(ctx, cp.URL, "token", NewStore(), auth.NewKeyStore(config.AuthConfig{})) }()
			deadline := time.Now().Add(5 * time.Second)
			for logs.count(slog.LevelWarn, "") == 0 && time.Now().Before(deadline) {
				time.Sleep(time.Millisecond)
			}
			cancel()
			<-done

			warns := logs.messages(slog.LevelWarn)
			if len(warns) != 1 {
				t.Fatalf("logged WARN records %q, want 1", warns)
			}
			if !strings.Contains(warns[0], tc.wantPhrase) || strings.Contains(warns[0], tc.notPhrase) {
				t.Errorf("WARN = %q, want it to name %q and not %q", warns[0], tc.wantPhrase, tc.notPhrase)
			}
		})
	}
}

// Replicas started together must not retry the control plane in lockstep.
func TestStartupSyncBackoffIsJittered(t *testing.T) {
	const wait = 10 * time.Second
	seen := map[time.Duration]bool{}
	for range 100 {
		d := jittered(wait)
		if d < wait*9/10 || d > wait*11/10 {
			t.Fatalf("jittered(%s) = %s, want within 10%%", wait, d)
		}
		seen[d] = true
	}
	if len(seen) < 2 {
		t.Errorf("jittered(%s) returned the same value 100 times", wait)
	}
	if d := jittered(0); d != 0 {
		t.Errorf("jittered(0) = %s, want 0", d)
	}
}

// The periodic re-sync runs from the start, next to the startup sync, so a
// half that has loaded is kept fresh even while the other keeps failing.
func TestRunControlPlaneSync_PeriodicSyncKeepsTheLoadedHalfFresh(t *testing.T) {
	logs := recordLogs(t)
	shortenStartupSync(t, time.Hour, time.Hour, time.Hour, time.Hour)
	cp := newFakeControlPlane(t, func() bool { return false })
	cp.keysBroken.Store(true)
	store, ks := NewStore(), auth.NewKeyStore(config.AuthConfig{})

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		RunControlPlaneSync(ctx, true, 5*time.Millisecond, cp.URL, "token", store, ks)
		close(done)
	}()

	deadline := time.Now().Add(5 * time.Second)
	for cp.orgRequests.Load() < 4 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	warns := logs.messages(slog.LevelWarn)
	cancel()
	<-done

	if n := cp.orgRequests.Load(); n < 4 {
		t.Fatalf("control plane got %d org sync requests, want the periodic sync re-syncing orgs while keys fail", n)
	}
	if store.Get("org-1") == nil {
		t.Error("org-1 not loaded from the control plane")
	}
	// Keys have never loaded, so their periodic failures are INFO; the
	// startup sync is the one that warns about a long outage.
	if len(warns) != 0 {
		t.Errorf("logged WARN records %q while keys had never loaded", warns)
	}
}

// Until a half first loads, the periodic re-sync logs its failures at INFO (a
// starting control plane is expected to fail); after that they are WARN.
func TestRunControlPlaneSync_PeriodicSyncIsQuietUntilItLoads(t *testing.T) {
	logs := recordLogs(t)
	shortenStartupSync(t, 50*time.Millisecond, 50*time.Millisecond, time.Hour, time.Hour)
	var down atomic.Bool
	down.Store(true)
	cp := newFakeControlPlane(t, down.Load)
	store, ks := NewStore(), auth.NewKeyStore(config.AuthConfig{})

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		RunControlPlaneSync(ctx, true, 5*time.Millisecond, cp.URL, "token", store, ks)
		close(done)
	}()
	defer func() {
		cancel()
		<-done
	}()

	waitFor := func(what string, cond func() bool) {
		t.Helper()
		deadline := time.Now().Add(5 * time.Second)
		for !cond() {
			if time.Now().After(deadline) {
				t.Fatalf("timed out waiting for %s", what)
			}
			time.Sleep(time.Millisecond)
		}
	}

	waitFor("periodic failures while the control plane starts", func() bool {
		return logs.count(slog.LevelInfo, "periodic sync failed") >= 2 &&
			logs.count(slog.LevelInfo, "periodic key sync failed") >= 2
	})
	if warns := logs.messages(slog.LevelWarn); len(warns) != 0 {
		t.Fatalf("logged WARN records %q while the control plane was starting", warns)
	}

	down.Store(false)
	waitFor("the startup and periodic syncs to load", func() bool {
		return logs.count(slog.LevelInfo, "control plane startup sync succeeded") == 1 &&
			logs.count(slog.LevelDebug, "periodic sync completed") >= 1
	})
	assertSynced(t, store, ks)

	cp.orgsBroken.Store(true)
	waitFor("a WARN for a periodic failure after loading", func() bool {
		return logs.count(slog.LevelWarn, "periodic sync failed") >= 1
	})
}
