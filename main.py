from azure_proxy.proxy import app
from azure_proxy.gui import init_gui
from azure_proxy.management import router as management_router
from mcp_proxy.mcp import lifespan, init_mcp

app.router.lifespan_context = lifespan
init_mcp(app)

init_gui(app)
app.include_router(management_router)