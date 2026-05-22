"""log-mcp-server 自定义异常类型。"""
from typing import Any, Dict, Optional


class LogMCPError(Exception):
    """log-mcp-server 所有自定义异常的基类。"""

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
    """连接日志后端失败。"""


class BackendHTTPError(LogMCPError):
    """日志后端返回 HTTP 错误。"""

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
    """日志后端鉴权 / 授权失败。"""


class BackendQueryError(LogMCPError):
    """日志后端执行查询失败。"""


class ConfigError(LogMCPError):
    """配置错误。"""


class ValidationError(LogMCPError):
    """工具入参 / 参数校验错误。"""
