"""Framework-neutral tool definitions and implementations."""

from sandboxed_goose.tools.calculator import (
    CALCULATE,
    CalculationError,
    evaluate_expression,
    render_calculation,
)
from sandboxed_goose.tools.definition import ToolDefinition
from sandboxed_goose.tools.status import (
    SANDBOX_STATUS,
    render_sandbox_status,
)

TOOL_DEFINITIONS = (SANDBOX_STATUS, CALCULATE)

__all__ = [
    "CALCULATE",
    "SANDBOX_STATUS",
    "TOOL_DEFINITIONS",
    "CalculationError",
    "ToolDefinition",
    "evaluate_expression",
    "render_calculation",
    "render_sandbox_status",
]
