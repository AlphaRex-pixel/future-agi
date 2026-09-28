package logging

import (
	"context"
	"encoding/json"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"runtime"
	"slices"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/futureagi/agentcc-gateway/internal/models"
)

// dropConnection makes fakeLogWebhook close the connection without answering,
// as a backend that is restarting does.
const dropConnection = -1

// fakeLogWebhook stands in for the backend's request-log webhook. It records
// the request IDs of every batch it is sent and answers with answer(n, r),
// where n counts requests from 1.
type fakeLogWebhook struct {
	*httptest.Server

	mu       sync.Mutex
	batches  [][]string
	statuses []int
}

func newFakeLogWebhook(t *testing.T, answer func(n int, r *http.Request) int) *fakeLogWebhook {
	t.Helper()
	wh := &fakeLogWebhook{}
	wh.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload logFlushPayload
		_ = json.NewDecoder(r.Body).Decode(&payload)
		ids := make([]string, len(payload.Logs))
		for i, entry := range payload.Logs {
			ids[i] = entry.RequestID
		}
		wh.mu.Lock()
		wh.batches = append(wh.batches, ids)
		n := len(wh.batches)
		wh.mu.Unlock()

		status := answer(n, r)
		wh.mu.Lock()
		wh.statuses = append(wh.statuses, status)
		wh.mu.Unlock()
		if status == dropConnection {
			if conn, _, err := w.(http.Hijacker).Hijack(); err == nil {
				conn.Close()
			}
			return
		}
		w.WriteHeader(status)
	}))
	t.Cleanup(wh.Close)
	return wh
}

// releaseAtCleanup returns a func that closes ch once, and also calls it when
// the test ends. Called after newFakeLogWebhook, it runs before the webhook
// closes, so an answer blocked on ch lets the webhook close.
func releaseAtCleanup(t *testing.T, ch chan struct{}) func() {
	t.Helper()
	var once sync.Once
	release := func() { once.Do(func() { close(ch) }) }
	t.Cleanup(release)
	return release
}

// requests reports how many requests the webhook has received.
func (wh *fakeLogWebhook) requests() int {
	wh.mu.Lock()
	defer wh.mu.Unlock()
	return len(wh.batches)
}

// delivered returns the request IDs of the batches it answered with a 2xx.
func (wh *fakeLogWebhook) delivered() []string {
	wh.mu.Lock()
	defer wh.mu.Unlock()
	var ids []string
	for i, status := range wh.statuses {
		if status >= 200 && status < 300 {
			ids = append(ids, wh.batches[i]...)
		}
	}
	return ids
}

func enqueue(f *LogFlusher, requestIDs ...string) {
	for _, id := range requestIDs {
		f.Enqueue(TraceRecord{RequestID: id, Timestamp: time.Now(), Model: "gpt-4", Provider: "openai"})
	}
}

// enqueueN enqueues n records, numbered from 1.
func enqueueN(f *LogFlusher, n int) {
	for i := 1; i <= n; i++ {
		enqueue(f, "req-"+strconv.Itoa(i))
	}
}

// undeliveredLogged returns the "undelivered" count of the record that says
// request logs were not delivered, its level, and whether there is one.
func undeliveredLogged(t *testing.T, h *capturingHandler) (int64, slog.Level, bool) {
	t.Helper()
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, rec := range h.records {
		if v, ok := findAttr(rec, "undelivered"); ok {
			return v.Int64(), rec.Level, true
		}
	}
	return 0, 0, false
}

// shortRetryWait makes Close's retries 10ms apart for the test.
func shortRetryWait(t *testing.T) {
	t.Helper()
	prev := finalFlushRetryWait
	finalFlushRetryWait = 10 * time.Millisecond
	t.Cleanup(func() { finalFlushRetryWait = prev })
}

// waitUntilClosing waits until Close has begun on f.
func waitUntilClosing(t *testing.T, f *LogFlusher) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !f.closing() {
		if time.Now().After(deadline) {
			t.Fatal("Close did not begin")
		}
		time.Sleep(time.Millisecond)
	}
}

// While a send is slow, the records that arrive wait for one pending flush
// instead of each starting one, and the buffer stops at twice maxBuffer: the
// records past that are refused, with a single warning.
func TestLogFlusher_StaysBoundedWhileASendIsSlow(t *testing.T) {
	logs, restore := installCapturingLogger()
	defer restore()
	sending := make(chan struct{}, 1)
	released := make(chan struct{})
	wh := newFakeLogWebhook(t, func(_ int, r *http.Request) int {
		select {
		case sending <- struct{}{}:
		default:
		}
		select {
		case <-r.Context().Done():
		case <-released:
		}
		return http.StatusOK
	})
	release := releaseAtCleanup(t, released)
	const maxBuffer = 100
	f := NewLogFlusher(wh.URL, "secret", time.Hour, maxBuffer)
	ctx, cancel := context.WithCancel(context.Background())
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		f.Run(ctx)
	}()
	defer func() {
		cancel()
		release()
		<-stopped
	}()
	enqueueN(f, maxBuffer)
	<-sending

	goroutines := runtime.NumGoroutine()
	enqueueN(f, 10*maxBuffer)

	if n := runtime.NumGoroutine() - goroutines; n > 5 {
		t.Errorf("%d goroutines started while %d records were enqueued during a slow send, want none", n, 10*maxBuffer)
	}
	f.mu.Lock()
	buffered := len(f.buffer)
	f.mu.Unlock()
	if buffered != 2*maxBuffer {
		t.Errorf("buffered %d records, want %d", buffered, 2*maxBuffer)
	}
	var warnings int
	logs.mu.Lock()
	for _, rec := range logs.records {
		if rec.Level == slog.LevelWarn && rec.Message == "log flusher: buffer full, dropping new records" {
			warnings++
		}
	}
	logs.mu.Unlock()
	if warnings != 1 {
		t.Errorf("logged %d buffer-full warnings, want 1", warnings)
	}
}

// A backend that is restarting when the gateway stops refuses the first send;
// the records buffered then are delivered on the retry, once each.
func TestPluginClose_DeliversBufferedLogsWhenTheWebhookComesBack(t *testing.T) {
	for _, tc := range []struct {
		name  string
		first int
	}{
		{"after a server error", http.StatusServiceUnavailable},
		{"after a dropped connection", dropConnection},
	} {
		t.Run(tc.name, func(t *testing.T) {
			shortRetryWait(t)
			logs, restore := installCapturingLogger()
			defer restore()
			wh := newFakeLogWebhook(t, func(n int, _ *http.Request) int {
				if n == 1 {
					return tc.first
				}
				return http.StatusOK
			})
			f := NewLogFlusher(wh.URL, "secret", time.Hour, 100)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			go f.Run(ctx)
			p := New(enabledCfg(), nil)
			p.SetFlusher(f)
			enqueue(f, "req-1", "req-2", "req-3")

			p.Close()

			if got, want := wh.delivered(), []string{"req-1", "req-2", "req-3"}; !slices.Equal(got, want) {
				t.Errorf("webhook was delivered %q on shutdown, want %q", got, want)
			}
			if n := wh.requests(); n != 2 {
				t.Errorf("webhook got %d requests, want 2: the failed send and its retry", n)
			}
			if n, _, ok := undeliveredLogged(t, logs); ok {
				t.Errorf("logged %d undelivered records, want none", n)
			}
		})
	}
}

// A backend that stays down: the shutdown still ends, after a few tries, and
// says how many request logs were lost.
func TestPluginClose_LogsHowManyLogsItCouldNotDeliver(t *testing.T) {
	shortRetryWait(t)
	logs, restore := installCapturingLogger()
	defer restore()
	wh := newFakeLogWebhook(t, func(int, *http.Request) int { return http.StatusServiceUnavailable })
	f := NewLogFlusher(wh.URL, "secret", time.Hour, 100)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go f.Run(ctx)
	p := New(enabledCfg(), nil)
	p.SetFlusher(f)
	enqueue(f, "req-1", "req-2", "req-3", "req-4")

	start := time.Now()
	p.Close()
	if elapsed := time.Since(start); elapsed > shutdownFlushTimeout+time.Second {
		t.Errorf("Close took %s against a webhook that never answers 2xx, want it bounded to about %s", elapsed, shutdownFlushTimeout)
	}

	if n := wh.requests(); n < 2 {
		t.Errorf("webhook got %d requests on shutdown, want the send retried", n)
	}
	if got := wh.delivered(); len(got) != 0 {
		t.Errorf("webhook was delivered %q, want nothing", got)
	}
	if n, level, ok := undeliveredLogged(t, logs); !ok || n != 4 || level != slog.LevelError {
		t.Errorf("logged undelivered = %d at %s (logged: %v), want 4 at ERROR", n, level, ok)
	}
}

// A client error will not change on a retry, so the last flush does not retry it.
func TestLogFlusherClose_DoesNotRetryAClientError(t *testing.T) {
	logs, restore := installCapturingLogger()
	defer restore()
	wh := newFakeLogWebhook(t, func(int, *http.Request) int { return http.StatusBadRequest })
	f := NewLogFlusher(wh.URL, "secret", time.Hour, 100)
	enqueue(f, "req-1", "req-2")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	f.Close(ctx)

	if n := wh.requests(); n != 1 {
		t.Errorf("webhook got %d requests, want 1", n)
	}
	if n, _, ok := undeliveredLogged(t, logs); !ok || n != 2 {
		t.Errorf("logged undelivered = %d (logged: %v), want 2", n, ok)
	}
}

// A webhook that never answers cannot hold the shutdown past Close's deadline,
// whether it hangs on the last flush or on a flush that was already sending.
// A send cut off at the deadline is not sent again.
func TestLogFlusherClose_IsBoundedWhenTheWebhookHangs(t *testing.T) {
	for _, tc := range []struct {
		name          string
		flushSendsNow bool
	}{
		{"on the last flush", false},
		{"on a flush already sending", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			logs, restore := installCapturingLogger()
			defer restore()
			sending := make(chan struct{}, 10)
			released := make(chan struct{})
			wh := newFakeLogWebhook(t, func(_ int, r *http.Request) int {
				sending <- struct{}{}
				select {
				case <-r.Context().Done():
				case <-released:
				}
				return http.StatusOK
			})
			releaseAtCleanup(t, released)
			f := NewLogFlusher(wh.URL, "secret", time.Hour, 100)
			enqueue(f, "req-1", "req-2")
			if tc.flushSendsNow {
				go f.flush()
				<-sending
			}

			ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
			defer cancel()
			start := time.Now()
			f.Close(ctx)
			if elapsed := time.Since(start); elapsed > 2*time.Second {
				t.Fatalf("Close took %s with a 200ms deadline", elapsed)
			}

			if n := wh.requests(); n != 1 {
				t.Errorf("webhook got %d requests, want 1", n)
			}
			if n, _, ok := undeliveredLogged(t, logs); !ok || n != 2 {
				t.Errorf("logged undelivered = %d (logged: %v), want 2", n, ok)
			}
		})
	}
}

// Close lets a flush that is sending finish rather than sending its batch
// again, and refuses records that arrive once it has begun, counting them.
func TestLogFlusherClose_WaitsForAFlushInProgress(t *testing.T) {
	logs, restore := installCapturingLogger()
	defer restore()
	sending := make(chan struct{}, 10)
	proceed := make(chan struct{})
	wh := newFakeLogWebhook(t, func(n int, _ *http.Request) int {
		if n == 1 {
			sending <- struct{}{}
			<-proceed
		}
		return http.StatusOK
	})
	letItAnswer := releaseAtCleanup(t, proceed)
	f := NewLogFlusher(wh.URL, "secret", time.Hour, 100)
	enqueue(f, "req-1", "req-2")
	go f.flush()
	<-sending

	closed := make(chan struct{})
	go func() {
		defer close(closed)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		f.Close(ctx)
	}()
	waitUntilClosing(t, f)
	enqueue(f, "req-3")
	letItAnswer()

	select {
	case <-closed:
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not return")
	}
	if got, want := wh.delivered(), []string{"req-1", "req-2"}; !slices.Equal(got, want) {
		t.Errorf("webhook was delivered %q, want %q", got, want)
	}
	if n := wh.requests(); n != 1 {
		t.Errorf("webhook got %d requests, want 1: the batch in flight, not sent again", n)
	}
	if n, _, ok := undeliveredLogged(t, logs); !ok || n != 1 {
		t.Errorf("logged undelivered = %d (logged: %v), want 1 for the record refused after Close began", n, ok)
	}
}

// A flush that gives up on its batch while Close waits for it leaves the batch
// to Close: Close retries it after the flush's last server error, and counts
// it after a client error, which a retry would not change.
func TestLogFlusherClose_TakesOverTheBatchOfAFlushThatGivesUp(t *testing.T) {
	for _, tc := range []struct {
		name            string
		status          int
		wantDelivered   []string
		wantRequests    int
		wantUndelivered int64
	}{
		{"after its last server error", http.StatusServiceUnavailable, []string{"req-1", "req-2"}, 2, 0},
		{"after a client error", http.StatusBadRequest, nil, 1, 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			logs, restore := installCapturingLogger()
			defer restore()
			sending := make(chan struct{}, 10)
			proceed := make(chan struct{})
			wh := newFakeLogWebhook(t, func(n int, _ *http.Request) int {
				if n == 1 {
					sending <- struct{}{}
					<-proceed
					return tc.status
				}
				return http.StatusOK
			})
			letItAnswer := releaseAtCleanup(t, proceed)
			f := NewLogFlusher(wh.URL, "secret", time.Hour, 100)
			f.consecutiveFails = maxFlushRetries // this send is the flush's last try
			enqueue(f, "req-1", "req-2")
			go f.flush()
			<-sending

			closed := make(chan struct{})
			go func() {
				defer close(closed)
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				f.Close(ctx)
			}()
			waitUntilClosing(t, f)
			letItAnswer()

			select {
			case <-closed:
			case <-time.After(5 * time.Second):
				t.Fatal("Close did not return")
			}
			if got := wh.delivered(); !slices.Equal(got, tc.wantDelivered) {
				t.Errorf("webhook was delivered %q, want %q", got, tc.wantDelivered)
			}
			if n := wh.requests(); n != tc.wantRequests {
				t.Errorf("webhook got %d requests, want %d", n, tc.wantRequests)
			}
			if n, _, _ := undeliveredLogged(t, logs); n != tc.wantUndelivered {
				t.Errorf("logged undelivered = %d, want %d", n, tc.wantUndelivered)
			}
		})
	}
}

// A backend that has already stopped (stop order often stops it before the
// gateway) makes the undelivered logs a WARN, not an ERROR.
func TestLogFlusherClose_WarnsWhenTheBackendIsGone(t *testing.T) {
	gone := httptest.NewServer(http.NotFoundHandler())
	gone.Close()
	for _, tc := range []struct {
		name string
		dial func(ctx context.Context, network, addr string) (net.Conn, error)
	}{
		{"it refuses the connection", nil},
		{"its name no longer resolves", func(context.Context, string, string) (net.Conn, error) {
			return nil, &net.DNSError{Err: "no such host", Name: "backend", IsNotFound: true}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			shortRetryWait(t)
			logs, restore := installCapturingLogger()
			defer restore()
			f := NewLogFlusher(gone.URL, "secret", time.Hour, 100)
			if tc.dial != nil {
				f.client = &http.Client{Transport: &http.Transport{DialContext: tc.dial}}
			}
			enqueue(f, "req-1", "req-2")

			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			f.Close(ctx)

			if n, level, ok := undeliveredLogged(t, logs); !ok || n != 2 || level != slog.LevelWarn {
				t.Errorf("logged undelivered = %d at %s (logged: %v), want 2 at WARN", n, level, ok)
			}
		})
	}
}

// blockingTraceHandler holds each request.trace log line until release closes.
type blockingTraceHandler struct{ release <-chan struct{} }

func (h blockingTraceHandler) Enabled(context.Context, slog.Level) bool { return true }
func (h blockingTraceHandler) Handle(_ context.Context, r slog.Record) error {
	if r.Message == "request.trace" {
		<-h.release
	}
	return nil
}
func (h blockingTraceHandler) WithAttrs([]slog.Attr) slog.Handler { return h }
func (h blockingTraceHandler) WithGroup(string) slog.Handler      { return h }

// The plugin's Close makes the last flush while the trace emitter drains, so
// their waits do not add up in the shutdown's time budget.
func TestPluginClose_FlushesWhileTheEmitterDrains(t *testing.T) {
	flushed := make(chan struct{})
	prev := slog.Default()
	slog.SetDefault(slog.New(blockingTraceHandler{release: flushed}))
	defer slog.SetDefault(prev)
	wh := newFakeLogWebhook(t, func(n int, _ *http.Request) int {
		if n == 1 {
			close(flushed) // the emitter drains only once the flush is sent
		}
		return http.StatusOK
	})
	p := New(enabledCfg(), nil)
	p.SetFlusher(NewLogFlusher(wh.URL, "secret", time.Hour, 100))
	rc := newRC()
	rc.Response = &models.ChatCompletionResponse{}
	p.ProcessResponse(context.Background(), rc)

	start := time.Now()
	p.Close()
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Errorf("Close took %s, want the flush and the emitter's drain to overlap", elapsed)
	}
	if got, want := wh.delivered(), []string{"req-123"}; !slices.Equal(got, want) {
		t.Errorf("webhook was delivered %q, want %q", got, want)
	}
}
