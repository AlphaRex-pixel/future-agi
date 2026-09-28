package logging

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strconv"
	"sync"
	"testing"
	"time"
)

// fakeLogWebhook stands in for the backend's request-log webhook. It records
// the request IDs of every batch it is sent and answers with answer(n, r),
// where n counts requests from 1.
type fakeLogWebhook struct {
	*httptest.Server

	mu      sync.Mutex
	batches [][]string
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

		w.WriteHeader(answer(n, r))
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
