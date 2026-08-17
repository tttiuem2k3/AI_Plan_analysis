from __future__ import annotations


class OptimizerError(Exception):
    """Base error for production optimizer."""


class InputValidationError(OptimizerError):
    """Raised when input schema/business validation fails."""


class SolveError(OptimizerError):
    """Raised when CP-SAT model cannot be solved as expected."""
