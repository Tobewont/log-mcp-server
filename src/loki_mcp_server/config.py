"""
Configuration management for Loki MCP Server.
"""
import os
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

import structlog

logger = structlog.get_logger(__name__)


class LokiConfig(BaseSettings):
    """Configuration for Loki MCP Server."""
    
    model_config = SettingsConfigDict(
        env_prefix="LOKI_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    # FastMCP configuration (optional, FastMCP handles mode detection automatically)
    fastmcp_debug: bool = Field(
        default=False,
        description="Enable FastMCP debug mode"
    )
    fastmcp_host: str = Field(
        default="127.0.0.1",
        description="Host address for FastMCP HTTP server (when running in HTTP mode)"
    )
    fastmcp_port: int = Field(
        default=8000,
        description="Port for FastMCP HTTP server (when running in HTTP mode)"
    )
    
    # Loki server configuration
    addr: str = Field(
        default="http://localhost:3100",
        description="Loki server address (e.g., http://localhost:3100)"
    )
    
    # Multi-tenant configuration
    tenants: str = Field(
        default="fake",
        description="Tenant IDs separated by | (e.g., 'tenant1|tenant2|tenant3'). Use 'fake' for single-tenant mode."
    )
    
    # Authentication
    username: Optional[str] = Field(
        default=None,
        description="Username for basic authentication"
    )
    password: Optional[str] = Field(
        default=None,
        description="Password for basic authentication"
    )
    bearer_token: Optional[str] = Field(
        default=None,
        description="Bearer token for authentication"
    )
    bearer_token_file: Optional[str] = Field(
        default=None,
        description="Path to file containing bearer token"
    )
    
    
    # TLS configuration
    ca_file: Optional[str] = Field(
        default=None,
        description="Path to CA certificate file"
    )
    cert_file: Optional[str] = Field(
        default=None,
        description="Path to client certificate file"
    )
    key_file: Optional[str] = Field(
        default=None,
        description="Path to client private key file"
    )
    tls_skip_verify: bool = Field(
        default=False,
        description="Skip TLS certificate verification"
    )
    
    # HTTP client timeouts (in seconds)
    connect_timeout: float = Field(
        default=10.0,
        description="Connection timeout in seconds"
    )
    read_timeout: float = Field(
        default=30.0,
        description="Read timeout in seconds"
    )
    write_timeout: float = Field(
        default=10.0,
        description="Write timeout in seconds"
    )
    pool_timeout: float = Field(
        default=10.0,
        description="Connection pool timeout in seconds"
    )
    
    # Query limits
    default_limit: int = Field(
        default=100,
        description="Default limit for query results"
    )
    max_limit: int = Field(
        default=5000,
        description="Maximum limit for query results"
    )
    
    # Default time range settings
    default_time_range_minutes: int = Field(
        default=30,
        description="Default time range in minutes when start/end not specified"
    )
    
    # Timezone settings
    timezone: str = Field(
        default="Asia/Shanghai",
        description="Default timezone for time operations"
    )
    
    @field_validator("addr")
    @classmethod
    def validate_addr(cls, v: str) -> str:
        """Validate Loki server address."""
        if not v:
            raise ValueError("Loki server address cannot be empty")
        if not v.startswith(("http://", "https://")):
            raise ValueError("Loki server address must start with http:// or https://")
        return v.rstrip("/")
    
    @field_validator("default_limit", "max_limit", "default_time_range_minutes")
    @classmethod
    def validate_limits(cls, v: int) -> int:
        """Validate query limits and time range."""
        if v <= 0:
            raise ValueError("Query limits and time range must be positive integers")
        return v
    
    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        """Validate timezone string."""
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(v)
        except Exception:
            # Fallback for older Python versions or invalid timezone
            try:
                import pytz
                pytz.timezone(v)
            except Exception:
                raise ValueError(f"Invalid timezone: {v}")
        return v
    
    @field_validator("fastmcp_port")
    @classmethod
    def validate_fastmcp_port(cls, v: int) -> int:
        """Validate FastMCP server port."""
        if not (1 <= v <= 65535):
            raise ValueError("FastMCP server port must be between 1 and 65535")
        return v
    
    @field_validator("fastmcp_host")
    @classmethod
    def validate_fastmcp_host(cls, v: str) -> str:
        """Validate FastMCP server host."""
        if not v.strip():
            raise ValueError("FastMCP server host cannot be empty")
        return v.strip()
    
    def __init__(self, **kwargs):
        """Initialize configuration with environment variables and config files."""
        # Load from config files first (lower priority)
        config_data = self._load_config_files()
        
        # Load FastMCP configuration from FASTMCP_ environment variables
        fastmcp_config = self._load_fastmcp_config_from_env()
        config_data.update(fastmcp_config)
        
        # Merge with provided kwargs (highest priority)
        config_data.update(kwargs)
        
        # Initialize with merged data
        super().__init__(**config_data)
        
        # Load bearer token from file if specified
        self._load_bearer_token_from_file()
        
        logger.info(
                "Configuration loaded",
                addr=self.addr,
                fastmcp_debug=self.fastmcp_debug,
                fastmcp_host=self.fastmcp_host,
                fastmcp_port=self.fastmcp_port,
                tenants=self.tenants,
                has_username=bool(self.username),
                has_password=bool(self.password),
                has_bearer_token=bool(self.bearer_token),
                tls_skip_verify=self.tls_skip_verify,
            )
    
    def _load_config_files(self) -> dict:
        """Load configuration from YAML files."""
        config_data = {}
        
        # Configuration file paths (in order of priority)
        config_paths = [
            os.environ.get("LOKI_CONFIG_PATH"),  # Custom path from env
            Path.cwd() / "loki-config.yaml",    # Current directory
            Path.cwd() / ".loki-config.yaml",   # Hidden file in current directory
            Path.home() / ".loki-config.yaml",  # Home directory
        ]
        
        for config_path in config_paths:
            if config_path and Path(config_path).exists():
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        file_config = yaml.safe_load(f) or {}
                    
                    # Convert keys to match pydantic field names
                    normalized_config = {}
                    for key, value in file_config.items():
                        # Remove LOKI_ prefix if present and convert to lowercase
                        normalized_key = key.lower()
                        if normalized_key.startswith("loki_"):
                            normalized_key = normalized_key[5:]
                        normalized_config[normalized_key] = value
                    
                    config_data.update(normalized_config)
                    logger.info("Configuration file loaded", path=str(config_path))
                    break
                    
                except Exception as e:
                    logger.warning(
                        "Failed to load configuration file",
                        path=str(config_path),
                        error=str(e),
                    )
        
        return config_data
    
    def _load_fastmcp_config_from_env(self) -> dict:
        """Load FastMCP configuration from FASTMCP_ environment variables."""
        fastmcp_config = {}
        
        # Map FASTMCP environment variables to config fields
        env_mappings = {
            "FASTMCP_DEBUG": "fastmcp_debug",
            "FASTMCP_HOST": "fastmcp_host", 
            "FASTMCP_PORT": "fastmcp_port",
        }
        
        for env_var, config_key in env_mappings.items():
            value = os.environ.get(env_var)
            if value is not None:
                # Convert port to integer
                if config_key == "fastmcp_port":
                    try:
                        value = int(value)
                    except ValueError:
                        logger.warning(
                            "Invalid port value in environment variable",
                            env_var=env_var,
                            value=value
                        )
                        continue
                # Convert debug to boolean
                elif config_key == "fastmcp_debug":
                    value = value.lower() in ("true", "1", "yes", "on")
                
                fastmcp_config[config_key] = value
                logger.debug("FastMCP config loaded from environment", env_var=env_var, value=value)
        
        return fastmcp_config
    
    def _load_bearer_token_from_file(self) -> None:
        """Load bearer token from file if specified."""
        if self.bearer_token_file and not self.bearer_token:
            try:
                token_path = Path(self.bearer_token_file)
                if token_path.exists():
                    self.bearer_token = token_path.read_text(encoding="utf-8").strip()
                    logger.info("Bearer token loaded from file", path=str(token_path))
                else:
                    logger.warning("Bearer token file not found", path=str(token_path))
            except Exception as e:
                logger.warning(
                    "Failed to load bearer token from file",
                    path=self.bearer_token_file,
                    error=str(e),
                )
    
    def get_tenant_list(self) -> List[str]:
        """Get list of configured tenants."""
        if not self.tenants:
            return ["fake"]
        
        # Split by | and clean up whitespace
        tenant_list = [tenant.strip() for tenant in self.tenants.split("|") if tenant.strip()]
        
        # If empty after cleaning, return default
        if not tenant_list:
            return ["fake"]
        
        return tenant_list
    
    def get_safe_config(self) -> dict:
        """Get configuration dict with sensitive data redacted."""
        config_dict = self.model_dump()
        
        # Redact sensitive fields
        sensitive_fields = ["password", "bearer_token"]
        for field in sensitive_fields:
            if config_dict.get(field):
                config_dict[field] = "[REDACTED]"
        
        return config_dict
