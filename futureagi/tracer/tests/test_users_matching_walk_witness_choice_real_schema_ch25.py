"""Live ClickHouse 25 on the DEPLOYED schema: the Users walk's two cost fixes.

Runs on the lane database provisioned with the repo's own DDL
(``provision-lane-ch-db.sh``, named by ``CH25_DATABASE``), under the same gate
as the other ``*_real_schema_ch25.py`` modules. Every statement goes through an
executor that applies the application's read settings, sends
``server_execution_cap_ms`` as ``max_execution_time`` exactly as
``AnalyticsQueryService`` does, and tags each statement with a query id of its
own, so ``system.query_log`` proves what the PRODUCT sent, not a harness clamp.

* R: a raw value on every span (``env = production``) and a rare
  ``status = ERROR``. The first page costs both witnesses with one
  ``EXPLAIN ESTIMATE`` each (no column rows read, each capped on the server)
  and walks the error leaf; the pages publish exactly the users graph's
  members, once, newest error first.
* U: a scope with no end user over twelve months, the tail estimate refused
  (forced below it, as production refused MUD's 408 marks). Three statements
  - the first slice, the estimate, the witness-free presence statement - and
  an empty, complete page; the presence statement is capped and reads next to
  nothing.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from conftest import _ch_test_native_client
from tracer.services import users_matching_walk as walk
from tracer.services.clickhouse import exact_graph_reads
from tracer.services.clickhouse.application_read_policy import (
    application_read_context,
    application_read_settings,
)
from tracer.services.clickhouse.list_cursor import ListCursor
from tracer.services.clickhouse.read_budget import ReadDeadlineExceeded
from tracer.services.users_list_manager import UsersListManager

pytestmark = pytest.mark.integration

ORGANIZATION = str(uuid.UUID(int=373))
R = str(uuid.uuid4())
U = str(uuid.uuid4())
USERS = 2_000
# R: 400,000 spans over two days, one error in 4,000 spans.
R_START = datetime(2026, 8, 1, tzinfo=UTC)
R_END = R_START + timedelta(days=2)
# U: 300,000 user-less spans over twelve months.
U_END = datetime(2026, 9, 1, tzinfo=UTC)
U_START = U_END - timedelta(days=365)
SERVICE = "tracer.services.users_list_manager.V2AnalyticsQueryService"
TAG = f"r5live-{uuid.uuid4().hex[:10]}"


def _lane_database() -> str:
    database = (os.environ.get("CH25_DATABASE") or "").strip()
    if not database:
        pytest.skip("no lane database named: set CH25_DATABASE")
    # CI gives every job its own throwaway ClickHouse, whose test_tfc carries
    # the deployed schema and runs one test at a time. Locally test_tfc is
    # shared with other runs, so a local run needs its own database
    # provisioned with provision-lane-ch-db.sh.
    if database == "test_tfc" and os.environ.get("GITHUB_ACTIONS") == "true":
        return database
    if database == "test_tfc" or not database.startswith("test_"):
        pytest.skip(
            f"not writing to {database!r}: point CH25_DATABASE at a database "
            "provisioned with provision-lane-ch-db.sh"
        )
    return database


@pytest.fixture(scope="module")
def ch_client():
    database = _lane_database()
    with _ch_test_native_client(database=database) as client:
        kind = client.execute(
            "SELECT default_kind FROM system.columns WHERE database = "
            "currentDatabase() AND table = 'spans' AND name = 'trace_name'"
        )
        if kind != [("MATERIALIZED",)]:
            pytest.fail(
                f"{database} does not carry the deployed spans schema; "
                "provision it with provision-lane-ch-db.sh"
            )
        yield client


def _user(n: str) -> str:
    return f"reinterpretAsUUID(concat(unhex(lpad(hex({n}), 16, '0')), unhex('00000000000000a5')))"


@pytest.fixture(scope="module")
def worlds(ch_client):
    spans = """
    INSERT INTO spans (project_id, observation_type, service_name, start_time,
      trace_id, id, name, end_time, end_user_id, status, model, provider,
      attrs_string, is_deleted, _version)
    """
    try:
        ch_client.execute(
            f"""{spans}
            SELECT toUUID('{R}'), 'llm', 'svc',
              toDateTime64('2026-08-01 00:00:00', 6, 'UTC')
                + toIntervalMicrosecond(number * 432000),
              toString(generateUUIDv4(number)), concat('r', toString(number)), 'op',
              NULL, {_user("number % " + str(USERS))},
              if(cityHash64(number, 'err') % 4000 = 0, 'ERROR', 'OK'),
              'gpt-4o', 'openai', map('env', 'production'), 0, 1
            FROM numbers(400000)
            """
        )
        ch_client.execute(
            f"""{spans}
            SELECT toUUID('{U}'), 'llm', 'svc',
              toDateTime64('2025-09-01 00:00:00', 6, 'UTC')
                + toIntervalMicrosecond(intDiv(number * 31536000000000, 300000)),
              toString(generateUUIDv4(number)), concat('u', toString(number)), 'op',
              NULL, NULL,
              if(cityHash64(number, 'err') % 2000 = 0, 'ERROR', 'OK'),
              'gpt-4o', 'openai', map('env', 'production'), 0, 1
            FROM numbers(300000)
            """,
            # A year of daily partitions in one block.
            settings={"max_partitions_per_insert_block": 1_000},
        )
        ch_client.execute(
            f"""
            INSERT INTO end_users (project_id, end_user_id, organization_id,
              user_id, user_id_type, first_seen)
            SELECT toUUID('{R}'), {_user("number")}, toUUID('{ORGANIZATION}'),
              concat('user-', toString(number)), 'string',
              toDateTime64('2026-07-01 00:00:00', 6, 'UTC')
            FROM numbers({USERS})
            """
        )
        yield
    finally:
        for table in ("spans", "end_users"):
            ch_client.execute(
                f"ALTER TABLE {table} DELETE WHERE project_id IN "
                "(toUUID(%(r)s), toUUID(%(u)s)) SETTINGS mutations_sync = 2",
                {"r": R, "u": U},
            )


class _TaggedExecutor:
    """What ``AnalyticsQueryService.execute_ch_query`` sends, tagged per statement."""

    def __init__(self, client, case: str):
        self.client = client
        self.prefix = f"{TAG}-{case}-{uuid.uuid4().hex[:6]}"
        self.statements: list[tuple[str, str, int | None]] = []

    def execute_ch_query(
        self,
        query,
        params=None,
        timeout_ms=None,
        settings=None,
        *,
        server_execution_cap_ms=None,
    ):
        query_id = f"{self.prefix}-{len(self.statements)}"
        self.statements.append((query, query_id, server_execution_cap_ms))
        with application_read_context(execution_cap_ms=server_execution_cap_ms):
            sent = application_read_settings(settings)
        started = time.monotonic()
        try:
            rows, columns = self.client.execute(
                query,
                params or {},
                with_column_types=True,
                settings=sent,
                query_id=query_id,
            )
        except Exception as exc:
            if server_execution_cap_ms is not None and getattr(exc, "code", 0) == 159:
                raise ReadDeadlineExceeded("stopped at its cap") from exc
            raise
        names = [name for name, _type in columns]
        return SimpleNamespace(
            data=[dict(zip(names, row, strict=True)) for row in rows],
            columns=names,
            query_time_ms=(time.monotonic() - started) * 1000.0,
        )

    def kinds(self) -> list[str]:
        def kind(query: str) -> str:
            if query.lstrip().startswith("EXPLAIN ESTIMATE"):
                return "estimate"
            if "AS present" in query:
                return "presence"
            if "AS witnessed" in query:
                return "existence"
            if "AS raw_end_user_id" in query:
                return "slice"
            return "other"

        return [kind(query) for query, _id, _cap in self.statements]

    def logged(self, client) -> dict[str, tuple[int, int, str | None]]:
        """Per query id: read rows, read bytes and the max_execution_time sent."""

        client.execute("SYSTEM FLUSH LOGS")
        rows = client.execute(
            "SELECT query_id, read_rows, read_bytes, "
            "Settings['max_execution_time'] FROM system.query_log "
            "WHERE type = 'QueryFinish' AND query_id LIKE %(prefix)s "
            "AND event_date >= today() - 1",
            {"prefix": self.prefix + "-%"},
        )
        return {
            query_id: (read_rows, read_bytes, cap or None)
            for query_id, read_rows, read_bytes, cap in rows
        }


def _date(start: datetime, end: datetime) -> dict:
    return {
        "column_id": "created_at",
        "filter_config": {
            "filter_type": "datetime",
            "filter_op": "between",
            "filter_value": [start.isoformat(), end.isoformat()],
        },
    }


def _leaf(column: str, value: str, col_type: str) -> dict:
    return {
        "column_id": column,
        "filter_config": {
            "col_type": col_type,
            "filter_type": "text",
            "filter_op": "equals",
            "filter_value": value,
        },
    }


STATUS_ERROR = _leaf("status", "ERROR", "SYSTEM_METRIC")
ENV_PRODUCTION = _leaf("env", "production", "SPAN_ATTRIBUTE")


def _page(client, project, filters, *, case, cursor=None):
    manager = UsersListManager(
        organization_id=ORGANIZATION,
        allowed_project_ids=[project],
        project_id=project,
        filters=filters,
        requested_columns=[],
        attribute_keys=[],
    )
    executor = _TaggedExecutor(client, case)
    with patch(SERVICE, return_value=executor):
        read = manager.list_cursor_payload(page_size=25, cursor=cursor)
    return read, executor, manager


def test_a_dense_raw_value_and_a_rare_error_walk_the_error_leaf(ch_client, worlds):
    window_start = R_END - timedelta(days=30)
    filters = [_date(window_start, R_END), STATUS_ERROR, ENV_PRODUCTION]
    names: list[str] = []
    cursor = None
    pages = 0
    # Walls wide enough that a loaded host never degrades a page: this test
    # proves the choice, membership and order, not timing.
    with (
        patch.object(walk, "USER_LIST_PAGE_WALL_MS", 60_000),
        patch.object(walk, "USER_LIST_WALK_FINISH_WALL_MS", 120_000),
    ):
        while True:
            read, executor, manager = _page(
                ch_client, R, filters, case="r", cursor=cursor
            )
            pages += 1
            kinds = executor.kinds()
            assert read.payload["query_status"] == "complete"
            if pages == 1:
                # Both witnesses costed, raw first in the static rank; the
                # error leaf walked.
                assert kinds[:2] == ["estimate", "estimate"]
                assert (manager._walk_witness.family, manager._walk_witness.key) == (
                    "native",
                    "status",
                )
                logged = executor.logged(ch_client)
                for _query, query_id, cap in executor.statements[:2]:
                    read_rows, _bytes, sent_cap = logged[query_id]
                    # Index analysis only: no column rows.
                    assert read_rows <= 1, (query_id, read_rows)
                    assert cap is not None and 25 <= cap <= 1_000
                    assert sent_cap is not None and float(sent_cap) > 0
            else:
                assert "estimate" not in kinds[:1]
                assert manager._walk_witness.key == "status"
            slices = [
                q for q, _id, _cap in executor.statements if "AS raw_end_user_id" in q
            ]
            assert slices and all("toString(status)" in q for q in slices)
            names.extend(row["user_id"] for row in read.payload["table"])
            if not read.has_more:
                break
            cursor = ListCursor(
                window_start=read.window_start,
                window_end=read.window_end,
                order=tuple(read.checkpoint_order),
                seen_rows=read.seen_rows,
            )
            assert pages < 20

    newest = ch_client.execute(
        "SELECT toString(end_user_id), max(start_time) FROM spans "
        "WHERE project_id = toUUID(%(p)s) AND status = 'ERROR' "
        "GROUP BY end_user_id",
        {"p": R},
    )
    label = {
        row[0]: f"user-{n}"
        for n, row in enumerate(
            ch_client.execute(
                f"SELECT toString({_user('number')}) FROM numbers({USERS})"
            )
        )
    }
    expected = [
        label[user]
        for user, _key in sorted(newest, key=lambda row: (row[1], row[0]), reverse=True)
    ]
    assert len(expected) > 25
    assert names == expected
    query, params, _needs_eval = exact_graph_reads._user_id_membership_sql(
        project_id=R,
        filters=filters,
        start_date=window_start,
        end_date=R_END,
        all_snapshot_users=True,
    )
    graph = {label[str(row[0])] for row in ch_client.execute(query, params)}
    assert set(names) == graph


@pytest.mark.parametrize(
    "leaf",
    [STATUS_ERROR, _leaf("model", "gpt-4o", "SYSTEM_METRIC")],
    ids=["status", "model"],
)
def test_a_user_less_twelve_month_scope_is_empty_and_complete_in_three_statements(
    ch_client, worlds, leaf
):
    # Production refused MUD's tail estimate (408 marks over the 1M-row
    # target); here the target sits below any estimate, so the estimate is
    # refused whatever this host's parts look like.
    with patch.object(walk, "USER_LIST_WALK_PROBE_TARGET_READ_ROWS", -1):
        read, executor, _manager = _page(
            ch_client, U, [_date(U_START, U_END), leaf], case="u"
        )
    assert executor.kinds() == ["slice", "estimate", "presence"]
    assert read.payload["table"] == [] and read.has_more is False
    assert read.payload["query_status"] == "complete"
    logged = executor.logged(ch_client)
    _query, query_id, cap = executor.statements[2]
    read_rows, read_bytes, sent_cap = logged[query_id]
    assert cap is not None and 25 <= cap <= 1_000
    assert sent_cap is not None and float(sent_cap) > 0
    # Boundary granules through the end-user projection, never the tail.
    assert read_bytes < 64 * 1024 * 1024, read_bytes
    assert read_rows < 300_000, read_rows
