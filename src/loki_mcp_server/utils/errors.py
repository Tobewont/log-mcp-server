"""
Error handling utilities for Loki MCP Server.
"""
from typing import Any, Dict, Optional


class LokiMCPError(Exception):
    """Base exception for Loki MCP Server errors."""
    
    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        """Initialize error with message, code, and details."""
        super().__init__(message)
        self.message = message
        self.error_code = error_code or self.__class__.__name__
        self.details = details or {}
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert error to dictionary for JSON serialization."""
        return {
            "error": self.error_code,
            "message": self.message,
            "details": self.details,
        }


class LokiConnectionError(LokiMCPError):
    """Error connecting to Loki server."""
    pass


class LokiHTTPError(LokiMCPError):
    """HTTP error from Loki API."""
    
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_text: Optional[str] = None,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        """Initialize HTTP error with status code and response."""
        super().__init__(message, error_code, details)
        self.status_code = status_code
        self.response_text = response_text
        
        # Add HTTP details to error details
        if status_code is not None:
            self.details["status_code"] = status_code
        if response_text:
            self.details["response_text"] = response_text


class LokiAuthError(LokiMCPError):
    """Authentication/authorization error."""
    pass


class LokiQueryError(LokiMCPError):
    """Error in Loki query execution."""
    pass


class LokiConfigError(LokiMCPError):
    """Configuration error."""
    pass


class LokiValidationError(LokiMCPError):
    """Input validation error."""
    pass


def create_mcp_error_response(
    error: Exception,
    context: Optional[str] = None,
) -> Dict[str, Any]:
    """Create standardized MCP error response."""
    if isinstance(error, LokiMCPError):
        error_dict = error.to_dict()
    else:
        error_dict = {
            "error": "UnexpectedError",
            "message": str(error),
            "details": {"type": type(error).__name__},
        }
    
    if context:
        error_dict["context"] = context
    
    return {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": f"Error: {error_dict['message']}",
            }
        ],
        "_meta": error_dict,
    }
