"""
HTTP server wrapper for SSE MCP server with health check endpoints.
"""

import asyncio
import json
from typing import Any, Dict, Optional

from ..config import LokiConfig
from ..utils.logging import setup_logging
from .sse_server import SSEServer

logger = setup_logging(__name__)


class HTTPServer:
    """HTTP server that wraps SSE MCP server and provides additional endpoints."""
    
    def __init__(self, config: LokiConfig):
        """Initialize HTTP server."""
        self.config = config
        self.host = getattr(config, 'server_host', '0.0.0.0')
        self.port = getattr(config, 'server_port', 8080)
        self.sse_server = SSEServer(config)
        self._server: Optional[asyncio.Server] = None
    
    async def start(self) -> None:
        """Start the HTTP server."""
        try:
            logger.info(
                "Starting HTTP server with SSE support",
                host=self.host,
                port=self.port
            )
            
            # Start the underlying SSE server
            await self.sse_server.start()
            
            # Start HTTP server
            self._server = await asyncio.start_server(
                self._handle_connection,
                self.host,
                self.port
            )
            
            logger.info(
                "HTTP server started successfully",
                endpoints={
                    "sse": f"http://{self.host}:{self.port}/sse",
                    "health": f"http://{self.host}:{self.port}/health",
                    "info": f"http://{self.host}:{self.port}/"
                }
            )
            
        except Exception as e:
            logger.error("Failed to start HTTP server", error=str(e), exc_info=True)
            raise
    
    async def stop(self) -> None:
        """Stop the HTTP server."""
        logger.info("Stopping HTTP server")
        
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        
        await self.sse_server.stop()
        logger.info("HTTP server stopped")
    
    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle HTTP connection."""
        try:
            # Read HTTP request
            request_line = await reader.readline()
            if not request_line:
                return
            
            request_line = request_line.decode('utf-8').strip()
            method, path, version = request_line.split(' ', 2)
            
            # Read headers
            headers = {}
            while True:
                line = await reader.readline()
                if not line or line == b'\r\n':
                    break
                line = line.decode('utf-8').strip()
                if ':' in line:
                    key, value = line.split(':', 1)
                    headers[key.strip().lower()] = value.strip()
            
            # Route the request
            if path == '/health':
                await self._handle_health_check(writer)
            elif path == '/':
                await self._handle_info(writer)
            elif path == '/sse':
                # For SSE endpoint, we need to delegate to the SSE transport
                # This is a simplified approach - in production, you'd want proper SSE handling
                await self._handle_sse_endpoint(writer, headers)
            else:
                await self._handle_not_found(writer)
                
        except Exception as e:
            logger.error("Error handling HTTP request", error=str(e))
            await self._handle_error(writer, str(e))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
    
    async def _handle_health_check(self, writer: asyncio.StreamWriter) -> None:
        """Handle health check endpoint."""
        try:
            health_status = await self.sse_server.health_check()
            status_code = 200 if health_status["status"] == "healthy" else 503
            
            response_body = json.dumps(health_status, indent=2)
            response = (
                f"HTTP/1.1 {status_code} {'OK' if status_code == 200 else 'Service Unavailable'}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(response_body)}\r\n"
                f"Access-Control-Allow-Origin: *\r\n"
                f"\r\n"
                f"{response_body}"
            )
            
            writer.write(response.encode('utf-8'))
            await writer.drain()
            
        except Exception as e:
            logger.error("Health check endpoint error", error=str(e))
            await self._handle_error(writer, str(e))
    
    async def _handle_info(self, writer: asyncio.StreamWriter) -> None:
        """Handle server info endpoint."""
        try:
            server_info = self.sse_server.get_server_info()
            response_body = json.dumps(server_info, indent=2)
            
            response = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(response_body)}\r\n"
                f"Access-Control-Allow-Origin: *\r\n"
                f"\r\n"
                f"{response_body}"
            )
            
            writer.write(response.encode('utf-8'))
            await writer.drain()
            
        except Exception as e:
            logger.error("Info endpoint error", error=str(e))
            await self._handle_error(writer, str(e))
    
    async def _handle_sse_endpoint(self, writer: asyncio.StreamWriter, headers: Dict[str, str]) -> None:
        """Handle SSE endpoint - placeholder for proper SSE implementation."""
        # This is a simplified response - proper SSE would require more complex handling
        response_body = json.dumps({
            "message": "SSE endpoint available",
            "note": "Use proper MCP SSE client to connect",
            "endpoint": f"http://{self.host}:{self.port}/sse"
        }, indent=2)
        
        response = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(response_body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"\r\n"
            f"{response_body}"
        )
        
        writer.write(response.encode('utf-8'))
        await writer.drain()
    
    async def _handle_not_found(self, writer: asyncio.StreamWriter) -> None:
        """Handle 404 Not Found."""
        response_body = json.dumps({
            "error": "Not Found",
            "available_endpoints": ["/", "/health", "/sse"]
        }, indent=2)
        
        response = (
            f"HTTP/1.1 404 Not Found\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(response_body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"\r\n"
            f"{response_body}"
        )
        
        writer.write(response.encode('utf-8'))
        await writer.drain()
    
    async def _handle_error(self, writer: asyncio.StreamWriter, error_msg: str) -> None:
        """Handle server error."""
        response_body = json.dumps({"error": error_msg}, indent=2)
        
        response = (
            f"HTTP/1.1 500 Internal Server Error\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(response_body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"\r\n"
            f"{response_body}"
        )
        
        writer.write(response.encode('utf-8'))
        await writer.drain()
