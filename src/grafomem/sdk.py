import numpy as np
from .runtime import (ExecutionContext, Governance, Registry, Selector, Scheduler, 
                      Linker, Loader, Receipt, InfeasibleSchedule, PolicyViolation)

def load(gfm_bytes: bytes, trusted_keys: dict, gov: Governance, ctx: ExecutionContext = None) -> ExecutionContext:
    """Load a signed .gfm CSO into an ExecutionContext. Verifies signatures and validates policies."""
    if ctx is None:
        ctx = ExecutionContext(model_id=gov.model_id)
    return Loader.load(ctx, gfm_bytes, gov, trusted_keys)

def compose(ctx: ExecutionContext, capabilities: list[str], registry: Registry, gov: Governance) -> ExecutionContext:
    """Dynamically compose a set of capabilities from the registry into the execution context."""
    selector = Selector(registry)
    scheduler = Scheduler(gov)
    
    candidates = selector.by_capability(capabilities, gov.model_id)
    if not candidates:
        raise InfeasibleSchedule(f"No candidates found for capabilities: {capabilities}")
        
    chosen = scheduler.schedule(candidates, capabilities)
    return Linker.link(ctx, chosen, gov)

def execute(ctx: ExecutionContext, query: np.ndarray, act: str = "identity") -> np.ndarray:
    """Execute a query against the loaded adaptive memory state."""
    return ctx.run(query, act)

def checkpoint(ctx: ExecutionContext, private_key, key_id: str) -> bytes:
    """Serialize and cryptographically sign the current execution context state into a .gfm."""
    return Loader.checkpoint(ctx, ctx.model_id, private_key, key_id)

def migrate(ctx: ExecutionContext, private_key, key_id: str, new_trusted_keys: dict, new_gov: Governance) -> ExecutionContext:
    """Migrate state to a new context. This performs a checkpoint-then-load within the same model family.
    Cross-model transcode (changing model_id/ABI) is out of scope for v1.0."""
    gfm_bytes = checkpoint(ctx, private_key, key_id)
    new_ctx = ExecutionContext(model_id=new_gov.model_id)
    return load(gfm_bytes, new_trusted_keys, new_gov, new_ctx)

def erase(ctx: ExecutionContext, scope: str, private_key, key_id: str) -> Receipt:
    """Zero out the memory state and return a cryptographically signed state-transition receipt."""
    return Loader.erase(ctx, scope, private_key, key_id)
