"""Guards the walker's ability to resolve the `scenario_columns.<name>.value`
mapping path that the FE dropdown offers. Prompt simulations save eval
mapping values in this dot-path form, so a regression here would surface as
"Column mapping mismatch" errors in the evaluation table (TH-6904).
"""

from simulate.temporal.activities.xl import (
    PATH_MISSING,
    _build_scenario_columns_subject,
    _build_scenario_graph_subject,
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


def test_build_scenario_columns_subject_returns_empty_when_no_row():
    class _Call:
        call_metadata = {}

    assert _build_scenario_columns_subject(_Call()) == {}


def test_build_scenario_columns_subject_returns_empty_when_call_metadata_is_none():
    class _Call:
        call_metadata = None

    assert _build_scenario_columns_subject(_Call()) == {}


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


def test_build_scenario_graph_subject_returns_empty_when_no_scenario_id():
    class _Call:
        scenario_id = None

    assert _build_scenario_graph_subject(_Call()) == {}
