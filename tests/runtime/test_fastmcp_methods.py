import asyncio
from mcp.server.fastmcp import FastMCP
app = FastMCP("test")
@app.resource("test://res")
def my_res() -> str: return "hello"
async def main():
    print(await app.list_resources())
    print(await app.read_resource("test://res"))
asyncio.run(main())
