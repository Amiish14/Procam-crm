"""
v2026-08 — Pre-Sales Intelligence Layer for Procam CRM.

Extends the CRM with Account Development and Project Intelligence entities
that live UPSTREAM of the existing Lead / Opportunity / RFQ / Quote pipeline.

    docs/PRE_SALES_INTELLIGENCE_ARCHITECTURE.md   ← full design spec

This package deliberately DOES NOT duplicate any existing sales-pipeline
logic. The current Lead → RFQ → Quote → Won/Lost flow is untouched. What
lives here is the pre-sales layer that eventually FEEDS the pipeline
through the OpportunitySourceLink table.

Phase 1 (this drop):
    * Account Master (extends existing Company by additive columns)
    * AccountRelationshipTag  — many-to-many role tagging
    * AccountAssignmentHistory — PIC handover audit trail
    * AccountStageHistory      — dev-stage transitions
    * AccountActivity          — chronological activity timeline
    * REST API under /api/accounts
"""
from flask import Blueprint

bp = Blueprint('presales', __name__)

# Import routes so the @bp.route decorators register.
from presales import routes                                        # noqa: E402, F401
