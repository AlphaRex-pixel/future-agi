"""Real-ClickHouse proof: the lean Sessions graph statement keeps dev's answer.

``read_exact_session_system_graph`` is the one statement behind every
Sessions chart, filtered or not, and it runs only on the exact-aggregation
background worker. Dev's statement read ``spans FINAL`` over every span of the
window four times (the candidate probe, the remap union, the selected-session
set and the aggregate itself), on one thread. The lean statement:

* reads the candidate session ids ONCE, into a scalar array;
* for an unfiltered chart (no span-level membership leaf), builds those ids
  from a narrow non-FINAL read of root VERSIONS only, a superset of dev's
  exact candidates that changes no remap lookup the aggregate makes;
* keeps ``spans FINAL`` for the aggregate itself, so "the latest live version
  of each span" is resolved exactly as before (re-parents, tombstones,
  resurrections, session moves and equal-version ties included);
* runs on ``EXACT_GRAPH_SESSION_READ_MAX_THREADS`` threads, on the
  background worker only.

**The oracle is dev's statement itself**, produced by the same public reader
with the lean switch and the session settings removed. Both run on one
unmerged store (merges stopped, every version a physical row) seeded with
re-versions, tombstones, resurrections, root/child re-parents, session moves,
start-time corrections inside the replacement hour, equal-version ties,
children, multi-trace sessions, window-edge sessions, remap straddlers and
remap chains, NULL/NIL sessions and another tenant in the same hours. Every
system metric must match per bucket (abs 1e-9, traffic exact), and every
per-session row must match, filtered and unfiltered.

The cost test pins the new shape through ``system.query_log``: one FINAL
spans scan for an unfiltered chart, two (not four) for a filtered one, a
candidate read that touches root versions only, and the background thread
count on the statement while the shared interactive pin stays at one.
"""

from __future__ import annotations

import os
import random
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import (
    _ch_test_http_port,
    _ch_test_native_client,
    _ch_test_owned_database,
    _open_ch_test_http_client,
)

pytestmark = pytest.mark.integration

PROJECT_ID = "5e551011-0000-4000-8000-00000000a001"
NOISE_PROJECT_ID = "5e551011-0000-4000-8000-00000000a002"
NIL = "00000000-0000-0000-0000-000000000000"
LO = datetime(2026, 6, 3, 7, 23, 11, 500000, tzinfo=UTC)
HI = datetime(2026, 6, 5, 16, 40, 0, 250000, tzinfo=UTC)
TOLERANCE = 1e-9
BASE_VERSION = 1_000_000_000

METRICS = (
    "latency",
    "tokens",
    "prompt_tokens",
    "completion_tokens",
    "cost",
    "total_cost",
    "traffic",
    "error_rate",
    "avg_duration",
    "avg_traces_per_session",
)

_SCHEMA_DIR = (
    Path(__file__).resolve().parents[1] / "services" / "clickhouse" / "v2" / "schema"
)

_WINDOW_FILTER = {
    "column_id": "created_at",
    "filter_config": {
        "col_type": "SYSTEM_METRIC",
        "filter_type": "datetime",
        "filter_op": "between",
        "filter_value": [LO.isoformat(), HI.isoformat()],
    },
}


def _uid(*parts) -> str:
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, "lean-session/" + "/".join(map(str, parts)))
    )


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

ROOT_TYPES = ("chain", "agent", "chain", "llm")
CHILD_TYPES = ("llm", "tool", "retriever", "embedding", "llm", "tool")
MODELS = ("gpt-x", "claude-y", "gemini-z", "")
COMPANIES = tuple(str(10_000_000 + index) for index in range(6))


class _Seed:
    def __init__(self):
        self.rng = random.Random(20260928)
        self.base = []
        self.versions = [[], [], []]
        self.remaps = []
        self.roots = []
        self.children = []
        self.facts = {}
        self._serial = 0

    def _next(self, label):
        self._serial += 1
        return _uid(label, self._serial)

    def span(
        self,
        project,
        otype,
        service,
        start,
        trace_id,
        parent,
        session,
        latency,
        **extra,
    ):
        rng = self.rng
        prompt, completion = rng.randint(0, 3000), rng.randint(0, 800)
        row = {
            "project_id": uuid.UUID(project),
            "observation_type": otype,
            "service_name": service,
            "start_time": start,
            "trace_id": trace_id,
            "id": self._next("span"),
            "parent_span_id": parent,
            "name": f"{otype}-op",
            "end_time": start + timedelta(milliseconds=latency),
            "latency_ms": latency,
            "trace_session_id": uuid.UUID(session) if session else None,
            "status": "ERROR" if rng.random() < 0.06 else "OK",
            "model": rng.choice(MODELS),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "cost": round(rng.random() * 0.05, 6),
            "attrs_string": {"company_id": rng.choice(COMPANIES)},
            "is_deleted": 0,
            "_version": BASE_VERSION + rng.randint(0, 1000),
        }
        row.update(extra)
        return row

    def trace(self, project, session, start, *, max_children=6):
        rng = self.rng
        trace_id = self._next("trace")
        service = rng.choice(("svc-a", "svc-b"))
        root_type = rng.choice(ROOT_TYPES)
        latency = rng.randint(100, 60_000)
        root = self.span(
            project, root_type, service, start, trace_id, "", session, latency
        )
        rows = [root]
        children = 0 if root_type == "llm" else rng.randint(0, max_children)
        for _ in range(children):
            offset = rng.randint(0, max(latency - 50, 1))
            rows.append(
                self.span(
                    project,
                    rng.choice(CHILD_TYPES),
                    service,
                    start + timedelta(milliseconds=offset),
                    trace_id,
                    root["id"],
                    session,
                    rng.randint(10, max(latency // 2, 11)),
                )
            )
        if project == PROJECT_ID:
            self.roots.append(root)
            self.children.extend(rows[1:])
        self.base.extend(rows)
        return root, rows

    def bump(self, row, step=1, **changes):
        return {
            **row,
            **changes,
            "_version": row["_version"] + 10_000 * step + self.rng.randint(1, 999),
        }


def _build_seed() -> _Seed:
    seed = _Seed()
    rng = seed.rng
    span_lo = LO - timedelta(hours=3)
    span_seconds = int((HI + timedelta(hours=3) - span_lo).total_seconds())

    # Multi-trace sessions around and inside the window, with NULL/NIL
    # sessions and cutover straddlers (old ids remapped onto the session).
    for number in range(420):
        start = span_lo + timedelta(
            seconds=rng.randint(0, span_seconds), microseconds=rng.randint(0, 999_999)
        )
        kind = rng.random()
        if kind < 0.04:
            session = None
        elif kind < 0.06:
            session = NIL
        else:
            session = _uid("session", number)
        ids = [session]
        if session and session != NIL and rng.random() < 0.06:
            olds = [_uid("old", number, index) for index in range(rng.randint(1, 2))]
            seed.remaps.extend((uuid.UUID(old), uuid.UUID(session), 1) for old in olds)
            ids.extend(olds)
        moment = start
        for _ in range(rng.randint(1, 4)):
            seed.trace(PROJECT_ID, rng.choice(ids), moment)
            moment += timedelta(
                minutes=rng.randint(1, 90), microseconds=rng.randint(0, 999_999)
            )

    # Sessions whose first root sits just before an exact window edge and
    # whose later roots fall inside it (and the mirror image at the end).
    for edge_name, edge in (("lo", LO), ("hi", HI)):
        for number in range(24):
            session = _uid("edge", edge_name, number)
            moment = edge - timedelta(
                minutes=rng.randint(1, 20), microseconds=rng.randint(0, 999_999)
            )
            for _ in range(3):
                seed.trace(PROJECT_ID, session, moment)
                moment += timedelta(minutes=rng.randint(2, 15))

    # Remap chains O -> Z -> N. Z's root is tombstoned, its live child matches
    # the model filter; O keeps one live root that does not. A candidate
    # superset leaking into a filtered chart changes these sessions.
    chain_time = LO + timedelta(hours=20)
    for number in range(8):
        old, middle, new = (
            _uid("chain-old", number),
            _uid("chain-mid", number),
            _uid("chain-new", number),
        )
        seed.remaps.extend(
            [
                (uuid.UUID(old), uuid.UUID(middle), 1),
                (uuid.UUID(middle), uuid.UUID(new), 1),
            ]
        )
        moment = chain_time + timedelta(minutes=number)
        root_old, rows_old = seed.trace(PROJECT_ID, old, moment, max_children=0)
        root_old["model"] = "gpt-x"
        root_mid, _ = seed.trace(PROJECT_ID, middle, moment, max_children=0)
        root_mid["model"] = "gpt-x"
        seed.versions[0].append(seed.bump(root_mid, is_deleted=1))
        child = seed.span(
            PROJECT_ID,
            "llm",
            root_mid["service_name"],
            moment + timedelta(seconds=1),
            root_mid["trace_id"],
            root_mid["id"],
            middle,
            10,
            model="claude-y",
        )
        seed.base.append(child)
        seed.children.append(child)
    # A remap whose later version points somewhere else, and remaps of ids
    # that own no span at all.
    straddler = next(row for row in seed.remaps if row[2] == 1)
    seed.remaps.append((straddler[0], uuid.UUID(_uid("later-target")), 5))
    seed.remaps.extend(
        (uuid.UUID(_uid("orphan-old", index)), uuid.UUID(_uid("orphan-new", index)), 1)
        for index in range(10)
    )

    # Another tenant in the same hours.
    for number in range(70):
        start = span_lo + timedelta(seconds=rng.randint(0, span_seconds))
        seed.trace(NOISE_PROJECT_ID, _uid("noise-session", number), start)

    # Version histories. Every change keeps the replacement key (the start
    # hour included), so FINAL must collapse it.
    counts = dict.fromkeys(
        (
            "reversion",
            "tombstone",
            "resurrect",
            "root_to_child",
            "child_to_root",
            "session_move",
            "time_correction",
            "tie",
        ),
        0,
    )
    roots = list(seed.roots)
    for root in roots:
        draw = rng.random()
        if draw < 0.08:
            latency = rng.randint(100, 60_000)
            seed.versions[rng.randint(0, 2)].append(
                seed.bump(
                    root,
                    latency_ms=latency,
                    end_time=root["start_time"] + timedelta(milliseconds=latency),
                    total_tokens=root["total_tokens"] + 7,
                    cost=root["cost"] + 0.01,
                    status=rng.choice(("OK", "ERROR")),
                )
            )
            counts["reversion"] += 1
        elif draw < 0.13:
            seed.versions[rng.randint(0, 2)].append(seed.bump(root, is_deleted=1))
            counts["tombstone"] += 1
        elif draw < 0.16:
            seed.versions[0].append(seed.bump(root, 1, is_deleted=1))
            seed.versions[2].append(
                seed.bump(root, 2, latency_ms=root["latency_ms"] + 333)
            )
            counts["resurrect"] += 1
        elif draw < 0.19:
            seed.versions[rng.randint(0, 2)].append(
                seed.bump(root, parent_span_id=_uid("reparent", root["id"]))
            )
            counts["root_to_child"] += 1
        elif draw < 0.22:
            target = rng.choice(roots)["trace_session_id"]
            seed.versions[rng.randint(0, 2)].append(
                seed.bump(root, trace_session_id=target)
            )
            counts["session_move"] += 1
        elif draw < 0.25:
            hour = root["start_time"].replace(minute=0, second=0, microsecond=0)
            moved = hour + timedelta(
                seconds=rng.randint(0, 3599), microseconds=rng.randint(0, 999_999)
            )
            seed.versions[rng.randint(0, 2)].append(seed.bump(root, start_time=moved))
            counts["time_correction"] += 1
        elif draw < 0.27:
            # Equal _version, different payload: FINAL keeps the last insert.
            seed.versions[1].append({**root, "latency_ms": root["latency_ms"] + 4_444})
            counts["tie"] += 1
    for child in list(seed.children):
        draw = rng.random()
        if draw < 0.03:
            seed.versions[rng.randint(0, 2)].append(seed.bump(child, parent_span_id=""))
            counts["child_to_root"] += 1
        elif draw < 0.08:
            seed.versions[rng.randint(0, 2)].append(
                seed.bump(child, latency_ms=child["latency_ms"] + 17)
            )
            counts["reversion"] += 1
        elif draw < 0.11:
            seed.versions[rng.randint(0, 2)].append(seed.bump(child, is_deleted=1))
            counts["tombstone"] += 1
    # Start-time corrections that cross an exact window edge inside the edge
    # hour: the older version is outside the window, the newer one inside.
    for edge, inside in ((LO, True), (HI, False)):
        for number in range(6):
            session = _uid("edge-correction", edge.isoformat(), number)
            outside = (
                edge - timedelta(minutes=3 + number)
                if inside
                else edge + timedelta(minutes=3 + number)
            )
            corrected = (
                edge + timedelta(minutes=2 + number)
                if inside
                else edge - timedelta(minutes=2 + number)
            )
            root, _ = seed.trace(PROJECT_ID, session, outside, max_children=2)
            seed.versions[1].append(seed.bump(root, start_time=corrected))
            counts["time_correction"] += 1
    seed.facts = counts
    return seed


_SPAN_COLUMNS = (
    "project_id",
    "observation_type",
    "service_name",
    "start_time",
    "trace_id",
    "id",
    "parent_span_id",
    "name",
    "end_time",
    "latency_ms",
    "trace_session_id",
    "status",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cost",
    "attrs_string",
    "is_deleted",
    "_version",
)


def _load(client, seed):
    client.execute("SYSTEM STOP MERGES spans")
    client.execute("SYSTEM STOP MERGES trace_session_id_remap")
    insert = f"INSERT INTO spans ({', '.join(_SPAN_COLUMNS)}) VALUES"
    no_collapse = {"optimize_on_insert": 0}

    def write(rows):
        # One day partition per INSERT: a block spread over several
        # partitions opens a part writer (every column, index and projection
        # stream) per partition at once, which a shared test server's
        # memory limit does not always allow.
        by_day = {}
        for row in rows:
            by_day.setdefault(row["start_time"].date(), []).append(row)
        for day in sorted(by_day):
            client.execute(
                insert,
                [tuple(row[name] for name in _SPAN_COLUMNS) for row in by_day[day]],
                settings=no_collapse,
            )

    base = list(seed.base)
    random.Random(7).shuffle(base)
    for index in range(4):
        write(base[index::4])
    for batch in seed.versions:
        write(batch)
    version = datetime(2020, 1, 1, tzinfo=UTC)
    client.execute(
        "INSERT INTO trace_session_id_remap (old_id, new_id, version) VALUES",
        [
            (old, new, version + timedelta(seconds=step))
            for old, new, step in seed.remaps
        ],
        settings=no_collapse,
    )


def _apply_v2_schema(database):
    from tracer.services.clickhouse.v2 import apply_schema

    _open_ch_test_http_client(database=database).close()
    rc = apply_schema.main(
        [
            "--schema-dir",
            str(_SCHEMA_DIR),
            "--ch-host",
            os.environ.get("CH25_HOST", "127.0.0.1"),
            "--ch-http-port",
            str(_ch_test_http_port().port),
            "--ch-user",
            os.environ.get("CH25_USER") or os.environ.get("CH_USERNAME") or "default",
            "--ch-password",
            os.environ.get("CH25_PASSWORD") or os.environ.get("CH_PASSWORD") or "",
            "--ch-database",
            database,
        ]
    )
    assert rc == 0, f"v2 schema apply failed with rc={rc}"


# ---------------------------------------------------------------------------
# Live reader
# ---------------------------------------------------------------------------


class _LiveAnalytics:
    """Run the product's own statements; tag each one for ``system.query_log``."""

    supports_per_query_read_settings = True

    def __init__(self, client, tag):
        self._client = client
        self._tag = tag
        self.calls = []

    def execute_ch_query(
        self, query, params=None, *, timeout_ms=None, settings=None, **_
    ):
        del timeout_ms
        bound = {
            key: tuple(value) if isinstance(value, list) else value
            for key, value in (params or {}).items()
        }
        comment = f"{self._tag}:{len(self.calls)}:{uuid.uuid4().hex}"
        sent = dict(settings or {})
        rows, columns = self._client.execute(
            query,
            bound,
            with_column_types=True,
            settings={**sent, "log_comment": comment},
        )
        names = [name for name, _type in columns]
        data = [dict(zip(names, row, strict=True)) for row in rows]
        self.calls.append(
            SimpleNamespace(
                sql=query, params=bound, settings=sent, rows=data, comment=comment
            )
        )
        return SimpleNamespace(data=data, columns=names)


def _filter_cases():
    return {
        "none": [_WINDOW_FILTER],
        # Lean candidates + a HAVING on the per-session aggregate.
        "duration": [
            _WINDOW_FILTER,
            {
                "column_id": "duration",
                "filter_config": {
                    "col_type": "SYSTEM_METRIC",
                    "filter_type": "number",
                    "filter_op": "greater_than",
                    "filter_value": 600,
                },
            },
        ],
        # Lean candidates + a resolved-session id predicate (a remap survivor
        # and a chain survivor among the excluded ids).
        "session_id": [
            _WINDOW_FILTER,
            {
                "column_id": "session_id",
                "filter_config": {
                    "col_type": "SYSTEM_METRIC",
                    "filter_type": "text",
                    "filter_op": "not_in",
                    "filter_value": [
                        _uid("session", 3),
                        _uid("session", 5),
                        _uid("chain-old", 0),
                        _uid("edge", "lo", 1),
                    ],
                },
            },
        ],
        # Span-level membership leaves: exact FINAL candidates, read once.
        "model": [
            _WINDOW_FILTER,
            {
                "column_id": "model",
                "filter_config": {
                    "col_type": "SYSTEM_METRIC",
                    "filter_type": "text",
                    "filter_op": "equals",
                    "filter_value": "claude-y",
                },
            },
        ],
        "company": [
            _WINDOW_FILTER,
            {
                "column_id": "company_id",
                "filter_config": {
                    "col_type": "SPAN_ATTRIBUTE",
                    "filter_type": "text",
                    "filter_op": "in",
                    "filter_value": list(COMPANIES[:2]),
                },
            },
        ],
    }


FILTER_CASES = tuple(_filter_cases())
LEAN_CASES = ("none", "duration", "session_id")


def _read(graph, analytics, filters, metric_id, *, dev):
    """Run the public reader; ``dev`` removes only the lean switch and the
    session settings override, which is dev's statement exactly."""

    if not dev:
        return graph.read_exact_session_system_graph(
            analytics=analytics,
            project_id=PROJECT_ID,
            filters=filters,
            interval="hour",
            metric_id=metric_id,
        )
    original_source = graph._session_aggregate_source_sql

    def dev_source(**kwargs):
        kwargs.pop("lean_graph_source", None)
        return original_source(**kwargs)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(graph, "_session_aggregate_source_sql", dev_source)
        # Dev ran the statement on the shared exact settings, unchanged.
        patcher.setattr(
            graph,
            "_session_graph_read_settings",
            lambda: dict(graph.EXACT_GRAPH_READ_SETTINGS),
            raising=False,
        )
        return graph.read_exact_session_system_graph(
            analytics=analytics,
            project_id=PROJECT_ID,
            filters=filters,
            interval="hour",
            metric_id=metric_id,
        )


def _graph_call(calls):
    """The one statement that produced the graph rows (never the probe)."""

    graph_calls = [
        call
        for call in calls
        if not call.sql.lstrip().startswith("SELECT 1 AS has_raw_witness")
    ]
    assert len(graph_calls) == 1, [call.sql[:80] for call in calls]
    return graph_calls[0]


def _session_source(sql):
    head, rest = sql.split("FROM (", 1)
    assert "primary_traffic" in head
    source, _tail = rest.rsplit(") AS exact_sessions", 1)
    return source


@pytest.fixture(scope="module")
def store():
    from tracer.services.clickhouse import exact_graph_reads as graph

    seed = _build_seed()
    with _ch_test_owned_database("test_session_lean_") as database:
        _apply_v2_schema(database)
        with _ch_test_native_client(database=database) as client:
            _load(client, seed)
            reads = {}
            for case, filters in _filter_cases().items():
                for metric in METRICS:
                    for label, dev in (("lean", False), ("dev", True)):
                        analytics = _LiveAnalytics(client, f"{label}:{case}:{metric}")
                        payload = _read(graph, analytics, filters, metric, dev=dev)
                        reads[(label, case, metric)] = SimpleNamespace(
                            payload=payload, calls=analytics.calls
                        )
            inner = {}
            for case in FILTER_CASES:
                for label in ("lean", "dev"):
                    call = _graph_call(reads[(label, case, "latency")].calls)
                    # Same statement settings, minus the graph's result-row cap
                    # (this reads one row per session, not per bucket).
                    inner_settings = {
                        name: value
                        for name, value in call.settings.items()
                        if name not in {"max_result_rows", "max_result_bytes"}
                    }
                    rows, columns = client.execute(
                        f"SELECT * FROM ({_session_source(call.sql)}) ORDER BY session_id",
                        call.params,
                        with_column_types=True,
                        settings=inner_settings,
                    )
                    names = [name for name, _type in columns]
                    inner[(label, case)] = [
                        dict(zip(names, row, strict=True)) for row in rows
                    ]
            client.execute("SYSTEM FLUSH LOGS")
            log = {}
            for key, read in reads.items():
                for call in read.calls:
                    found = client.execute(
                        "SELECT read_rows, read_bytes, peak_threads_usage"
                        " FROM system.query_log"
                        " WHERE type = 'QueryFinish' AND log_comment = %(comment)s"
                        "   AND event_date >= yesterday()",
                        {"comment": call.comment},
                    )
                    assert len(found) == 1, (key, call.comment, found)
                    log[call.comment] = SimpleNamespace(
                        read_rows=found[0][0],
                        read_bytes=found[0][1],
                        peak_threads=found[0][2],
                    )
            scan_hours = (
                LO.replace(minute=0, second=0, microsecond=0),
                HI.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1),
            )
            physical, physical_roots = client.execute(
                "SELECT count(), countIf(parent_span_id = '') FROM spans"
                " WHERE project_id = %(project)s"
                "   AND toStartOfHour(start_time) >= %(lo)s AND toStartOfHour(start_time) < %(hi)s",
                {
                    "project": uuid.UUID(PROJECT_ID),
                    "lo": scan_hours[0],
                    "hi": scan_hours[1],
                },
            )[0]
            final_live_roots = client.execute(
                "SELECT count() FROM spans FINAL"
                " WHERE project_id = %(project)s AND parent_span_id = ''"
                "   AND start_time >= %(lo)s AND start_time < %(hi)s",
                {"project": uuid.UUID(PROJECT_ID), "lo": LO, "hi": HI},
            )[0][0]
    return SimpleNamespace(
        seed=seed,
        reads=reads,
        inner=inner,
        log=log,
        physical=physical,
        physical_roots=physical_roots,
        final_live_roots=final_live_roots,
        graph=graph,
    )


# ---------------------------------------------------------------------------
# The seed exercises what it claims to
# ---------------------------------------------------------------------------


def test_seed_exercises_every_version_and_session_shape(store):
    facts = store.seed.facts
    assert facts["reversion"] >= 30 and facts["tombstone"] >= 20
    for name in (
        "resurrect",
        "root_to_child",
        "child_to_root",
        "session_move",
        "time_correction",
        "tie",
    ):
        assert facts[name] >= 5, (name, facts)
    assert len(store.seed.remaps) >= 30
    assert store.physical > 2 * store.physical_roots > 0
    assert store.final_live_roots > 500
    sessions = {case: len(store.inner[("dev", case)]) for case in FILTER_CASES}
    # Every filtered case selects a proper, non-empty subset of the sessions.
    for case in FILTER_CASES[1:]:
        assert 0 < sessions[case] < sessions["none"], sessions
    assert sessions["none"] > 300, sessions


# ---------------------------------------------------------------------------
# Same answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", FILTER_CASES)
@pytest.mark.parametrize("metric", METRICS)
def test_every_bucket_equals_devs_statement(store, case, metric):
    lean = _graph_call(store.reads[("lean", case, metric)].calls).rows
    dev = _graph_call(store.reads[("dev", case, metric)].calls).rows
    assert [row["time_bucket"] for row in lean] == [row["time_bucket"] for row in dev]
    assert len(dev) > 10
    for mine, theirs in zip(lean, dev, strict=True):
        assert mine["primary_traffic"] == theirs["primary_traffic"], (mine, theirs)
        assert abs(float(mine["value"]) - float(theirs["value"])) <= TOLERANCE, (
            mine,
            theirs,
        )
    lean_points = store.reads[("lean", case, metric)].payload["data"]
    dev_points = store.reads[("dev", case, metric)].payload["data"]
    assert [p["timestamp"] for p in lean_points] == [p["timestamp"] for p in dev_points]
    for mine, theirs in zip(lean_points, dev_points, strict=True):
        assert mine["primary_traffic"] == theirs["primary_traffic"]
        # The payload rounds to 9 decimals; allow one unit of that rounding.
        assert abs(mine["value"] - theirs["value"]) <= TOLERANCE + 1e-12


@pytest.mark.parametrize("case", FILTER_CASES)
def test_every_session_row_equals_devs_statement(store, case):
    lean = store.inner[("lean", case)]
    dev = store.inner[("dev", case)]
    assert [row["session_id"] for row in lean] == [row["session_id"] for row in dev]
    for mine, theirs in zip(lean, dev, strict=True):
        assert mine.keys() == theirs.keys()
        for name, value in theirs.items():
            if isinstance(value, float):
                assert abs(mine[name] - value) <= TOLERANCE, (name, mine, theirs)
            else:
                assert mine[name] == value, (name, mine, theirs)


# ---------------------------------------------------------------------------
# Cheaper shape
# ---------------------------------------------------------------------------


def _final_scans(sql):
    return len(re.findall(r"\bFROM\s+spans\s+FINAL\b", sql))


@pytest.mark.parametrize("case", FILTER_CASES)
def test_statement_reads_spans_final_once_per_needed_relation(store, case):
    lean = _graph_call(store.reads[("lean", case, "latency")].calls).sql
    dev = _graph_call(store.reads[("dev", case, "latency")].calls).sql
    # Dev: candidates + aggregate as text, four physical scans (see below).
    assert _final_scans(dev) == 2
    if case in LEAN_CASES:
        # One FINAL scan (the aggregate); candidates come from root versions.
        assert _final_scans(lean) == 1
        candidates = lean.split("candidate_physical_session_ids AS (", 1)[1].split(
            "),", 1
        )[0]
        assert "FINAL" not in candidates
        assert re.search(r"WHERE\s+parent_span_id\s*=\s*''", candidates)
    else:
        assert _final_scans(lean) == 2
    # Candidates are read once, into a scalar array, on both paths.
    assert "candidate_session_remap_candidate_array" in lean


@pytest.mark.parametrize("case", FILTER_CASES)
def test_query_log_reads_fewer_rows_on_background_threads(store, case):
    lean = _graph_call(store.reads[("lean", case, "latency")].calls)
    dev = _graph_call(store.reads[("dev", case, "latency")].calls)
    lean_log, dev_log = store.log[lean.comment], store.log[dev.comment]
    graph = store.graph
    # Background-only parallelism on the statement; the shared interactive
    # pin every other exact read uses is unchanged.
    threads = graph.settings.EXACT_GRAPH_SESSION_READ_MAX_THREADS
    assert threads > graph.settings.FILTER_SELECTOR_MAX_THREADS
    assert lean.settings["max_threads"] == threads
    # Only the thread budget differs from the shared exact envelope.
    assert {k: v for k, v in lean.settings.items() if k != "max_threads"} == {
        k: v for k, v in dev.settings.items() if k != "max_threads"
    }
    assert dev.settings["max_threads"] == graph.settings.FILTER_SELECTOR_MAX_THREADS
    # ``Settings['max_threads']`` is blank when the value equals the server's
    # auto default, so prove the parallelism by the threads the query used.
    assert lean_log.peak_threads > dev_log.peak_threads, (lean_log, dev_log)
    assert (
        graph.EXACT_GRAPH_READ_SETTINGS["max_threads"]
        == graph.settings.FILTER_SELECTOR_MAX_THREADS
    )
    # Dev reads every scanned physical span four times (the FINAL source is
    # expanded at each reference). The lean unfiltered statement reads it
    # once plus one narrow root-version pass; a filtered one saves two of
    # the four FINAL passes.
    assert dev_log.read_rows >= 4 * store.physical
    if case in LEAN_CASES:
        assert lean_log.read_rows <= 0.6 * dev_log.read_rows, (lean_log, dev_log)
        assert lean_log.read_bytes <= 0.5 * dev_log.read_bytes, (lean_log, dev_log)
    else:
        assert lean_log.read_rows <= dev_log.read_rows - 1.5 * store.physical, (
            lean_log,
            dev_log,
        )
        assert lean_log.read_bytes <= 0.75 * dev_log.read_bytes, (lean_log, dev_log)
