from __future__ import annotations

import pytest

from cua.core.templating import TemplateError, env_secrets, is_pure_placeholder, render


def test_render_inputs_and_secrets() -> None:
    secrets = env_secrets({"CU_CORE_PASSWORD": "pw", "CU_CORE_USERNAME": "svc"})
    assert render("{{ inputs.member_id }}", {"member_id": "10042"}) == "10042"
    assert (
        render("{{secrets.cu_core.username}}:{{secrets.cu_core.password}}", {}, secrets) == "svc:pw"
    )
    assert render("no placeholders", {}) == "no placeholders"


@pytest.mark.parametrize(
    ("text", "inputs"),
    [
        ("{{inputs.account}}", {"member_id": "1"}),
        ("{{env.HOME}}", {}),
        ("{{secrets.cu_core.password}}", {}),
    ],
)
def test_render_errors(text: str, inputs: dict[str, str]) -> None:
    with pytest.raises(TemplateError):
        render(text, inputs)


def test_missing_secret_names_the_env_var() -> None:
    with pytest.raises(TemplateError, match="CU_CORE_PASSWORD"):
        render("{{secrets.cu_core.password}}", {}, env_secrets({}))


def test_is_pure_placeholder() -> None:
    assert is_pure_placeholder(" {{inputs.member_id}} ")
    assert not is_pure_placeholder("ID {{inputs.member_id}}")
