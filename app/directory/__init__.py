"""Global CRM as a relationship database — Release 4, Group K.

The CRM already knew every person it had ever met; it had no way of being
asked about them. This package is the asking: one scoped search over
Contacts, one dashboard over the same rows, one relationship view per
person, and the Group D account-ownership directory ("who handles this
customer?") behind a single endpoint the Copilot can reuse.

Nothing here owns data that another module owns. Ownership of a contact
is still ``contacts.assigned_to`` and ``contacts.account_id``; account
ownership is still ``companies.pic_emp_code``, changed through the
Pre-Sales endpoints and the admin screen. The only new storage is the
side table in ``models.py``, which carries the three facts about a person
that no existing column could hold.
"""
