"""Dataset filter values read from PostgreSQL, and bounded read-only
decoding of evaluation *choice* cell storage.

PostgreSQL holds the dataset and answers one column from its index; the
ClickHouse mirror is ordered by cell id and trails every write by a CDC batch.
Choice cells: TextField historically stored Python repr; newer/imported cells
can contain JSON. This is not a generic JSON flattener or an evaluator of
Python code.
"""

import ast
import io
import json
import math
import tokenize
import uuid
from collections import Counter

from django.db import InterfaceError, OperationalError, connection, transaction

from tracer.services.clickhouse.read_budget import ReadDeadlineExceeded
from tracer.services.postgres_read_policy import application_postgres_reads

MAX_CHOICE_TEXT = 16_384
MAX_CHOICE_NODES = 1024
MAX_CHOICES = 256
MAX_CHOICE_DEPTH = 4


class InvalidChoiceCell(ValueError):
    """The cell cannot provide an exact choice vocabulary."""


def _reject(*_args):
    raise InvalidChoiceCell("Invalid evaluation choice cell")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            _reject()
        result[key] = value
    return result


def _literal(node, depth=0, budget=None):
    """Convert only literal nodes, with no eval/exec or callable/name lookup."""
    if budget is None:
        budget = [MAX_CHOICE_NODES]
    budget[0] -= 1
    if depth > MAX_CHOICE_DEPTH or budget[0] < 0:
        _reject()
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, float):
        return node.value
    if isinstance(node, ast.List):
        return [_literal(item, depth + 1, budget) for item in node.elts]
    if isinstance(node, ast.Dict):
        return _object(
            (_literal(key, depth + 1, budget), _literal(value, depth + 1, budget))
            for key, value in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _literal(node.operand, depth + 1, budget)
        if type(value) in (int, float):
            return -value
    _reject()


# The metadata document of ``model_hub_cell.value_infos``: historical cells
# store the JSON once more as a JSON string, which this unwraps once.
CHOICE_DOCUMENT_SQL = (
    "CASE WHEN jsonb_typeof(value_infos) = 'string' "
    "THEN value_infos #>> '{}' ELSE value_infos::text END"
)

# Over ``val`` (the stored text), ``value_infos`` (its JSONField text),
# ``document`` and ``ascii_form`` (``json.dumps(val)``): a superset of the
# cells ``literal_choice`` accepts, decided without parsing the metadata
# (PostgreSQL 15 cannot parse untrusted JSON text without failing the whole
# statement). The metadata must hold ``val`` as a JSON string. PostgreSQL and
# ``json.dumps(ensure_ascii=False)`` spell it as ``to_jsonb(val)`` does, and
# ``json.dumps`` as ``ascii_form``. Any other spelling escapes a character
# neither writes escaped (\/, upper-case hex, printable ASCII, a character
# with a short escape), or escapes one character from DEL up but writes
# another verbatim, so metadata holding such an escape always qualifies.
LITERAL_CANDIDATE_SQL = (
    f"length(value_infos) <= {MAX_CHOICE_TEXT} "
    "AND (strpos(document, to_jsonb(val)::text) > 0 "
    "OR strpos(document, ascii_form) > 0 "
    r"OR document ~ '\\(/|u([0-9a-f]{0,3}[A-F]|00[2-6]|007[0-9a-e]|000[89acd]))' "
    "OR ((octet_length(document) <> char_length(document) "
    "OR strpos(document, chr(127)) > 0) "
    r"AND document ~ '\\u(007f|00[89a-f]|0[1-9a-f]|[1-9a-f])'))"
)


class _RepeatedKeys(dict):
    """A metadata object whose storage repeated a key."""


def _metadata_object(pairs):
    unique = dict(pairs)
    return unique if len(unique) == len(pairs) else _RepeatedKeys(unique)


def literal_choice(value, value_infos):
    """Whether the cell's own metadata names ``value`` itself as the choice.

    ``value_infos`` is the stored JSONField text. Historical cells hold that
    JSON once more as a JSON string, so one string layer is unwrapped. Only
    the metadata object and its ``data`` object must have unique keys.
    """
    if not isinstance(value_infos, str) or len(value_infos) > MAX_CHOICE_TEXT:
        return False
    try:
        infos = json.loads(
            value_infos, parse_constant=_reject, object_pairs_hook=_metadata_object
        )
        if isinstance(infos, str):
            infos = json.loads(
                infos, parse_constant=_reject, object_pairs_hook=_metadata_object
            )
    except (ValueError, RecursionError):
        return False
    if type(infos) is not dict or infos.get("output") != "choices":
        return False
    result = infos.get("data")
    if isinstance(result, dict):
        keys = result.keys() & {"result", "choice"}
        if type(result) is not dict or len(keys) != 1:
            return False
        result = result[keys.pop()]
    return isinstance(result, str) and result == value


def _historical(text):
    # Reject expressions, comments and implicit adjacent-string concatenation
    # before AST conversion. Only the tokens emitted by repr of these cells
    # are needed; neither Python execution nor permissive string repair is used.
    previous = None
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type in (tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER):
            continue
        if token.type == tokenize.OP and token.string in "[]{}:,-":
            previous = token.type
            continue
        if token.type not in (tokenize.STRING, tokenize.NUMBER):
            _reject()
        if token.type == previous == tokenize.STRING:
            _reject()
        previous = token.type
    return _literal(ast.parse(text, mode="eval").body)


def evaluation_choice_labels(serialized, *, literal=False):
    """Return exact string labels, or reject instead of suggesting a raw blob.

    Call only for authorized evaluation-origin array columns. Generic dataset
    JSON, numeric/boolean evaluations, reasons and tags retain their readers.
    """
    if not isinstance(serialized, str) or len(serialized) > MAX_CHOICE_TEXT:
        _reject()
    # Raw scalar/literal paths must be safe for the cursor's UTF-8 digest too.
    try:
        serialized.encode("utf-8")
    except UnicodeEncodeError:
        _reject()
    text = serialized.strip()
    if not text:
        return []
    # A literal choice can itself look like a container. Only same-cell,
    # structured producer evidence exactly matching the raw value resolves it.
    if literal:
        return [serialized]
    if text[0] not in "[{":
        # A scalar containing quote characters is still the literal choice.
        return [serialized]
    try:
        try:
            parsed = json.loads(text, parse_constant=_reject, object_pairs_hook=_object)
        except json.JSONDecodeError:
            parsed = _historical(text)
    except (
        SyntaxError,
        ValueError,
        RecursionError,
        OverflowError,
        tokenize.TokenError,
    ):
        _reject()

    if isinstance(parsed, dict):
        keys = set(parsed)
        choice_keys = keys & {"choice", "choices"}
        if len(choice_keys) != 1 or keys - {"choice", "choices", "score"}:
            _reject()
        if "score" in parsed:
            score = parsed["score"]
            if type(score) not in (int, float):
                _reject()
            try:
                if not math.isfinite(score):
                    _reject()
            except OverflowError:
                _reject()
        parsed = parsed[next(iter(choice_keys))]
        if "choice" in choice_keys and not isinstance(parsed, str):
            _reject()
        if isinstance(parsed, str):
            parsed = [parsed]
    if not isinstance(parsed, list) or len(parsed) > MAX_CHOICES:
        _reject()
    if any(not isinstance(item, str) or not item.strip() for item in parsed):
        _reject()
    # JSON combines valid surrogate pairs; historical lone surrogate escapes
    # are not Unicode scalar labels and must fail before cursor serialization.
    try:
        for item in parsed:
            item.encode("utf-8")
    except UnicodeEncodeError:
        _reject()
    return parsed


class DatasetValuesTooBroad(ValueError):
    """More values match than the caller may return exactly."""


class DatasetValuesTooLarge(RuntimeError):
    """The matching values exceed the read's result-byte budget."""


# The read failures a picker answers 503 for: the request wall, a PostgreSQL
# statement timeout or lost connection, and an answer over the byte budget.
# Anything else is a defect in the read and must reach Sentry.
UNAVAILABLE_READ_ERRORS = (
    ReadDeadlineExceeded,
    OperationalError,
    InterfaceError,
    DatasetValuesTooLarge,
)


def _read(deadline, wall_ms, read):
    """Run ``read(fetch)`` in one read-only snapshot, each statement timed.

    Every statement gets the remaining request wall as its PostgreSQL
    ``statement_timeout``. It reads the primary, so a value written a moment
    ago is suggested at once.
    """

    def remaining_ms():
        return deadline.remaining_ms(wall_ms)

    with application_postgres_reads(
        connection=connection,
        atomic=transaction.atomic,
        check_request=remaining_ms,
        statement_timeout_ms=remaining_ms,
        read_only=True,
        repeatable_read=True,
    ):
        with connection.cursor() as cursor:

            def fetch(sql, params):
                cursor.execute(sql, params)
                columns = [col[0] for col in cursor.description]
                return [dict(zip(columns, row, strict=True)) for row in cursor]

            return read(fetch)


def _fetch_bounded(fetch, select, params, *, size, order, max_bytes):
    """Fetch ``select`` in ``order``, refusing an answer over ``max_bytes``.

    PostgreSQL has no result-size cap. The statement stops one row past the
    budget, so an oversized answer is refused without being transferred.
    """
    rows = fetch(
        "SELECT * FROM ("
        f"SELECT *, sum({size}) OVER (ORDER BY {order}) AS result_bytes "
        f"FROM ({select}) AS inventory"
        f") AS bounded WHERE result_bytes - ({size}) <= %(max_result_bytes)s "
        f"ORDER BY {order}",
        {**params, "max_result_bytes": max_bytes},
    )
    if any(row["result_bytes"] > max_bytes for row in rows):
        raise DatasetValuesTooLarge("result_bytes")
    return rows


def _distinct_values(fetch, select, params, *, max_values, max_bytes):
    """Fetch the ``val`` rows of ``select``, refusing more than ``max_values``."""
    rows = _fetch_bounded(
        fetch,
        f"{select}ORDER BY val LIMIT %(result_limit)s",
        {**params, "result_limit": max_values + 1},
        size="octet_length(val)",
        order="val",
        max_bytes=max_bytes,
    )
    if len(rows) > max_values:
        raise DatasetValuesTooBroad()
    return rows


# Dataset widget dimensions whose vocabulary PostgreSQL answers from the
# dataset and column tables. The ClickHouse mirror is ordered by cell id, so a
# workspace's column names read the whole cell table, and it trails every
# write (on dev it also misses whole columns and datasets).
_DATASET_WORKSPACE_ROWS = (
    "FROM model_hub_dataset AS d "
    "WHERE d.workspace_id = %(workspace_id)s "
    "AND d.organization_id = %(organization_id)s "
    "AND d.deleted = false "
)
# The widgets read cells, so a column is suggested only while it is live and
# holds a live cell, as when the names came from the cells. The probe stops at
# the column's first live cell on the column index; adding the cell's dataset
# makes the planner intersect the dataset index (dev: 3.5 s instead of 25 ms).
_DATASET_LIVE_COLUMN_ROWS = (
    "FROM model_hub_column AS col "
    f"WHERE col.dataset_id = ANY(ARRAY(SELECT d.id {_DATASET_WORKSPACE_ROWS})) "
    "AND col.deleted = false "
    "AND EXISTS (SELECT 1 FROM model_hub_cell AS c "
    "WHERE c.column_id = col.id AND c.deleted = false) "
)
_DATASET_METADATA_VALUES = {
    "dataset": ("d.name", _DATASET_WORKSPACE_ROWS),
    "eval_template": ("col.name", _DATASET_LIVE_COLUMN_ROWS),
    "column_name": ("col.name", _DATASET_LIVE_COLUMN_ROWS),
    "column_source": ("col.source", _DATASET_LIVE_COLUMN_ROWS),
}
DATASET_METADATA_METRICS = frozenset(_DATASET_METADATA_VALUES)


def read_dataset_metadata_values(
    workspace, metric_name, *, search, max_values, max_bytes, deadline, wall_ms
):
    """Return a workspace's distinct dataset or column names, in byte order.

    ``metric_name`` is one of ``DATASET_METADATA_METRICS``. More matching
    values than ``max_values`` raise ``DatasetValuesTooBroad``.
    """
    expression, rows = _DATASET_METADATA_VALUES[metric_name]
    params = {
        "workspace_id": workspace.id,
        "organization_id": workspace.organization_id,
        "search": search,
    }
    return [
        row["val"]
        for row in _read(
            deadline,
            wall_ms,
            lambda fetch: _distinct_values(
                fetch,
                f'SELECT DISTINCT {expression} COLLATE "C" AS val {rows}'
                f"AND {expression} <> '' "
                "AND (%(search)s = '' OR "
                f"strpos(lower({expression}), lower(%(search)s)) > 0) ",
                params,
                max_values=max_values,
                max_bytes=max_bytes,
            ),
        )
    ]


_CELL_ROWS = (
    "FROM model_hub_cell "
    "WHERE dataset_id = %(dataset_id)s "
    "AND column_id = %(column_id)s "
    "AND deleted = false "
    "AND value <> '' "
)


def _cell_params(dataset_id, column_id, search):
    return {
        "dataset_id": uuid.UUID(str(dataset_id)),
        "column_id": uuid.UUID(str(column_id)),
        "search": search,
    }


def read_column_values(
    dataset_id, column_id, *, search, max_values, max_bytes, deadline, wall_ms
):
    """Return one column's distinct non-empty values containing ``search``.

    More matching values than ``max_values`` raise ``DatasetValuesTooBroad``,
    never a sample. The caller validates that the column is the user's.
    """
    return [
        row["val"]
        for row in _read(
            deadline,
            wall_ms,
            lambda fetch: _distinct_values(
                fetch,
                f"SELECT DISTINCT value AS val {_CELL_ROWS}"
                "AND (%(search)s = '' OR strpos(lower(value), lower(%(search)s)) > 0) ",
                _cell_params(dataset_id, column_id, search),
                max_values=max_values,
                max_bytes=max_bytes,
            ),
        )
    ]


# Choice labels are decoded in Python, so an eval-choice search cannot be
# answered by matching the stored text. It can still be *bounded* by it. A
# decoded label differs from its storage only at a backslash escape, and an
# ASCII-only case-insensitive match agrees with Python's casefold only while
# both sides stay ASCII, so keeping every row that satisfies any of those three
# arms can never drop a row the decoded filter would have kept. Without it a
# narrow search still reads the whole inventory and a column above the cap
# answers 422 no matter what the user types, which the error's own advice
# cannot resolve.
_CHOICE_SEARCH_SQL = (
    # The "C" collation lowercases ASCII letters only.
    'AND (strpos(lower(value COLLATE "C"), '
    'lower(%(choice_search)s COLLATE "C")) > 0 '
    # chr(92) is a backslash: escaped storage may decode to a label whose
    # characters are not literally present.
    "OR strpos(value, chr(92)) > 0 "
    # A non-ASCII cell may casefold differently than it lowercases; never let
    # this arm decide such a row.
    "OR octet_length(value) <> char_length(value)) "
)


def read_choice_column_values(
    dataset_id, column_id, *, search, max_values, max_bytes, deadline, wall_ms
):
    """Return each distinct stored text of an eval-choice column with its modes.

    Mode bit 1: some cell reads the text as a container of labels. Bit 2: some
    cell's own metadata names the text itself as the choice. ``search`` only
    bounds the read; the caller filters the decoded labels. More stored texts
    than ``max_values`` raise ``DatasetValuesTooBroad``.
    """
    params = _cell_params(dataset_id, column_id, search)
    choice_search = search.strip()
    search_clause = ""
    if choice_search and choice_search.isascii():
        search_clause = _CHOICE_SEARCH_SQL
        params["choice_search"] = choice_search
    rows_sql = f"{_CELL_ROWS}{search_clause}"

    def read(fetch):
        values = _distinct_values(
            fetch,
            f"SELECT value AS val, count(*) AS cells {rows_sql}GROUP BY value ",
            params,
            max_values=max_values,
            max_bytes=max_bytes,
        )
        literal = _literal_cells(
            fetch, rows_sql, params, [row["val"] for row in values], max_bytes
        )
        return [
            (
                row["val"],
                (2 if literal[row["val"]] else 0)
                | (1 if literal[row["val"]] < row["cells"] else 0),
            )
            for row in values
        ]

    return _read(deadline, wall_ms, read)


def _literal_cells(fetch, rows_sql, params, values, max_bytes):
    """Count, per stored text, the cells whose metadata names it the choice.

    Only bracketed storage reads differently as a literal (a scalar is its own
    label either way). SQL ships only the few cells whose metadata may name
    their text (``LITERAL_CANDIDATE_SQL``), and ``literal_choice`` decides.
    """
    bracketed = [value for value in values if "[" in value or "{" in value]
    counts = Counter()
    if not bracketed:
        return counts
    for cell in _fetch_bounded(
        fetch,
        "SELECT id, val, value_infos FROM ("
        "SELECT id, value AS val, value_infos::text AS value_infos, "
        "%(ascii_forms)s::jsonb ->> value AS ascii_form, "
        f"{CHOICE_DOCUMENT_SQL} AS document {rows_sql}"
        # OFFSET 0 keeps PostgreSQL from inlining ``document`` into each arm
        # of the predicate, which would unwrap every cell's metadata per arm.
        "AND value = ANY(%(literal_values)s::text[]) OFFSET 0"
        f") AS cells WHERE {LITERAL_CANDIDATE_SQL}",
        {
            **params,
            "literal_values": bracketed,
            "ascii_forms": json.dumps(
                {value: json.dumps(value) for value in bracketed}
            ),
        },
        size="octet_length(val) + octet_length(value_infos)",
        order="id",
        max_bytes=max_bytes,
    ):
        if literal_choice(cell["val"], cell["value_infos"]):
            counts[cell["val"]] += 1
    return counts
