"""An affordable Sessions latency chart is computed inline, not queued.

Every Sessions latency chart is an exact snapshot, and in the US deployment
the exact worker has one slot for every tenant: a small tenant's 0.3-1.6 s
chart waited behind the largest tenant's 30-day read. Before the job is
scheduled, ``fetch_session_graph_ch`` now sends one ``EXPLAIN ESTIMATE`` of
the lean statement's root read (project, window, ``is_deleted = 0``,
``parent_span_id = ''``, a session) with a small server cap. At or below
``SESSION_GRAPH_INLINE_MAX_ESTIMATED_ROWS`` (0 disables) the SAME statement
runs inline on the interactive wall with the interactive settings (one
thread) and the chart is returned complete, not cached. Otherwise - a larger
or unknown estimate, a shape that is not the lean one, a lane that cannot
carry per-query settings, or an inline read stopped at the wall - the
background path is unchanged.

The live equality of the inline and background numbers against an oracle is
in ``test_latency_mean_parity_ch25`` (``session/inline/none``).
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from clickhouse_driver.errors import ErrorCodes, ServerException
from django.conf import settings
from django.test import override_settings

PROJECT_ID = "7c3a2b11-0000-4000-8000-00000000e001"
ORGANIZATION_ID = "7c3a2b11-0000-4000-8000-00000000e0a1"
LO = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
HI = LO + timedelta(hours=6)
NAMESPACE = "observe-session-system-graph"
ESTIMATE_COLUMNS = ["database", "table", "parts", "rows", "marks"]

WINDOW = {
    "column_id": "created_at",
    "filter_config": {
        "col_type": "SYSTEM_METRIC",
        "filter_type": "datetime",
        "filter_op": "between",
        "filter_value": [LO.isoformat(), HI.isoformat()],
    },
}


def _system(column, filter_type, op, value):
    return {
        "column_id": column,
        "filter_config": {
            "col_type": "SYSTEM_METRIC",
            "filter_type": filter_type,
            "filter_op": op,
            "filter_value": value,
        },
    }


LEAN_SHAPES = {
    "window": [WINDOW],
    "duration": [WINDOW, _system("duration", "number", "greater_than", 5)],
    "session_id": [
        WINDOW,
        _system(
            "session_id", "text", "not_in", ["11111111-1111-4111-8111-111111111111"]
        ),
    ],
}
NOT_LEAN_SHAPES = {
    "model": [WINDOW, _system("model", "text", "equals", "gpt-x")],
    "attribute": [
        WINDOW,
        {
            "column_id": "customer_tier",
            "filter_config": {
                "col_type": "SPAN_ATTRIBUTE",
                "filter_type": "text",
                "filter_op": "equals",
                "filter_value": "gold",
            },
        },
    ],
    "first_message": [WINDOW, _system("first_message", "text", "contains", "hi")],
}

GRAPH_ROWS = [
    {"time_bucket": LO + timedelta(hours=1), "value": 812.25, "primary_traffic": 3},
    {"time_bucket": LO + timedelta(hours=4), "value": 40.5, "primary_traffic": 1},
]


def _estimate(rows):
    def respond(call):
        del call
        return SimpleNamespace(
            data=[
                {
                    "database": "db",
                    "table": "spans",
                    "parts": 1,
                    "rows": rows,
                    "marks": 1,
                }
            ],
            columns=list(ESTIMATE_COLUMNS),
        )

    return respond


def _graph(call):
    del call
    return SimpleNamespace(
        data=[dict(row) for row in GRAPH_ROWS],
        columns=["time_bucket", "value", "primary_traffic"],
    )


class _Analytics:
    """Records each statement with the timeout, settings and server cap."""

    def __init__(self, *, estimate=None, graph=_graph, per_query_settings=True):
        self.calls = []
        self._estimate = estimate or _estimate(1_000)
        self._graph = graph
        self.supports_per_query_read_settings = per_query_settings

    def execute_ch_query(
        self,
        query,
        params=None,
        timeout_ms=None,
        settings=None,
        *,
        server_execution_cap_ms=None,
    ):
        call = SimpleNamespace(
            query=query,
            params=dict(params or {}),
            timeout_ms=timeout_ms,
            settings=dict(settings or {}),
            cap=server_execution_cap_ms,
        )
        self.calls.append(call)
        if "EXPLAIN ESTIMATE" in query:
            return self._estimate(call)
        return self._graph(call)

    def estimates(self):
        return [call for call in self.calls if "EXPLAIN ESTIMATE" in call.query]

    def graphs(self):
        return [call for call in self.calls if "EXPLAIN ESTIMATE" not in call.query]


@pytest.fixture()
def scheduled(monkeypatch):
    from tracer.services.clickhouse import session_graph

    calls = []

    def read_or_schedule(namespace, identity, **kwargs):
        calls.append(SimpleNamespace(namespace=namespace, identity=identity, **kwargs))
        return {**kwargs["pending_payload"], "query_refreshing": True}

    monkeypatch.setattr(
        session_graph, "read_or_schedule_exact_snapshot", read_or_schedule
    )
    return calls


def _fetch(analytics, filters, metric_id="latency", **kwargs):
    from tracer.services.clickhouse.session_graph import fetch_session_graph_ch

    return fetch_session_graph_ch(
        analytics=analytics,
        project_id=PROJECT_ID,
        filters=filters,
        interval="hour",
        req_data_config={"type": "SYSTEM_METRIC", "id": metric_id},
        organization_id=ORGANIZATION_ID,
        **kwargs,
    )


def _points(payload):
    points = {}
    for point in payload["data"]:
        stamp = datetime.fromisoformat(str(point["timestamp"]))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        points[stamp.astimezone(UTC)] = point
    return points


@pytest.mark.unit
@pytest.mark.parametrize("shape", LEAN_SHAPES)
def test_an_affordable_latency_chart_is_computed_inline_not_scheduled(scheduled, shape):
    analytics = _Analytics()

    payload = _fetch(analytics, LEAN_SHAPES[shape])

    assert scheduled == []
    assert payload["query_status"] == "complete"
    assert payload["query_complete"] is True
    assert payload["metric_statistic"] == "mean"
    assert "query_refreshing" not in payload and "query_cached" not in payload
    points = _points(payload)
    assert points[LO + timedelta(hours=1)]["value"] == 812.25
    assert points[LO + timedelta(hours=1)]["primary_traffic"] == 3
    assert points[LO]["value"] == 0.0
    # One estimate, then the one statement.
    (estimate,) = analytics.estimates()
    (graph,) = analytics.graphs()
    assert analytics.calls == [estimate, graph]
    assert "AS exact_sessions" in graph.query


@pytest.mark.unit
def test_the_estimate_costs_the_lean_root_read_with_a_small_server_cap(scheduled):
    analytics = _Analytics()

    _fetch(analytics, [WINDOW])

    (estimate,) = analytics.estimates()
    sql = " ".join(estimate.query.split())
    assert sql.startswith("EXPLAIN ESTIMATE SELECT trace_session_id FROM spans")
    for predicate in (
        "project_id = toUUID(%(project_id)s)",
        "is_deleted = 0",
        "parent_span_id = ''",
        "start_time >= fromUnixTimestamp64Micro(%(start_date_us)s)",
        "start_time < fromUnixTimestamp64Micro(%(end_date_us)s)",
        "isNotNull(trace_session_id)",
    ):
        assert predicate in sql, predicate
    assert estimate.params == {
        "project_id": PROJECT_ID,
        "start_date_us": int(LO.timestamp() * 1_000_000),
        "end_date_us": int(HI.timestamp() * 1_000_000),
    }
    assert 0 < estimate.cap <= 1_500


@pytest.mark.unit
def test_the_inline_statement_runs_on_the_interactive_wall_and_one_thread(scheduled):
    analytics = _Analytics()

    _fetch(analytics, [WINDOW], wall_deadline_ms=12_000)

    (graph,) = analytics.graphs()
    assert graph.settings["max_threads"] == settings.FILTER_SELECTOR_MAX_THREADS == 1
    # The server stops it at what is left of the interactive wall.
    assert 0 < graph.cap <= 12_000
    assert graph.timeout_ms <= 12_000


@pytest.mark.unit
def test_inline_and_background_run_the_same_statement_with_the_same_numbers(
    scheduled, monkeypatch
):
    from tracer.tasks import exact_aggregation

    inline_analytics = _Analytics()
    inline = _fetch(inline_analytics, [WINDOW])

    background_analytics = _Analytics()
    monkeypatch.setattr(
        exact_aggregation,
        "_exact_observe_analytics",
        lambda: nullcontext(background_analytics),
    )
    monkeypatch.setattr(
        exact_aggregation, "_reauthorize_exact_observe_project", lambda _identity: None
    )
    background = exact_aggregation._load_exact_payload(
        NAMESPACE,
        {
            "project_id": PROJECT_ID,
            "organization_id": ORGANIZATION_ID,
            "filters": [WINDOW],
            "interval": "hour",
            "metric_id": "latency",
        },
    )

    (inline_graph,) = inline_analytics.graphs()
    (background_graph,) = background_analytics.calls
    assert inline_graph.query == background_graph.query
    assert inline_graph.params == background_graph.params
    # Only the thread budget differs: interactive one thread, worker four.
    assert inline_graph.settings["max_threads"] == settings.FILTER_SELECTOR_MAX_THREADS
    assert (
        background_graph.settings["max_threads"]
        == settings.EXACT_GRAPH_SESSION_READ_MAX_THREADS
    )
    assert {k: v for k, v in inline_graph.settings.items() if k != "max_threads"} == {
        k: v for k, v in background_graph.settings.items() if k != "max_threads"
    }
    assert inline["data"] == background["data"]
    assert inline["metric_statistic"] == background["metric_statistic"] == "mean"


@pytest.mark.unit
@pytest.mark.parametrize(
    "rows,runs_inline",
    [(2_000_000, True), (2_000_001, False), (77_000_000, False)],
    ids=["at-threshold", "above", "largest-30d"],
)
def test_the_threshold_decides_inline_or_background(scheduled, rows, runs_inline):
    analytics = _Analytics(estimate=_estimate(rows))

    payload = _fetch(analytics, [WINDOW])

    assert len(analytics.estimates()) == 1
    if runs_inline:
        assert scheduled == [] and len(analytics.graphs()) == 1
        assert payload["query_status"] == "complete"
    else:
        assert analytics.graphs() == []
        assert [call.namespace for call in scheduled] == [NAMESPACE]
        assert payload["query_status"] == "pending"


def _raises(exc):
    def respond(call):
        del call
        raise exc

    return respond


@pytest.mark.unit
@pytest.mark.parametrize(
    "estimate",
    [
        _raises(ServerException("stopped", code=ErrorCodes.TIMEOUT_EXCEEDED)),
        _raises(ServerException("unknown", code=ErrorCodes.UNKNOWN_IDENTIFIER)),
        _raises(TimeoutError("socket")),
        _raises(EOFError()),
        lambda _call: SimpleNamespace(data=[], columns=[]),
        lambda _call: SimpleNamespace(
            data=[{"rows": "many"}], columns=list(ESTIMATE_COLUMNS)
        ),
        lambda _call: SimpleNamespace(data=[("db", 5)], columns=list(ESTIMATE_COLUMNS)),
    ],
    ids=[
        "stopped",
        "server-error",
        "timeout",
        "eof",
        "no-columns",
        "bad-rows",
        "not-a-dict",
    ],
)
def test_an_unknown_estimate_never_guesses_inline(scheduled, estimate):
    analytics = _Analytics(estimate=estimate)

    payload = _fetch(analytics, [WINDOW])

    assert analytics.graphs() == []
    assert [call.namespace for call in scheduled] == [NAMESPACE]
    assert payload["query_status"] == "pending"


@pytest.mark.unit
def test_an_empty_estimate_answer_is_zero_rows(scheduled):
    analytics = _Analytics(
        estimate=lambda _call: SimpleNamespace(data=[], columns=list(ESTIMATE_COLUMNS))
    )

    payload = _fetch(analytics, [WINDOW])

    assert scheduled == [] and payload["query_status"] == "complete"


@pytest.mark.unit
def test_zero_turns_inline_off(scheduled):
    analytics = _Analytics()

    with override_settings(SESSION_GRAPH_INLINE_MAX_ESTIMATED_ROWS=0):
        payload = _fetch(analytics, [WINDOW])

    assert analytics.calls == []
    assert [call.namespace for call in scheduled] == [NAMESPACE]
    assert payload["query_status"] == "pending"


@pytest.mark.unit
def test_a_lane_without_per_query_settings_is_never_inline(scheduled):
    analytics = _Analytics(per_query_settings=False)

    _fetch(analytics, [WINDOW])

    assert analytics.calls == []
    assert [call.namespace for call in scheduled] == [NAMESPACE]


@pytest.mark.unit
@pytest.mark.parametrize("shape", NOT_LEAN_SHAPES)
def test_span_level_and_message_filters_keep_the_background_path(scheduled, shape):
    analytics = _Analytics()

    _fetch(analytics, NOT_LEAN_SHAPES[shape])

    assert analytics.calls == []
    assert [call.namespace for call in scheduled] == [NAMESPACE]


@pytest.mark.unit
def test_other_session_metrics_are_unchanged(scheduled):
    analytics = _Analytics()

    _fetch(analytics, LEAN_SHAPES["duration"], metric_id="error_rate")

    assert analytics.calls == []
    assert [call.namespace for call in scheduled] == [NAMESPACE]


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        ServerException("stopped", code=ErrorCodes.TIMEOUT_EXCEEDED),
        ServerException("cancelled", code=ErrorCodes.QUERY_WAS_CANCELLED),
    ],
    ids=["timeout", "cancelled"],
)
def test_an_inline_read_stopped_at_the_wall_goes_to_the_worker(scheduled, error):
    analytics = _Analytics(graph=_raises(error))

    payload = _fetch(analytics, [WINDOW])

    assert len(analytics.graphs()) == 1
    assert [call.namespace for call in scheduled] == [NAMESPACE]
    assert scheduled[0].refresh is False
    assert payload["query_status"] == "pending"


@pytest.mark.unit
def test_an_inline_programming_error_is_not_hidden(scheduled):
    analytics = _Analytics(
        graph=_raises(ServerException("syntax", code=ErrorCodes.SYNTAX_ERROR))
    )

    with pytest.raises(ServerException):
        _fetch(analytics, [WINDOW])

    assert scheduled == []


@pytest.mark.unit
def test_an_explicit_refresh_of_an_affordable_scope_is_inline(scheduled):
    analytics = _Analytics()

    payload = _fetch(analytics, [WINDOW], refresh=True)

    assert scheduled == [] and payload["query_status"] == "complete"


@pytest.mark.unit
def test_an_empty_window_is_inline_without_a_statement(scheduled):
    analytics = _Analytics()
    empty = _system("created_at", "datetime", "between", [LO.isoformat()] * 2)

    payload = _fetch(analytics, [empty])

    assert analytics.calls == [] and scheduled == []
    assert payload["query_status"] == "complete" and payload["data"] == []


@pytest.mark.unit
def test_the_threshold_is_a_registered_runtime_setting_documented_for_operators():
    from tfc.settings.runtime_setting_specs import RUNTIME_NUMERIC_SETTING_SPECS

    name = "SESSION_GRAPH_INLINE_MAX_ESTIMATED_ROWS"
    spec = RUNTIME_NUMERIC_SETTING_SPECS[name]
    assert (
        spec.parse(name)
        == 2_000_000
        == settings.SESSION_GRAPH_INLINE_MAX_ESTIMATED_ROWS
    )
    assert spec.parse(name, "") == 2_000_000
    assert spec.parse(name, "0") == 0  # off
    with pytest.raises(ValueError, match=name):
        spec.parse(name, "-1")
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    assert f"\n{name}=2000000\n" in env_example.read_text()
