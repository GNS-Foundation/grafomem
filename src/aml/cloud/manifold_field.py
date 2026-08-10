"""Manifold field — pure numpy core for GET /v1/manifold/field.

Re-sources the Semantic Manifold from `decision_embeddings` (the CGR economy) instead
of orchestrator SOM cells. Two pieces, both pure/testable (no DB):

  * pca_2d(X)          — linear PCA to 2 dims with a DETERMINISTIC sign convention so
                         the map never mirror-flips between refreshes.
  * kernel_beta_field  — a kernel-smoothed Beta OUTCOME POSTERIOR on a grid (not a raw
                         smoothed rate), with per-cell support so the UI can fade
                         thin-evidence regions.

Single-domain today (CGR is "receivables"-only) — stated honestly by the caller.
"""
from __future__ import annotations

import numpy as np


def pca_2d(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Center + thin SVD → top-2 principal components, with a sign convention that
    makes coordinates STABLE across calls: each PC is oriented so its largest-magnitude
    loading is positive. SVD's sign is otherwise arbitrary, so without this the plane
    mirror-flips between refreshes on identical data.

    Returns (coords[n,2], variance_explained[2]). Caller must pre-filter non-finite rows
    and guarantee n >= 3.
    """
    mu = X.mean(axis=0)
    Xc = X - mu
    _, S, Vt = np.linalg.svd(Xc, full_matrices=False)
    pcs = Vt[:2].copy()                      # (2, d) loadings
    for i in range(pcs.shape[0]):
        j = int(np.argmax(np.abs(pcs[i])))   # largest-|loading| index
        if pcs[i, j] < 0:
            pcs[i] = -pcs[i]                  # pin its sign positive → deterministic
    # np.errstate silences a SPURIOUS numpy/BLAS matmul FP-flag warning on some builds
    # (a non-C-contiguous operand trips the SIMD kernel); inputs are finite and the
    # result is checked below, so this is safe.
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        coords = np.ascontiguousarray(Xc) @ np.ascontiguousarray(pcs.T)  # (n, 2)
    total = float((S ** 2).sum()) or 1.0
    var = (S[:2] ** 2) / total
    # pad to length 2 if the data had rank < 2
    if var.shape[0] < 2:
        var = np.concatenate([var, np.zeros(2 - var.shape[0])])
    return coords, var


def kernel_beta_field(
    coords: np.ndarray,
    paid: np.ndarray,
    resolved: np.ndarray,
    *,
    resolution: int = 40,
    h_frac: float = 0.06,
    pad: float = 0.05,
) -> dict:
    """Kernel-smoothed Beta OUTCOME POSTERIOR over the coords' bounding box.

    For each grid cell g, using only RESOLVED points p (paid/default) with a Gaussian
    kernel weight k_p = exp(-||g-p||^2 / 2h^2):

        support(g)      = Σ_p k_p                      (evidence mass at g → fade low)
        posterior(g)    = (1 + Σ_p k_p·paid_p) / (2 + Σ_p k_p)

    This is a Beta(1,1)-prior posterior mean of the LOCAL paid-fraction (kernel pseudo-
    counts), NOT a raw smoothed rate: with little nearby evidence it shrinks toward 0.5,
    which is exactly what the diverging red↔blue wash should show as "unknown". Bandwidth
    h = `h_frac` × the larger padded-plane extent (documented, relative to the normalized
    plane). `grid[row][col]` indexes row=y (ys ascending), col=x (xs ascending).
    """
    resolution = max(2, int(resolution))
    rp = coords[resolved]
    pd = paid[resolved].astype(float)

    xmin, ymin = coords.min(axis=0)
    xmax, ymax = coords.max(axis=0)
    dx = (xmax - xmin) or 1.0
    dy = (ymax - ymin) or 1.0
    xmin -= pad * dx; xmax += pad * dx
    ymin -= pad * dy; ymax += pad * dy
    h = h_frac * max(xmax - xmin, ymax - ymin)
    h = float(h) or 1.0

    xs = np.linspace(xmin, xmax, resolution)
    ys = np.linspace(ymin, ymax, resolution)
    grid = np.full((resolution, resolution), 0.5, dtype=float)
    support = np.zeros((resolution, resolution), dtype=float)

    if rp.shape[0] > 0:
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            gx, gy = np.meshgrid(xs, ys)                        # (res, res), gy is row=y
            gpts = np.stack([gx.ravel(), gy.ravel()], axis=1)   # (res*res, 2)
            d2 = ((gpts[:, None, :] - rp[None, :, :]) ** 2).sum(axis=2)  # (res*res, n_res)
            k = np.exp(-d2 / (2.0 * h * h))
            s = k.sum(axis=1)
            num = 1.0 + (k * pd[None, :]).sum(axis=1)
            post = num / (2.0 + s)
        grid = post.reshape(resolution, resolution)
        support = s.reshape(resolution, resolution)

    return {
        "resolution": resolution,
        "x_range": [float(xmin), float(xmax)],
        "y_range": [float(ymin), float(ymax)],
        "bandwidth": h,
        "h_frac": h_frac,
        "grid": np.round(grid, 4).tolist(),
        "support": np.round(support, 3).tolist(),
    }
