"""A code eval this install cannot run says how to fix the setup.

Standalone's built-in sandbox has no Node.js, so a JavaScript eval fails with
the code-executor's hint to turn on the `sandbox` profile. Dataset cells show
it; the eval playground and "Test Evaluation" must show it too instead of their
generic retry message, while other code eval failures stay generic there.

urlopen is replaced at the urllib boundary with the code-executor's answer.
"""

import json
import urllib.request
from unittest import mock

import pytest
from rest_framework import status

from agentic_eval.core_evals.fi_evals.function.functions import custom_code_eval
from agentic_eval.core_evals.fi_utils import sandbox
from agentic_eval.core_evals.fi_utils.exceptions import CodeEvalSetupError
from model_hub.models.choices import OwnerChoices
from model_hub.models.evals_metric import EvalTemplate

JS_CODE = "function evaluate(output, expected) { return output === expected; }\n"
# code-executor/server.py's answer to a JavaScript eval without Node.js.
NO_NODE_MESSAGE = (
    "JavaScript evals need Node.js, which this sandbox does not have. In "
    "Standalone, set COMPOSE_PROFILES=sandbox in .env and run "
    "docker compose up -d: the nsjail sandbox it adds runs them."
)
NO_NODE_RESULT = {
    "status": "error",
    "data": NO_NODE_MESSAGE,
    "setup_error": True,
    "execution_time": 0.001,
}
PRIVATE_ERROR = "Sandbox error: /app/backend/secret-path"


@pytest.fixture
def executor_answers(monkeypatch, settings):
    settings.CLOUD_DEPLOYMENT = ""
    settings.CODE_EXECUTOR_LOCAL_FALLBACK = False
    opener = mock.MagicMock()
    monkeypatch.setattr(urllib.request, "urlopen", opener)

    def answer(result: dict):
        body = json.dumps(result).encode()
        response = opener.return_value.__enter__.return_value
        response.read.side_effect = lambda amt=-1: body if amt < 0 else body[:amt]

    return answer


@pytest.mark.parametrize(
    "result, message",
    [
        (NO_NODE_RESULT, NO_NODE_MESSAGE),
        (
            {
                "status": "error",
                "data": sandbox.EXECUTOR_UNAVAILABLE_MESSAGE,
                "setup_error": True,
            },
            sandbox.EXECUTOR_UNAVAILABLE_MESSAGE,
        ),
    ],
)
def test_a_setup_error_keeps_the_sandbox_message(result, message):
    with mock.patch(
        "agentic_eval.core_evals.fi_evals.function.functions.CodeExecution.execute",
        return_value=result,
    ):
        with pytest.raises(CodeEvalSetupError) as raised:
            custom_code_eval(JS_CODE, language="javascript", output="a")

    assert str(raised.value) == message


def test_other_code_eval_errors_are_not_setup_errors():
    with mock.patch(
        "agentic_eval.core_evals.fi_evals.function.functions.CodeExecution.execute",
        return_value={"status": "error", "data": PRIVATE_ERROR},
    ):
        with pytest.raises(ValueError) as raised:
            custom_code_eval(JS_CODE, language="javascript", output="a")

    assert not isinstance(raised.value, CodeEvalSetupError)


def _js_code_eval(user, workspace):
    return EvalTemplate.no_workspace_objects.create(
        name="js-setup-error",
        organization=user.organization,
        workspace=workspace,
        owner=OwnerChoices.USER.value,
        eval_type="code",
        config={
            "code": JS_CODE,
            "output": "Pass/Fail",
            "eval_type_id": "CustomCodeEval",
            "required_keys": ["output", "expected"],
        },
        visible_ui=True,
        output_type_normalized="pass_fail",
        pass_threshold=0.5,
    )


def _playground(auth_client, template):
    return auth_client.post(
        "/model-hub/eval-playground/",
        {
            "template_id": str(template.id),
            "model": "",
            "mapping": {"output": "same", "expected": "same"},
            "config": {"params": {}},
        },
        format="json",
    )


def _test_evaluation(auth_client, template):
    return auth_client.post(
        "/model-hub/test-evaluation/",
        {
            "name": "js-setup-error-test",
            "template_type": "Function",
            "template_id": str(template.id),
            "eval_type_id": "CustomCodeEval",
            "model": "",
            "output_type": "Pass/Fail",
            "required_keys": ["output", "expected"],
            "input_data_types": {"output": "text", "expected": "text"},
            "config": {
                "mapping": {"output": "same", "expected": "same"},
                "config": {"code": JS_CODE},
            },
        },
        format="json",
    )


@pytest.mark.django_db
@pytest.mark.parametrize("call", [_playground, _test_evaluation])
def test_javascript_eval_without_node_shows_the_sandbox_hint(
    call, auth_client, user, workspace, executor_answers
):
    executor_answers(NO_NODE_RESULT)

    response = call(auth_client, _js_code_eval(user, workspace))

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert NO_NODE_MESSAGE in str(response.data)
    assert "Please retry" not in str(response.data)


@pytest.mark.django_db
@pytest.mark.parametrize("call", [_playground, _test_evaluation])
def test_other_code_eval_failures_stay_generic(
    call, auth_client, user, workspace, executor_answers
):
    executor_answers({"status": "error", "data": PRIVATE_ERROR, "execution_time": 0})

    response = call(auth_client, _js_code_eval(user, workspace))

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert PRIVATE_ERROR not in str(response.data)
    assert "could not be completed" in str(response.data)
