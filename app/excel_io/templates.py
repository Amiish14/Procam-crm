"""Excel template definitions — §43, §44, §45.

Each template declares its columns once: the header people see, the model
field it writes, whether it is required, and which Master Data list
governs its allowed values.  The downloadable workbook, the validator and
the error report are all generated from this, so a template can never
disagree with what the importer accepts.

§45 — where a value is controlled, the allowed values come from Master
Data at generation time, so a vertical added this morning appears in the
template this afternoon.
"""

# kind → definition
TEMPLATES = {}


def _t(kind, label, description, entity, columns, notes=()):
    TEMPLATES[kind] = {
        'kind': kind, 'label': label, 'description': description,
        'entity': entity, 'columns': columns, 'notes': list(notes),
    }


# column = (header, field, required, lookup_list, example, help)
_t('company', 'Company Master',
   'Organisations — customers, partners, competitors, vendors. One row '
   'per company, whatever its relationship.',
   'Company',
   [
     ('Company Name',   'name',        True,  None,
      'Bharat Heavy Electricals Limited',
      'Must be unique. An existing company is matched on this name.'),
     ('Relationship',   '_relationship', False, 'relationship',
      'Customer, Vendor',
      'One or more, comma separated. A company may hold several at once.'),
     ('Industry',       'industry',    False, 'industry', 'Power',
      'Must be a value from the Industry list.'),
     ('Website',        'website',     False, None, 'bhel.com', ''),
     ('Country',        'country',     False, None, 'India', ''),
     ('State',          'state',       False, None, 'Telangana', ''),
     ('City',           'city',        False, None, 'Hyderabad', ''),
     ('Address',        'address',     False, None, '', ''),
     ('Phone',          'phone',       False, None, '+91 40 2318 5000', ''),
     ('Email',          'email',       False, None, 'info@bhel.com', ''),
     ('LinkedIn',       'linkedin',    False, None, '', ''),
     ('Account Owner',  'pic_emp_code', False, None, 'EMP372011',
      'Employee code of the person who owns this relationship.'),
     ('Account Stage',  'dev_stage',   False, 'account_stage', '', ''),
     ('Priority',       'priority',    False, 'priority', 'Medium', ''),
     ('Notes',          'notes',       False, None, '', ''),
   ],
   notes=[
     'A company is matched on its name. Legal suffixes are ignored, so '
     '"BHEL Ltd" and "BHEL Limited" are treated as the same organisation.',
     'Do NOT create separate rows for the same company under different '
     'relationships. Use one row and list every relationship in the '
     'Relationship column.',
   ])

_t('person', 'People Master',
   'Individuals. One row per person, linked to their company by name.',
   'Contact',
   [
     ('Full Name',    'name',        True,  None, 'Rita Shah', ''),
     ('Company',      '_company',    True,  None, 'Bharat Heavy Electricals',
      'Must match a company in Company Master, or one in this upload.'),
     ('Designation',  'designation', False, None, 'Head of Logistics', ''),
     ('Department',   'department',  False, None, 'Supply Chain', ''),
     ('Email',        'email',       False, None, 'rita@bhel.com',
      'Used to spot an existing person.'),
     ('Mobile',       'mobile',      False, None, '+91 98200 00000', ''),
     ('Phone',        'phone',       False, None, '', ''),
     ('LinkedIn',     'linkedin',    False, None, '', ''),
     ('City',         'city',        False, None, 'Hyderabad', ''),
     ('Country',      'country',     False, None, 'India', ''),
     ('Assigned To',  'assigned_to', False, None, 'EMP372011',
      'Employee code of the owner.'),
   ],
   notes=[
     'A person is matched on email where present, otherwise on name plus '
     'company.',
     'Do not create a second person because their role changed. One '
     'person, one record.',
   ])

_t('company_contact', 'Company + Contact combined',
   'One row carrying both an organisation and a person at it — the shape '
   'most collected lists arrive in.',
   'Company+Contact',
   [
     ('Company Name',  'name',        True,  None, 'Vedanta Limited', ''),
     ('Relationship',  '_relationship', False, 'relationship', 'Customer', ''),
     ('Industry',      'industry',    False, 'industry', 'Metals', ''),
     ('Website',       'website',     False, None, 'vedanta.co.in', ''),
     ('Country',       'country',     False, None, 'India', ''),
     ('City',          'city',        False, None, 'Mumbai', ''),
     ('Contact Name',  '_person_name', False, None, 'A Kumar', ''),
     ('Designation',   '_designation', False, None, 'Procurement Head', ''),
     ('Contact Email', '_person_email', False, None, 'a.kumar@vedanta.co.in',
      ''),
     ('Contact Mobile', '_person_mobile', False, None, '', ''),
     ('Account Owner', 'pic_emp_code', False, None, '', ''),
   ],
   notes=[
     'The company is created or matched first, then the contact is '
     'attached to it. Several rows for the same company create ONE '
     'company with several contacts.',
   ])

_t('lead', 'Historical Lead',
   'Past enquiries and opportunities, for history and reporting.',
   'Lead',
   [
     ('Company',       'company',     True,  None, 'Adani Power', ''),
     ('Project',       'project',     False, None, 'Transformer movement', ''),
     ('Industry',      'industry',    False, 'industry', 'Power', ''),
     ('Vertical',      'procam_vertical', False, 'vertical',
      'Heavy Transport', ''),
     ('Source',        'source',      False, 'source', 'manual', ''),
     ('Stage',         'stage',       False, None, 'New', ''),
     ('Contact Person', 'pic',        False, None, 'R Sharma', ''),
     ('Email',         'email',       False, None, '', ''),
     ('Phone',         'phone',       False, None, '', ''),
     ('State',         'state',       False, None, 'Gujarat', ''),
     ('City',          'city',        False, None, 'Mundra', ''),
     ('Assigned To',   'assigned_to', False, None, 'EMP372011',
      'Employee code. Leave blank only if genuinely unassigned.'),
     ('Notes',         'notes',       False, None, '', ''),
   ],
   notes=[
     'The Company column is matched against Company Master and linked by '
     'id. Names that cannot be matched go to the Data Mapping queue for a '
     'decision rather than being guessed.',
   ])

_t('network', 'Network Membership',
   'Which networks a company belongs to — PCN, THLG and others.',
   'Network',
   [
     ('Company Name', 'name',      True,  None, 'Logfret Inc.', ''),
     ('Network',      '_network',  True,  'network', 'PCN',
      'Must be a value from the Network list.'),
   ],
   notes=['Networks are recorded as relationship classifications on the '
          'one company record.'])


def lookups_for(kind):
    """The Master Data lists a template's columns reference (§45)."""
    from app.master_data import service as md
    lists = {}
    for _h, _f, _r, lookup, _e, _help in TEMPLATES[kind]['columns']:
        if lookup and lookup not in lists:
            lists[lookup] = [i.label for i in md.items(lookup)]
    return lists


def required_headers(kind):
    return [c[0] for c in TEMPLATES[kind]['columns'] if c[2]]


def all_headers(kind):
    return [c[0] for c in TEMPLATES[kind]['columns']]
