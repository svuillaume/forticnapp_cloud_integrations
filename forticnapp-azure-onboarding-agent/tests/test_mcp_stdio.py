"""Drive the FortiCNAPP MCP server over stdio like Claude Desktop would."""
import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ENV = {**os.environ, "LW_ACCOUNT": "http://127.0.0.1:9000", "LW_API_KEY": "lw-key",
       "LW_API_SECRET": "lw-secret", "FCNAPP_SECRET_AZURE": "real-azure-secret"}
T = "eeeeeeee-1111-2222-3333-ffffffffffff"; C = "11111111-2222-3333-4444-555555555555"

async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.forticnapp_mcp"],
                                   env=ENV, cwd=os.getcwd())
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = (await s.list_tools()).tools
        print("TOOLS:", [(t.name, t.annotations.readOnlyHint) for t in tools])
        async def call(n, a):
            res = await s.call_tool(n, a)
            out = res.structuredContent or json.loads(res.content[0].text)
            return out.get("result", out) if isinstance(out, dict) and set(out) == {"result"} else out
        print("LIST:", json.dumps(await call("forticnapp_list_cloud_accounts", {}))[:200])
        base = dict(name="azure-desk-cfg", type="AzureCfg", tenant_id=T, client_id=C, secret_ref="env:FCNAPP_SECRET_AZURE")
        print("BAD REF:", await call("forticnapp_create_azure_cloud_account", {**base, "secret_ref": "env:LW_API_SECRET"}))
        print("DRY RUN:", await call("forticnapp_create_azure_cloud_account", base))
        print("CREATE:", json.dumps(await call("forticnapp_create_azure_cloud_account", {**base, "dry_run": False}))[:250])
        print("DUPLICATE:", (await call("forticnapp_create_azure_cloud_account", base))["status"])

asyncio.run(main())
