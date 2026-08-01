"""CGR — Capability-Grounded Reputation (v1, receivables).

A separable scoring layer over the CGR substrate captured in Ticket #1. Reads
governed decisions + ground-truth outcomes and emits a per-agent Beta-mean trust
score plus a TierGate-style band contract. Import-isolated: the core
(substrate/scoring/engine) imports only stdlib + numpy and takes its data-access
objects by injection — no portal/billing/UI dependencies — so it can graduate to
a standalone package (the capability-grounded upgrade to GEIANT's TierGate).
"""
from aml.cgr.engine import (
    MIN_RESOLVED_PROVEN, compute_scores, compute_scores_from_rows, to_tiergate,
)
from aml.cgr.scoring import (
    CGRResult, DIMENSION_RECEIVABLES, K_PRIOR, beta_prior, reviewer_weights, score_agent,
)
from aml.cgr.substrate import (
    CGR_OUTCOMES_STORE, CGR_OUTCOME_SCHEMA, CGR_REVIEWS_STORE, CGR_REVIEW_SCHEMA,
    DecisionRow, ReviewEvent, export_reviews, export_rows, load_reviews, load_substrate,
)

__all__ = [
    "CGRResult", "DecisionRow", "ReviewEvent",
    "DIMENSION_RECEIVABLES", "K_PRIOR", "MIN_RESOLVED_PROVEN",
    "CGR_OUTCOMES_STORE", "CGR_OUTCOME_SCHEMA", "CGR_REVIEWS_STORE", "CGR_REVIEW_SCHEMA",
    "beta_prior", "reviewer_weights", "score_agent",
    "compute_scores", "compute_scores_from_rows", "to_tiergate",
    "load_substrate", "load_reviews", "export_rows", "export_reviews",
]
