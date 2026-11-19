"""Business Card Import — data model (Phase 12 of the CRM upgrade).

One table.  Each row is an upload / extraction attempt.  Follows the
Claude Vision pipeline in app/services/business_card_ocr.py.
"""
from datetime import datetime

from app import db


class BusinessCardImport(db.Model):
    __tablename__ = 'business_card_imports'

    id                 = db.Column(db.Integer, primary_key=True)
    uploaded_by_id     = db.Column(db.String(20), nullable=False)
    uploaded_at        = db.Column(db.DateTime, default=datetime.utcnow,
                                   index=True)
    image_path         = db.Column(db.Text)          # server-stored image
    # CRM-SEARCH-SCAN-01 Part 2 — front and back are ONE card. The
    # address and the second number are usually on the back, and two
    # separate scans of one person is the duplicate this exists to
    # avoid.
    back_image_path    = db.Column(db.Text)
    #: {field: {score, why, low}} — what the reviewer should look at.
    confidence_json    = db.Column(db.JSON)
    #: What preparing the picture did to it, in words.
    prep_notes         = db.Column(db.Text)
    ocr_raw            = db.Column(db.Text)          # raw model response
    extracted_json     = db.Column(db.JSON)          # dict of parsed fields
    status             = db.Column(db.String(24), default='Extracted',
                                   index=True)
    # ── Result linking ────────────────────────────────────────────────
    created_account_id = db.Column(db.Integer,
                                   db.ForeignKey('companies.id'),
                                   nullable=True)
    created_contact_id = db.Column(db.Integer,
                                   db.ForeignKey('contacts.id'),
                                   nullable=True)
    linked_account_id  = db.Column(db.Integer,
                                   db.ForeignKey('companies.id'),
                                   nullable=True)
    linked_contact_id  = db.Column(db.Integer,
                                   db.ForeignKey('contacts.id'),
                                   nullable=True)
    dup_matches        = db.Column(db.JSON)          # {accounts:[], contacts:[]}
    remarks            = db.Column(db.Text)

    def to_dict(self):
        return {
            'id': self.id,
            'uploaded_by_id': self.uploaded_by_id,
            'uploaded_at': str(self.uploaded_at) if self.uploaded_at else '',
            'status': self.status,
            'image_path': self.image_path or '',
            'back_image_path': self.back_image_path or '',
            'has_back': bool(self.back_image_path),
            'confidence': self.confidence_json or {},
            'prep_notes': [n for n in (self.prep_notes or '').split('\n')
                           if n.strip()],
            'extracted': self.extracted_json or {},
            'dup_matches': self.dup_matches or {'accounts': [], 'contacts': []},
            'created_account_id': self.created_account_id,
            'created_contact_id': self.created_contact_id,
            'linked_account_id': self.linked_account_id,
            'linked_contact_id': self.linked_contact_id,
            'remarks': self.remarks or '',
        }


class CardDomainHint(db.Model):
    """What a card from this email domain was corrected to — CRM-SCAN-02.

    One row per (domain, field). Written when a reviewer saves a card
    whose company differs from what was read; read by
    `card_smart.rescore` as the strongest company candidate for the
    next card from the same domain. A new table: created by
    `db.create_all()` at boot, nothing else changes.
    """
    __tablename__ = 'card_domain_hints'

    id         = db.Column(db.Integer, primary_key=True)
    domain     = db.Column(db.String(120), nullable=False, index=True)
    field      = db.Column(db.String(40), nullable=False, default='company')
    value      = db.Column(db.String(255), nullable=False)
    times      = db.Column(db.Integer, default=1)
    updated_by = db.Column(db.String(20))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('domain', 'field',
                                          name='uq_card_domain_field'),)
