"""Guards the walker's ability to resolve the `scenario_columns.<name>.value`
mapping path that the FE dropdown offers. Prompt simulations save eval
mapping values in this dot-path form, so a regression here would surface as
"Column mapping mismatch" errors in the evaluation table (TH-6904).
"""

from simulate.temporal.activities.xl import (
    PATH_MISSING,
    walk_subject_path,
)


def _subjects_with(scenario_columns=None, scenario_graph=None):
    subjects = {
        "call": object(),
        "agent": None,
        "agent_version": None,
        "persona": None,
        "prompt": None,
        "scenario": None,
        "simulation": None,
    }
    if scenario_columns is not None:
        subjects["scenario_columns"] = scenario_columns
    if scenario_graph is not None:
        subjects["scenario_graph"] = scenario_graph
    return subjects


def test_walker_resolves_scenario_columns_dot_path():
    subjects = _subjects_with(
        {
            "Ideal Outcome": {
                "value": "the golden path",
                "column_name": "Ideal Outcome",
                "dataset_column_id": "col-uuid-1",
            }
        }
    )
    assert (
        walk_subject_path(subjects, "scenario_columns.Ideal Outcome.value")
        == "the golden path"
    )


def test_walker_resolves_column_name_and_dataset_column_id_subpaths():
    subjects = _subjects_with(
        {"persona": {"value": "Sarah", "column_name": "persona", "dataset_column_id": "u"}}
    )
    assert walk_subject_path(subjects, "scenario_columns.persona.column_name") == "persona"
    assert (
        walk_subject_path(subjects, "scenario_columns.persona.dataset_column_id") == "u"
    )


def test_walker_returns_none_for_unknown_column():
    subjects = _subjects_with({"Ideal Outcome": {"value": "x"}})
    # walker returns None (not PATH_MISSING) for a missing intermediate segment,
    # which the caller stringifies to empty
    assert walk_subject_path(subjects, "scenario_columns.NoSuchColumn.value") is None


def test_walker_returns_none_when_scenario_columns_dict_is_empty():
    # head matches, sub-segment misses -> None (walker's contract for a
    # matched head with a missing intermediate), which the caller stringifies
    # to an empty cell value rather than a "column mismatch" error.
    subjects = _subjects_with({})
    assert walk_subject_path(subjects, "scenario_columns.anything.value") is None


def test_walker_returns_path_missing_when_scenario_columns_key_absent():
    # No scenario_columns key at all -> PATH_MISSING, so the caller falls
    # through to the existing "Column mismatch" error branch.
    subjects = {
        "call": object(),
        "agent": None,
        "agent_version": None,
        "persona": None,
        "prompt": None,
        "scenario": None,
        "simulation": None,
    }
    assert (
        walk_subject_path(subjects, "scenario_columns.anything.value") is PATH_MISSING
    )


def test_walker_resolves_scenario_graph_node_path():
    subjects = _subjects_with(
        scenario_graph={
            "nodes": [{"id": "n1", "type": "intent", "data": {"label": "Greet"}}],
            "edges": [{"source": "n1", "target": "n2"}],
        }
    )
    assert (
        walk_subject_path(subjects, "scenario_graph.nodes.0.type") == "intent"
    )
    assert (
        walk_subject_path(subjects, "scenario_graph.nodes.0.data.label") == "Greet"
    )
    assert (
        walk_subject_path(subjects, "scenario_graph.edges.0.source") == "n1"
    )


def test_walker_returns_none_for_unknown_scenario_graph_path():
    subjects = _subjects_with(scenario_graph={"nodes": []})
    assert walk_subject_path(subjects, "scenario_graph.nodes.99.type") is None


def test_walker_returns_none_when_scenario_graph_dict_is_empty():
    subjects = _subjects_with(scenario_graph={})
    assert walk_subject_path(subjects, "scenario_graph.nodes.0.type") is None


def test_walker_returns_path_missing_when_scenario_graph_key_absent():
    subjects = _subjects_with(scenario_columns={})
    assert (
        walk_subject_path(subjects, "scenario_graph.nodes.0.type") is PATH_MISSING
    )


class _CallStub:
    """Stand-in for a CallExecution row exposing the top-level attrs the
    walker's bare-head fall-through has to resolve."""

    call_type = "Inbound"
    duration = 42
    overall_score = 0.87
    audio_url = None
    avg_agent_latency_ms = 850


def _subjects_with_call(call):
    return {
        "call": call,
        "agent": None,
        "agent_version": None,
        "persona": None,
        "prompt": None,
        "scenario": None,
        "simulation": None,
    }


def test_walker_resolves_bare_head_via_call_subject():
    subjects = _subjects_with_call(_CallStub())
    assert walk_subject_path(subjects, "call_type") == "Inbound"
    assert walk_subject_path(subjects, "duration") == 42
    assert walk_subject_path(subjects, "overall_score") == 0.87
    assert walk_subject_path(subjects, "avg_agent_latency_ms") == 850


def test_walker_returns_none_for_bare_head_attr_with_none_value():
    subjects = _subjects_with_call(_CallStub())
    assert walk_subject_path(subjects, "audio_url") is None


def test_walker_returns_path_missing_for_bare_head_not_on_any_subject():
    subjects = _subjects_with_call(_CallStub())
    assert walk_subject_path(subjects, "totally_made_up_field") is PATH_MISSING


def test_walker_returns_path_missing_for_empty_string():
    subjects = _subjects_with_call(_CallStub())
    assert walk_subject_path(subjects, "") is PATH_MISSING
