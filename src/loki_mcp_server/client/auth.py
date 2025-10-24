"""
Authentication utilities for Loki API.
"""
import base64
from typing import Dict, Optional

import structlog

from ..config import LokiConfig
from ..utils.errors import LokiAuthError

logger = structlog.get_logger(__name__)


class LokiAuth:
    """Authentication handler for Loki API requests."""
    
    def __init__(self, config: LokiConfig):
        """Initialize authentication with configuration."""
        self.config = config
        self._validate_auth_config()
    
    def _validate_auth_config(self) -> None:
        """Validate authentication configuration."""
        has_basic_auth = bool(self.config.username and self.config.password)
        has_bearer_token = bool(self.config.bearer_token)
        
        if has_basic_auth and has_bearer_token:
            logger.warning(
                "Both basic auth and bearer token configured, "
                "bearer token will take precedence"
            )
        
        if not has_basic_auth and not has_bearer_token:
            logger.info("No authentication configured, using anonymous access")
    
    def get_auth_headers(self, tenant: Optional[str] = None) -> Dict[str, str]:
        """Get authentication headers for requests."""
        headers = {}
        
        # Add authentication headers
        if self.config.bearer_token:
            headers["Authorization"] = f"Bearer {self.config.bearer_token}"
            logger.debug("Using bearer token authentication")
        elif self.config.username and self.config.password:
            auth_string = f"{self.config.username}:{self.config.password}"
            auth_bytes = auth_string.encode("utf-8")
            auth_b64 = base64.b64encode(auth_bytes).decode("ascii")
            headers["Authorization"] = f"Basic {auth_b64}"
            logger.debug("Using basic authentication", username=self.config.username)
        
        # Add tenant/organization headers
        if tenant:
            headers["X-Scope-OrgID"] = tenant
            logger.debug("Using tenant scope", tenant=tenant)
        elif self.config.org_id:
            headers["X-Org-ID"] = self.config.org_id
            logger.debug("Using organization ID", org_id=self.config.org_id)
        
        return headers
    
    def validate_tenant_access(self, tenant: str) -> None:
        """Validate tenant access (placeholder for future ACL implementation)."""
        if not tenant:
            raise LokiAuthError("Tenant cannot be empty")
        
        # TODO: Implement actual tenant access validation
        # This could check against a list of allowed tenants,
        # validate tenant format, or perform other access checks
        logger.debug("Tenant access validated", tenant=tenant)
    
    def is_authenticated(self) -> bool:
        """Check if authentication is configured."""
        return bool(
            (self.config.username and self.config.password) or 
            self.config.bearer_token
        )
    
    def get_auth_type(self) -> str:
        """Get the type of authentication being used."""
        if self.config.bearer_token:
            return "bearer_token"
        elif self.config.username and self.config.password:
            return "basic_auth"
        else:
            return "none"
