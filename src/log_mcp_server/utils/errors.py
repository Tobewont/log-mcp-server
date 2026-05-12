"""Error types for the log MCP server."""
from typing import Any, Dict, Optional


class LogMCPError(Exception):
    """Base exception for all log MCP server errors."""

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code or self.__class__.__name__
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": self.error_code,
            "message": self.message,
            "details": self.details,
        }


class BackendConnectionError(LogMCPError):
    """Failed to connect to the log backend."""


class BackendHTTPError(LogMCPError):
    """HTTP error from the log backend."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_text: Optional[str] = None,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message, error_code, details)
        self.status_code = status_code
        self.response_text = response_text
        if status_code is not None:
            self.details["status_code"] = status_code
        if response_text:
            self.details["response_text"] = response_text


class BackendAuthError(LogMCPError):
    """Authentication or authorization error against backend."""


class BackendQueryError(LogMCPError):
    """Backend query execution error."""


class ConfigError(LogMCPError):
    """Configuration error."""


class ValidationError(LogMCPError):
    """Input validation error from a tool/argument."""
