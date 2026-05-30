"""grafomem-landing — GRAFOMEM v3.0 "Governance Airport" reference + conformance.

NOTE: this is the governance *landing* (the airport / Landing Certificate), distinct
from src/aml/static/landing/ which is the marketing landing *page*.
"""
from .hashing import canon, b2_256, b2_128, US
from .identity import (gen_key, pub_hex, sign_hex, verify_hex, TIERS, tier_rank,
                       issue_delegation, verify_delegation)
from .crumbs import Crumbs
from .worldmodel import WorldModel
from .certificate import issue_landing_certificate, verify_landing_certificate

__version__ = "0.1.0"
