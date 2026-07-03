"""Grafomem — reference implementation of the Adaptive Memory Runtime (SPEC-1.0)."""
from .contracts import read, feasible, merge
from .cso import CSO, GFM_MAGIC, GFM_VERSION
from .runtime import (Registry, Selector, Scheduler, Linker, Governance,
                      ExecutionContext, Loader, Receipt)
from .errors import InfeasibleSchedule, PolicyViolation, SignatureMismatch, UnknownKey
from .mcp import to_mcp_resource, from_mcp_resource, MCP_CONTENT_TYPE
from .sdk import load, compose, execute, checkpoint, migrate, erase

__version__ = "1.0.0"
