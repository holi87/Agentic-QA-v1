"""Typed errors mapped to CLI exit codes."""
from __future__ import annotations


class AgenticOSError(Exception):
    exit_code: int = 2


class UsageError(AgenticOSError):
    exit_code = 64

    def __init__(self, message: str):
        super().__init__(message)


class ConfigError(AgenticOSError):
    exit_code = 2


class InfraError(AgenticOSError):
    exit_code = 2


class BudgetExceededError(InfraError):
    pass


class ProductFailure(AgenticOSError):
    exit_code = 1


class UserAbort(AgenticOSError):
    exit_code = 130
