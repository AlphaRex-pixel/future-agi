package server

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/future-agi/future-agi/fi-collector/pkg/auth"
	"github.com/future-agi/future-agi/fi-collector/pkg/chwriter"
	"github.com/future-agi/future-agi/fi-collector/pkg/observedcatalog"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"
)

// Direct mode (Standalone, Helm): OTLP HTTP, the async source flusher, the
// fsync spool and a restart, then merged replay into the index as a writer
// with only the bootstrap's grants. The same spool records inserted one by
// one, as the Kafka consumer does, must give identical index contents.
// Authentication is a synthetic trusted result, as in the Kafka test.
func TestClickHouseDirectObservedCatalogFromOTLPHTTP(t *testing.T) {
	origin := os.Getenv("OBS_TEST_CH_URL")
	if origin == "" {
		t.Skip("set OBS_TEST_CH_URL for isolated local integration")
	}
	u, err := url.Parse(origin)
	if err != nil || u.Scheme != "http" || u.Hostname() != "127.0.0.1" || u.Port() == "" || u.User != nil || u.RawQuery != "" || (u.Path != "" && u.Path != "/") {
		t.Fatal("ClickHouse integration requires an explicit loopback HTTP origin")
	}
	admin, adminPassword := observedTestCredentials()
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	suffix := fmt.Sprint(time.Now().UnixNano())
	sourceDB, directDB, consumerDB := "observed_direct_source_"+suffix, "observed_direct_catalog_"+suffix, "observed_direct_consumer_"+suffix
	for _, database := range []string{sourceDB, directDB, consumerDB} {
		observedLocalSQL(t, origin, "default", "CREATE DATABASE "+database)
		t.Cleanup(func() { observedLocalSQL(t, origin, "default", "DROP DATABASE "+database) })
	}
	sourceDDL, _, found := strings.Cut(observedDDL(t, "schema", "002_spans_v2.sql"), "\nTTL ")
	if !found {
		t.Fatal("canonical source DDL boundary changed")
	}
	observedLocalSQL(t, origin, sourceDB, sourceDDL+"\nSETTINGS deduplicate_merge_projection_mode = 'rebuild'")
	for _, database := range []string{directDB, consumerDB} {
		for _, statement := range strings.Split(observedDDL(t, "observed_catalog", "schema.sql"), ";") {
			if strings.TrimSpace(statement) != "" {
				observedLocalSQL(t, origin, database, statement)
			}
		}
	}
	// The writer the Standalone and Helm bootstraps create: SELECT and INSERT
	// on the two index tables, nothing else.
	writer := directDB + "_writer"
	observedLocalSQL(t, origin, "default", "CREATE USER "+writer+" IDENTIFIED WITH sha256_password BY '"+suffix+"'")
	t.Cleanup(func() { observedLocalSQL(t, origin, "default", "DROP USER "+writer) })
	for _, table := range []string{observedcatalog.KeyTable, observedcatalog.ValueTable} {
		observedLocalSQL(t, origin, "default", "GRANT SELECT, INSERT ON "+directDB+"."+table+" TO "+writer)
	}

	spoolCfg := observedcatalog.SpoolConfig{Directory: t.TempDir()}
	spool, err := observedcatalog.NewWriter(spoolCfg, observedcatalog.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { spool.Close() }()
	source, err := chwriter.New(chwriter.Config{URL: origin, Database: sourceDB, Table: "spans", Username: admin, Password: adminPassword, MaxRetries: 1, RequestTimeout: 10 * time.Second, DeadLetterFile: filepath.Join(t.TempDir(), "source-deadletter.jsonl")})
	if err != nil {
		t.Fatal(err)
	}
	defer source.Close()
	completion := &observedHandoff{Writer: spool, done: make(chan error, 1)}
	s := New(Config{BatchMaxRows: 1, BatchMaxAge: 10 * time.Millisecond}, source, nil, NoopUsageEmitter{}, NoopMetering{}, WithPropertyCatalogWriter(completion))
	project := "33333333-3333-4333-8333-333333333333"
	result := &auth.ResolveResult{OrgID: "11111111-1111-4111-8111-111111111111", WorkspaceID: "22222222-2222-4222-8222-222222222222", Projects: map[string]string{"local-project": project}}
	httpServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.handleHTTPTraces(w, r.WithContext(auth.WithResolvedContext(r.Context(), result, "synthetic-test-proof")))
	}))
	defer httpServer.Close()
	traces := makeTraces("observed-direct-smoke", "44444444-4444-4444-8444-444444444444")
	rs := traces.ResourceSpans().At(0)
	rs.Resource().Attributes().PutStr("project_name", "local-project")
	span := rs.ScopeSpans().At(0).Spans().At(0)
	span.SetParentSpanID([8]byte{0xcc})
	span.Attributes().PutStr("customer.label", "Hello")
	span.Attributes().PutDouble("customer.score", 1.5)
	span.Attributes().PutBool("customer.enabled", true)
	items := span.Attributes().PutEmptySlice("customer.items")
	items.AppendEmpty().SetStr("1")
	items.AppendEmpty().SetInt(1)
	items.AppendEmpty().SetBool(true)
	span.Attributes().PutEmptySlice("customer.empty")
	span.Attributes().PutStr("gen_ai.request.model", "direct-model")
	body, err := ptraceotlp.NewExportRequestFromTraces(traces).MarshalJSON()
	if err != nil {
		t.Fatal(err)
	}
	response, err := httpServer.Client().Post(httpServer.URL+"/v1/traces", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, response.Body)
	response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("OTLP HTTP status %d", response.StatusCode)
	}
	s.wg.Add(1)
	go s.flushLoop()
	defer func() { close(s.stopCh); s.wg.Wait() }()
	select {
	case err := <-completion.done:
		if err != nil {
			t.Fatal("catalog handoff", err)
		}
	case <-ctx.Done():
		t.Fatal("canonical/spool handoff timed out", source.Snapshot())
	}
	if stats := source.Snapshot(); stats.RowsInserted != 1 || stats.RowsDeadLettered != 0 {
		t.Fatal("canonical insert failed", stats)
	}

	// What the Kafka consumer writes: each spool record, decoded and inserted.
	records, err := filepath.Glob(filepath.Join(spoolCfg.Directory, "observed-*.json"))
	if err != nil || len(records) == 0 {
		t.Fatal("no spooled observations", err)
	}
	consumer, err := observedcatalog.NewClickHouseSink(observedcatalog.ClickHouseConfig{URL: origin, Database: consumerDB, Username: admin, Password: adminPassword})
	if err != nil {
		t.Fatal(err)
	}
	for _, record := range records {
		raw, err := os.ReadFile(record)
		if err != nil {
			t.Fatal(err)
		}
		batch, err := observedcatalog.Decode(raw)
		if err != nil {
			t.Fatal(err)
		}
		if err := consumer.Insert(ctx, batch); err != nil {
			t.Fatal(err)
		}
	}

	// Restart, then replay as the collector does in direct mode.
	if err := spool.Close(); err != nil {
		t.Fatal(err)
	}
	spool, err = observedcatalog.NewWriter(spoolCfg, observedcatalog.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	sink, err := observedcatalog.NewClickHouseSink(observedcatalog.ClickHouseConfig{URL: origin, Database: directDB, Username: writer, Password: suffix})
	if err != nil {
		t.Fatal(err)
	}
	if n, err := spool.ReplayMerged(ctx, sink); err != nil || n != len(records) {
		t.Fatalf("direct replay: %d of %d, %v", n, len(records), err)
	}
	if n, err := spool.ReplayMerged(ctx, sink); err != nil || n != 0 {
		t.Fatalf("acknowledged records retained: %d %v", n, err)
	}

	if got := observedLocalSQL(t, origin, directDB, "SELECT count() FROM observed_attribute_values WHERE startsWith(attribute_key, 'customer.')"); got != "6" {
		t.Fatal("custom values missing", got)
	}
	if got := observedLocalSQL(t, origin, directDB, "SELECT count() FROM observed_attribute_keys WHERE startsWith(attribute_key, 'customer.')"); got != "5" {
		t.Fatal("empty array key or typed key missing", got)
	}
	if got := observedLocalSQL(t, origin, directDB, "SELECT attribute_type, value_json FROM observed_attribute_values WHERE attribute_key = 'customer.score' OR attribute_key = 'customer.enabled' ORDER BY attribute_type"); got != "boolean\ttrue\nnumber\t1.5" {
		t.Fatal("scalar types lost", got)
	}
	if got := observedLocalSQL(t, origin, directDB, "SELECT source_kind, value_json FROM observed_attribute_values WHERE attribute_key = 'model'"); got != "system_attribute\t\"direct-model\"" {
		t.Fatal("system model suggestion missing", got)
	}
	for _, table := range []string{observedcatalog.KeyTable, observedcatalog.ValueTable} {
		if got := observedLocalSQL(t, origin, directDB, "SELECT count() FROM "+table+" WHERE organization_id != '"+result.OrgID+"' OR workspace_id != '"+result.WorkspaceID+"' OR project_id != '"+project+"'"); got != "0" {
			t.Fatal("index used untrusted payload scope", table, got)
		}
		logical := "SELECT * EXCEPT (first_seen, last_seen), min(first_seen), max(last_seen) FROM %s." + table + " GROUP BY ALL ORDER BY ALL FORMAT JSONEachRow"
		direct := observedLocalSQL(t, origin, "default", fmt.Sprintf(logical, directDB))
		viaConsumer := observedLocalSQL(t, origin, "default", fmt.Sprintf(logical, consumerDB))
		if direct == "" || direct != viaConsumer {
			t.Fatalf("%s differs from the Kafka consumer's rows:\n%s\n---\n%s", table, direct, viaConsumer)
		}
	}
}
