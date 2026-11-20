"""The single decision authority.

Claude Desktop starts one interceptor process per MCP server, so there is no
one process that can own a queue, a rules file or a window. This is that
process: the interceptors stay thin and ask it, and it holds all the state.
"""

from mcp_gatekeeper.daemon.decisions import Decider, DecisionQueue, Withdrawn
from mcp_gatekeeper.daemon.gate import make_gate
from mcp_gatekeeper.daemon.server import Daemon

__all__ = ["Daemon", "Decider", "DecisionQueue", "Withdrawn", "make_gate"]
