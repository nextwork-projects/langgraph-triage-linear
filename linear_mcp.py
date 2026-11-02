"""Secret Mission: an agent reads Linear through its MCP server, with read-only tools.

Every write still goes through linear.py. This agent can only look: it gets its
tools from https://mcp.linear.app/mcp/readonly, which offers only read tools
(get_, list_ and search_), so the same LINEAR_API_KEY is safe to send. Linear's
MCP server takes that key as a Bearer token (the GraphQL API takes it with no prefix).
"""
import asyncio
import os
import warnings

from langchain_core._api import LangChainBetaWarning

READONLY_URL = "https://mcp.linear.app/mcp/readonly"


def configured() -> bool:
    return bool(os.environ.get("LINEAR_API_KEY"))


def adapter(target=None):
    """An MCPAdapter for Linear's read-only server. Tests pass an in-process server instead."""
    # langchain.mcp is in beta and warns on import. Expected; nothing to fix.
    warnings.filterwarnings("ignore", category=LangChainBetaWarning)
    from fastmcp import Client
    from langchain.mcp import MCPAdapter

    if target is None:
        target = Client(READONLY_URL, auth=os.environ["LINEAR_API_KEY"])  # sent as Authorization: Bearer
    return MCPAdapter(target)


async def _history(model, service: str, incident_issue: str, target=None) -> str:
    from langchain.agents import create_agent
    from mcp.shared.exceptions import MCPError

    try:
        tools = await adapter(target).list_tools()
    except MCPError as error:
        # A bad or missing key shows up as a generic server error, not a 401.
        raise RuntimeError(f"Linear MCP refused the connection. Check LINEAR_API_KEY. ({error})")
    agent = create_agent(
        model,
        tools,
        # The agent runs inside a graph node, so it would inherit the outer graph's
        # checkpointer, which can't be used from async code. It needs no memory of its own.
        checkpointer=False,
        system_prompt=(
            "You read a Linear workspace with read-only tools. Find earlier incident "
            f"issues for the service {service}, other than {incident_issue}. Answer with "
            "at most 3 bullets: identifier, title, and how it ended. If there are none, "
            "answer 'No earlier incidents.'"
        ),
    )
    # It would also inherit the outer run's durability="sync", which fails without a checkpointer.
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": f"Past incidents for {service}?"}]},
        durability="exit",
    )
    return result["messages"][-1].text


def incident_history(model, service: str, incident_issue: str, target=None) -> str:
    """Run the read-only agent from a sync graph node. MCP tools are async only."""
    return asyncio.run(_history(model, service, incident_issue, target))
