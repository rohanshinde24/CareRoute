import asyncio
from mcp import Client

from app.mcp_server import mcp
from app.tools import RiskClass, TOOLS

def test_tool_contracts_define_operational_behavior():
    assert set(TOOLS) == {"getReferral", "getPatient", "getCoverage", "getReferralDocuments", "getReferralHistory", "getRecentProcedures", "requestMissingDocument", "findProviders", "getAvailableSlots"}
    assert TOOLS["requestMissingDocument"].risk == RiskClass.ADMINISTRATIVE_WRITE
    assert TOOLS["requestMissingDocument"].idempotent is True
    assert all(tool.timeout_seconds > 0 and tool.failure_behavior for tool in TOOLS.values())

def test_mcp_exposes_typed_tool_surface():
    async def list_names():
        async with Client(mcp) as client:
            return {tool.name for tool in (await client.list_tools()).tools}
    assert asyncio.run(list_names()) == set(TOOLS)
