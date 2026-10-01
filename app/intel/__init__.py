"""
External intelligence — projects, competitors, vendors and vessels.

    app.intel.adapters     where intelligence can come from, and whether
                           that source is actually available
    app.intel.projects     ingesting items into ONE project timeline
    app.intel.competitors  competitor activity by vertical and service
    app.intel.vendors      vendors, vessel capability, India port calls
    app.intel.routes       the pages and the JSON API

Two rules run through all of it.

**Nothing is invented.** An adapter that has no subscription, key or
licence returns an empty list and a reason. It never returns a plausible
example row, because a plausible example row is indistinguishable from
intelligence once it is on a screen. `adapters.status_report()` is the
honest answer to "what is actually running?" and the Sources page shows
it verbatim.

**Vocabularies come from Master Data.** Vendor categories, vessel types,
India ports, competitor activity types and confidence levels are all
Master Data lists. The tuples below are what the migration seeds the
lists WITH; they are not read at runtime, so a value an administrator
adds, renames or retires takes effect everywhere at once.
"""
from __future__ import annotations

# ── Master Data list keys this module reads ──────────────────────────
LIST_VENDOR_CATEGORY = 'vendor_category'
LIST_VESSEL_TYPE = 'vessel_type'
LIST_INDIA_PORT = 'india_port'
LIST_CONFIDENCE = 'intel_confidence'
LIST_ACTIVITY_TYPE = 'competitor_activity_type'
LIST_INTEL_STAGE = 'intel_project_stage'

#: (key, label, description) for the MasterList registry rows.
INTEL_LISTS = (
    (LIST_VENDOR_CATEGORY, 'Vendor Category',
     'Categories of carrier, operator, agent and partner the vendor '
     'intelligence register is organised by.'),
    (LIST_VESSEL_TYPE, 'Vessel Type',
     'Vessel types used by the vessel capability register.'),
    (LIST_INDIA_PORT, 'India Port',
     'Indian ports that port-call intelligence is recorded against. '
     'Add a port here rather than in code.'),
    (LIST_CONFIDENCE, 'Intelligence Confidence',
     'How much weight an intelligence record carries. Shown next to '
     'every external claim.'),
    (LIST_ACTIVITY_TYPE, 'Competitor Activity Type',
     'What a competitor was seen doing.'),
    (LIST_INTEL_STAGE, 'Project Intelligence Stage',
     'The external lifecycle of a project, from announcement to '
     'commissioning. Maps onto the CRM project stages.'),
)

# ── seeds ────────────────────────────────────────────────────────────
# Seeded once by scripts/2026_10_09_intelligence.py. Runtime code reads
# Master Data, never these tuples.
SEED_VENDOR_CATEGORIES = (
    ('shipping_line', 'Shipping line'),
    ('mpv_breakbulk', 'MPV / breakbulk owner or operator'),
    ('roro_operator', 'RoRo owner or operator'),
    ('heavy_lift_operator', 'Heavy-lift owner or operator'),
    ('container_line', 'Container line'),
    ('port_agent', 'Port agent'),
    ('vessel_agent', 'Vessel agent'),
    ('transporter', 'Transporter'),
    ('heavy_haul', 'Heavy-haul operator'),
    ('crane_hire', 'Crane hire'),
    ('spmt_operator', 'SPMT operator'),
    ('rigging', 'Rigging contractor'),
    ('warehouse', 'Warehouse'),
    ('customs_partner', 'Customs partner'),
    ('airline', 'Airline'),
    ('air_cargo_agent', 'Air cargo agent'),
    ('overseas_partner', 'Overseas partner'),
)

SEED_VESSEL_TYPES = (
    ('mpv', 'Multi-purpose vessel'),
    ('breakbulk', 'Breakbulk'),
    ('heavy_lift', 'Heavy-lift'),
    ('roro', 'RoRo'),
    ('container', 'Container'),
    ('bulk_carrier', 'Bulk carrier'),
    ('barge', 'Barge / pontoon'),
    ('tug', 'Tug'),
)

#: The ports the brief names. Extensible: an administrator adds a port
#: to the `india_port` list and it appears in every filter at once.
SEED_INDIA_PORTS = (
    ('INBOM', 'Mumbai'),
    ('INNSA', 'JNPT / Nhava Sheva'),
    ('INMAA', 'Chennai'),
    ('INPRT', 'Paradip'),
    ('INMUN', 'Mundra'),
    ('INIXY', 'Kandla / Deendayal'),
    ('INCCU', 'Kolkata'),
    ('INHAL', 'Haldia'),
)

SEED_CONFIDENCE = (
    ('confirmed', 'Confirmed — a named, checkable source says so'),
    ('reported', 'Reported — one public source, not yet corroborated'),
    ('unverified', 'Unverified — heard, not yet sourced'),
)

SEED_ACTIVITY_TYPES = (
    ('contract_win', 'Contract win'),
    ('bid_participation', 'Bid participation'),
    ('pricing', 'Pricing observed'),
    ('new_office', 'New office or branch'),
    ('new_service', 'New service or lane'),
    ('equipment', 'Equipment acquired'),
    ('partnership', 'Partnership or agency'),
    ('personnel', 'Personnel movement'),
    ('loss_to_competitor', 'Procam lost work to them'),
    ('other', 'Other'),
)


#: The external project lifecycle, in the brief's order: announced →
#: land → clearance → EPC → equipment order → supplier → manufacturing →
#: shipment → commissioning. Each entry is
#: (code, label, the presales PROJECT_STAGES value it maps onto, the
#: phrases that are evidence for it).
#:
#: It lives here rather than in projects.py so the migration can seed the
#: Master Data list from it without importing the Flask application.
INTEL_STAGES = (
    ('announced', 'Announced', 'Announced / Proposed',
     ('announce', 'proposed', 'plans to set up', 'to invest', 'mou',
      'board approves')),
    ('land', 'Land acquisition', 'Planning',
     ('land acqui', 'land allot', 'site selected', 'acquires land')),
    ('clearance', 'Clearances', 'Approval / Funding',
     ('environment clearance', 'clearance granted', 'approval granted',
      'financial closure', 'cabinet approv')),
    ('epc', 'EPC appointed', 'EPC Appointed',
     ('epc contract', 'epc awarded', 'wins contract', 'bags order',
      'lstk', 'appointed epc')),
    ('equipment_order', 'Equipment ordered', 'Procurement Started',
     ('places order', 'equipment order', 'purchase order',
      'tender awarded')),
    ('supplier', 'Supplier identified', 'Equipment Procurement',
     ('supplier', 'to supply', 'vendor selected', 'oem')),
    ('manufacturing', 'Under manufacture', 'Logistics Opportunity Identified',
     ('under manufacture', 'fabrication', 'rolled out of the shop',
      'ready for dispatch')),
    ('shipment', 'Shipment', 'RFQ Expected',
     ('shipment', 'shipped', 'sails', 'consignment', 'cargo moved',
      'heavy lift moved')),
    ('commissioning', 'Commissioning', 'Execution',
     ('commission', 'inaugurat', 'goes on stream', 'first production')),
)

SEED_INTEL_STAGES = tuple((code, label) for code, label, _p, _k
                          in INTEL_STAGES)


def master_items(list_key):
    """The live values of a Master Data list, as (code, label) pairs.

    Returns [] when the list is empty or Master Data is unreachable:
    an empty vocabulary is an honest "nobody has configured this yet",
    and the screens say so rather than falling back to a hidden list.
    """
    try:
        from app.master_data import service as md
        return [(i.code, i.label) for i in md.items(list_key)]
    except Exception:
        return []


def master_codes(list_key):
    return [code for code, _label in master_items(list_key)]


def master_labels(list_key):
    return dict(master_items(list_key))
