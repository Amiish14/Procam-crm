"""Operational monitoring — the checks, the admin page and its API.

checks.py imports nothing from the application, so scripts/ops_status.py
can load it by path and run on the server without booting app.py (whose
import runs the boot autoheal against the live database).
"""
