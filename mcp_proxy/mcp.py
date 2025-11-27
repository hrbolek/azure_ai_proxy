import asyncio
import subprocess
import os
import httpx
from contextlib import asynccontextmanager
from fastapi import Request, HTTPException
from fastapi.responses import Response

mcp_process = None
mcp_client = None

@asynccontextmanager
async def lifespan(app_instance):
    # Startup: Start MCP server
    global mcp_process, mcp_client
    mcp_port = os.getenv("MCP_PORT", "7999")
    mcp_api_key = os.getenv("MCP_API_KEY", "top-secret")
    mcp_config = os.getenv("MCP_CONFIG", "mcp_proxy/config.json")
    
    print(f"Starting MCP server with config: {mcp_config}")
    mcp_process = subprocess.Popen(
        ["mcpo", "--port", mcp_port, "--api-key", mcp_api_key, "--config", mcp_config],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Wait a bit for the server to start
    await asyncio.sleep(2)
    
    # Create HTTP client for proxying
    mcp_client = httpx.AsyncClient(
        base_url=f"http://localhost:{mcp_port}",
        timeout=60.0
    )
    
    print(f"MCP server started on port {mcp_port} (PID: {mcp_process.pid})")
    
    yield
    
    # Shutdown: Stop MCP server
    if mcp_client:
        await mcp_client.aclose()
    if mcp_process:
        mcp_process.terminate()
        mcp_process.wait()
        print("MCP server stopped")

def init_mcp(app):
    """Initialize MCP proxy routes on the FastAPI app."""
    @app.api_route("/mcp/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], include_in_schema=False)
    async def mcp_proxy(request: Request, path: str):
        global mcp_client
        
        # Debug logging
        print(f"[MCP PROXY] {request.method} /mcp/{path}")
        print(f"[MCP PROXY] Full URL: {request.url}")
        print(f"[MCP PROXY] Query params: {dict(request.query_params)}")
        
        if mcp_client is None:
            raise HTTPException(status_code=503, detail="MCP server not available")
        
        # Construct the target URL - path already contains everything after /mcp/
        # So "time/openapi.json" becomes "/time/openapi.json"
        target_url = f"/{path}"
        print(f"[MCP PROXY] Forwarding to: http://localhost:{os.getenv('MCP_PORT', '7999')}{target_url}")
        
        # Get query parameters
        query_params = dict(request.query_params)
        
        # Get request body
        body = await request.body() if request.method in ["POST", "PUT", "PATCH"] else None
        
        # Forward headers (excluding host and connection)
        headers = {k: v for k, v in request.headers.items() 
                   if k.lower() not in ["host", "connection", "content-length"]}
        
        try:
            # Make request to MCP server
            response = await mcp_client.request(
                method=request.method,
                url=target_url,
                params=query_params,
                content=body,
                headers=headers,
                follow_redirects=True
            )
            
            print(f"[MCP PROXY] Response status: {response.status_code}")
            
            # Stream the response
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers={k: v for k, v in response.headers.items() 
                        if k.lower() not in ["content-length", "connection", "transfer-encoding"]},
                media_type=response.headers.get("content-type")
            )
        except httpx.ConnectError as e:
            print(f"[MCP PROXY] Connection error: {e}")
            raise HTTPException(status_code=502, detail=f"MCP server connection error: {str(e)}")
        except httpx.RequestError as e:
            print(f"[MCP PROXY] Request error: {e}")
            raise HTTPException(status_code=502, detail=f"MCP server error: {str(e)}")

