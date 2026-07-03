"""Layer 1 — frozen contracts (Papers A–D, RFC 0000/0001). Do not edit while building."""
from __future__ import annotations
import numpy as np

def read(M: np.ndarray, q: np.ndarray, act: str = "identity") -> np.ndarray:
    """The ABI: query-linear read y = M q (RFC 0001). σ optional & 1-Lipschitz."""
    y = q @ M.T
    return np.tanh(y) if act == "tanh" else y

def feasible(M: np.ndarray, norm_budget: float) -> bool:
    """Feasibility region V (Paper D / RFC 0000): bounded state; NOT closed under merge."""
    return float(np.linalg.norm(M)) <= norm_budget + 1e-9

def merge(states) -> np.ndarray:
    """Canonical merge = vector addition (RFC 0000). Capacity-additive."""
    return np.sum([s.M for s in states], axis=0)
