"""FastMCP 工具模块（与具体后端无关）。"""

from .log_tools import initialize_tools, register_tools

__all__ = ["initialize_tools", "register_tools"]
