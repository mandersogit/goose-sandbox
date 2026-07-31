import builtins
import json

import pytest

from sandboxed_goose.tools import CalculationError, evaluate_expression, render_calculation


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        pytest.param("1 + 2 * 3", 7, id="precedence"),
        pytest.param("(12 + 8) * 3 / 4", 15.0, id="parentheses"),
        pytest.param("-5 // 2", -3, id="unary-and-floor-division"),
        pytest.param("17 % 5", 2, id="modulo"),
        pytest.param("2**10 + 7", 1031, id="power"),
        pytest.param("2 ** -3", 0.125, id="negative-power"),
    ],
)
def test_evaluate_expression_supports_basic_arithmetic(
    expression: str,
    expected: int | float,
) -> None:
    assert evaluate_expression(expression) == expected


def test_render_calculation_returns_stable_json() -> None:
    assert json.loads(render_calculation("(6 + 4) * 2")) == {
        "expression": "(6 + 4) * 2",
        "result": 20,
    }


@pytest.mark.parametrize(
    "expression",
    [
        pytest.param("", id="empty"),
        pytest.param("1 / 0", id="division-by-zero"),
        pytest.param("2 ** 101", id="oversized-exponent"),
        pytest.param("10**100 * 10", id="oversized-result"),
        pytest.param("__import__('os').system('echo unsafe')", id="function-call"),
        pytest.param("(1).__class__", id="attribute-access"),
        pytest.param("[1, 2][0]", id="subscript"),
        pytest.param("sum([1, 2])", id="name-and-call"),
        pytest.param("True + 1", id="boolean"),
        pytest.param("1 << 2", id="unsupported-operator"),
        pytest.param(" + ".join(["1"] * 40), id="too-complex"),
        pytest.param("1" * 201, id="too-long"),
    ],
)
def test_evaluate_expression_rejects_unsafe_or_unbounded_input(expression: str) -> None:
    with pytest.raises(CalculationError):
        evaluate_expression(expression)


def test_evaluator_does_not_use_eval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_eval(*args: object, **kwargs: object) -> None:
        raise AssertionError("eval must not be called")

    monkeypatch.setattr(builtins, "eval", reject_eval)

    assert evaluate_expression("(2 + 3) * 4") == 20
