"""Malformed scoring configuration must not break dashboard reads."""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import connection

from simulate.models import CallExecution
from simulate.services.run_results_v3 import call_outcome, eval_rows
from simulate.services.run_results_v3_queries import (
    _configured_eval_verdict,
    run_calls_queryset,
)
from simulate.services.run_results_v3_scoring import (
    judge_stored_eval,
    resolve_eval_scoring_spec,
)


def _config(runtime=None, template_threshold=0.5):
    return SimpleNamespace(
        id="eval-1",
        config=runtime or {},
        eval_template=SimpleNamespace(
            output_type_normalized="percentage",
            config={},
            choice_scores={},
            pass_threshold=template_threshold,
        ),
    )


@pytest.mark.parametrize(
    "threshold",
    ["bad", "", [], {}, True, False, float("nan"), float("inf"), -0.1, 1.1],
)
@pytest.mark.parametrize("source", ["nested", "runtime", "template"])
def test_invalid_threshold_is_unmeasured_for_rows_and_sql(threshold, source):
    config = _config()
    if source == "nested":
        config.config = {"run_config": {"pass_threshold": threshold}}
    elif source == "runtime":
        config.config = {"pass_threshold": threshold}
    else:
        config.eval_template.pass_threshold = threshold

    spec = resolve_eval_scoring_spec(config)
    assert spec.threshold is None
    for output in [0.9, "Passed", "Failed", {"failure": False}]:
        judgement = judge_stored_eval({"status": "completed", "output": output}, spec)
        assert judgement.outcome is None
        assert judgement.score is None

    score, passed, failed = _configured_eval_verdict("eval-1", config)
    query = CallExecution.objects.all().query
    compiler = query.get_compiler(connection=connection)
    sql, params = compiler.compile(score.resolve_expression(query))
    assert sql == "(NULL)::double precision"
    assert not params
    assert passed.children == [("pk__in", [])]
    assert failed.children == [("pk__in", [])]


@pytest.mark.parametrize(
    "runtime,template_threshold,expected",
    [
        ({"run_config": {"pass_threshold": 0}}, 0.5, 0.0),
        ({"pass_threshold": 1}, 0.5, 1.0),
        ({"pass_threshold": "0.7"}, 0.5, 0.7),
        ({"run_config": {"pass_threshold": None}, "pass_threshold": 0.2}, 0.5, 0.2),
        ({"pass_threshold": None}, 0.8, 0.8),
        ({}, None, 0.5),
        ({"run_config": {"pass_threshold": 0.7}, "pass_threshold": True}, False, 0.7),
        ({"pass_threshold": 0.7}, "bad", 0.7),
    ],
)
def test_valid_threshold_priority_and_fallback_are_preserved(
    runtime, template_threshold, expected
):
    assert (
        resolve_eval_scoring_spec(_config(runtime, template_threshold)).threshold
        == expected
    )


def test_invalid_threshold_preserves_execution_errors():
    spec = resolve_eval_scoring_spec(_config({"pass_threshold": "bad"}))
    assert (
        judge_stored_eval({"status": "error", "output": None}, spec).outcome == "error"
    )


def test_malformed_binding_does_not_discard_valid_evaluations():
    invalid = _config({"pass_threshold": "bad"})
    valid = _config({"pass_threshold": 0.7})
    valid.id = "eval-2"
    call = SimpleNamespace(
        status=CallExecution.CallStatus.COMPLETED,
        call_metadata={},
        eval_outputs={
            "eval-1": {"status": "completed", "output": 0.1},
            "eval-2": {"status": "completed", "output": 0.9},
        },
    )
    configs = {"eval-1": invalid, "eval-2": valid}
    assert call_outcome(call, configs) == "passed"
    rows = {row["id"]: row for row in eval_rows(call, configs)}
    assert rows["eval-1"]["score"] is None
    assert rows["eval-1"]["passed"] is None
    assert rows["eval-2"]["score"] == 0.9
    assert rows["eval-2"]["passed"] is True
    with patch(
        "simulate.services.run_results_v3_queries.SimulateEvalConfig.objects.filter"
    ) as filtered:
        filtered.return_value.select_related.return_value = [invalid, valid]
        queryset = run_calls_queryset(SimpleNamespace(run_test=None), [uuid4()])
    sql = str(queryset.query)
    assert "result_outcome" in sql
