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
            'extracted': self.extracted_json or {},
            'dup_matches': self.dup_matches or {'accounts': [], 'contacts': []},
            'created_account_id': self.created_account_id,
            'created_contact_id': self.created_contact_id,
            'linked_account_id': self.linked_account_id,
            'linked_contact_id': self.linked_contact_id,
            'remarks': self.remarks or '',
        }
