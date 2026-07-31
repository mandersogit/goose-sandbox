"""A small, safe arithmetic evaluator shared by both MCP adapters."""

from __future__ import annotations

import ast
import json
import math
from typing import TypeAlias

from sandboxed_goose.tools.definition import ToolDefinition

Number: TypeAlias = int | float

_MAX_EXPRESSION_LENGTH = 200
_MAX_AST_NODES = 64
_MAX_ABSOLUTE_VALUE = 10**100
_MAX_ABSOLUTE_EXPONENT = 100

CALCULATE = ToolDefinition(
    name="calculate",
    description=(
        "Evaluate basic arithmetic with parentheses and the +, -, *, /, //, %, and ** operators."
    ),
)


class CalculationError(ValueError):
    """Raised when an expression is invalid, unsafe, or outside calculator limits."""


def _checked_number(value: object) -> Number:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalculationError("Only real numeric literals and arithmetic operators are allowed.")
    if isinstance(value, float) and not math.isfinite(value):
        raise CalculationError("Numbers and results must be finite.")
    if abs(value) > _MAX_ABSOLUTE_VALUE:
        raise CalculationError("Numbers and results must not exceed 1e100 in magnitude.")
    return value


def _evaluate_node(node: ast.AST) -> Number:
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body)

    if isinstance(node, ast.Constant):
        return _checked_number(node.value)

    if isinstance(node, ast.UnaryOp):
        operand = _evaluate_node(node.operand)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.USub):
            return _checked_number(-operand)
        raise CalculationError("Only unary + and - operators are allowed.")

    if not isinstance(node, ast.BinOp):
        raise CalculationError("Only real numeric literals and arithmetic operators are allowed.")

    left = _evaluate_node(node.left)
    right = _evaluate_node(node.right)

    try:
        if isinstance(node.op, ast.Add):
            result: object = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = left * right
        elif isinstance(node.op, ast.Div):
            result = left / right
        elif isinstance(node.op, ast.FloorDiv):
            result = left // right
        elif isinstance(node.op, ast.Mod):
            result = left % right
        elif isinstance(node.op, ast.Pow):
            if abs(right) > _MAX_ABSOLUTE_EXPONENT:
                raise CalculationError("Exponents must not exceed 100 in magnitude.")
            result = left**right
        else:
            raise CalculationError("That arithmetic operator is not supported.")
    except ZeroDivisionError as error:
        raise CalculationError("Division by zero is not allowed.") from error
    except OverflowError as error:
        raise CalculationError("The result is too large.") from error

    return _checked_number(result)


def evaluate_expression(expression: str) -> Number:
    """Evaluate one bounded arithmetic expression without executing Python code."""
    normalized = expression.strip()
    if not normalized:
        raise CalculationError("Expression must not be empty.")
    if len(normalized) > _MAX_EXPRESSION_LENGTH:
        raise CalculationError("Expression must not exceed 200 characters.")

    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as error:
        raise CalculationError("Expression is not valid arithmetic.") from error

    if sum(1 for _node in ast.walk(tree)) > _MAX_AST_NODES:
        raise CalculationError("Expression is too complex.")

    return _evaluate_node(tree)


def render_calculation(expression: str) -> str:
    """Render the calculator response returned by both MCP implementations."""
    return json.dumps(
        {
            "expression": expression,
            "result": evaluate_expression(expression),
        },
        sort_keys=True,
    )
