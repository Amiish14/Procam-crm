"""Tests for forwarded-lead extraction in the leads mailbox.

Policy: EVERY message that lands in leads@procamgroup.in becomes a Lead —
the mailbox is the filter, nothing is dropped. What the parser must get
right is WHO the lead is: for a forwarded email that is the ORIGINAL
prospect inside the forward, never the employee who forwarded it.

Pure parser/enricher tests — no DB, no network, no Flask app import.
Run with:  python3 tests/test_forwarded_leads.py
       or: python3 -m pytest tests/test_forwarded_leads.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_ingest import parser as P            # noqa: E402
from email_ingest import enrich as E            # noqa: E402

EMPLOYEE = "amiish.shah@procamlogistics.com"


def msg(subject, body, from_email, from_name="", html=False):
    return {
        "id": "AAA",
        "internetMessageId": "<x@y>",
        "subject": subject,
        "from": {"emailAddress": {"address": from_email, "name": from_name}},
        "bodyPreview": body[:200],
        "body": {"contentType": "html" if html else "text", "content": body},
    }


def lead_of(m):
    """Run the real ingest path (parser → enricher) and return the Lead kwargs."""
    extracted = P.extract_lead(m)
    sender = (extracted.get("email") or "").strip().lower()
    kwargs = E.build_enriched_lead_kwargs(
        m, extracted,
        sender_email=sender,
        sender_domain=sender.split("@", 1)[1] if "@" in sender else "",
        forwarded_by=extracted.get("forwarded_by"),
    )
    return extracted, kwargs


# ── 1. A forwarded lead resolves to the ORIGINAL prospect ─────────────
FORWARDED_LEAD = """Hi team, please log this one. Call me on 9999988888.

---------- Forwarded message ----------
From: Rajesh Kumar <rajesh.kumar@adanipower.com>
Sent: Monday, 1 September 2026 09:14
To: Amiish Shah <amiish.shah@procamlogistics.com>
Subject: Requirement for ODC movement Mundra to Nagpur

Dear Amiish,

We have an urgent requirement to move 4 transformer units (ODC, approx
78 MT each) from Mundra to Nagpur in October. Please share your quote.

Regards,
Rajesh Kumar
DGM - Projects, Adani Power
+91 9820012345
"""


def test_forwarded_lead_uses_the_original_sender_not_the_forwarder():
    extracted, kw = lead_of(msg(
        "FW: Requirement for ODC movement Mundra to Nagpur",
        FORWARDED_LEAD, EMPLOYEE, "Amiish Shah"))
    assert kw["email"] == "rajesh.kumar@adanipower.com"
    assert kw["pic"] == "Rajesh Kumar"
    assert kw["company"] == "Adanipower"
    # The employee's own number in the covering note must not become the
    # lead's phone — the original signature's number must.
    assert kw["phone"] == "+91-9820012345"
    # Original subject and body replace the forward wrapper.
    assert extracted["subject"] == "Requirement for ODC movement Mundra to Nagpur"
    assert extracted["forward_note"].startswith("Hi team, please log this one.")
    assert "Hi team, please log this one." not in extracted["body_text"]
    # Provenance is kept, clearly labelled, in the notes.
    assert f"[Forwarded to CRM by {EMPLOYEE}" in kw["notes"]
    assert "[Their note: Hi team, please log this one." in kw["notes"]
    # Signals come from the original message.
    assert extracted["signals"]["origin"] == "Mundra"
    assert extracted["signals"]["destination"] == "Nagpur"
    assert extracted["signals"]["rfq"] is True


def test_apple_mail_style_forward():
    body = ("Please add to CRM.\n\n"
            "Begin forwarded message:\n\n"
            "From: Priya Nair <priya@sterlitepower.com>\n"
            "Date: 1 September 2026 at 09:14:22 IST\n"
            "To: Amiish Shah <amiish.shah@procamlogistics.com>\n"
            "Subject: RFQ - transformer movement Chennai to Hyderabad\n\n"
            "Hi, we need a quote for moving 2 transformers from Chennai to "
            "Hyderabad. Contact 9845012345.\n")
    extracted, kw = lead_of(msg("Fwd: RFQ - transformer movement", body,
                                EMPLOYEE, "Amiish Shah"))
    assert kw["email"] == "priya@sterlitepower.com"
    assert extracted["subject"] == "RFQ - transformer movement Chennai to Hyderabad"


def test_html_outlook_forward():
    html = (
        "<html><body><div>Hi team, new lead attached.</div>"
        "<div>&nbsp;</div>"
        "<div><b>From:</b> Vikram Rao &lt;vikram.rao@jswsteel.in&gt;<br>"
        "<b>Sent:</b> Monday, 1 September 2026 09:14<br>"
        "<b>To:</b> Amiish Shah &lt;amiish.shah@procamlogistics.com&gt;<br>"
        "<b>Subject:</b> Enquiry - project cargo Vizag to Raipur</div>"
        "<div>&nbsp;</div>"
        "<div>We need rates for project cargo movement from Vizag to Raipur. "
        "Approx 120 MT. Please call 9812345678.</div>"
        "</body></html>")
    extracted, kw = lead_of(msg("FW: Enquiry - project cargo", html, EMPLOYEE,
                                "Amiish Shah", html=True))
    assert kw["email"] == "vikram.rao@jswsteel.in"
    assert kw["pic"] == "Vikram Rao"
    assert "Hi team, new lead attached." not in extracted["body_text"]


def test_employee_forwarding_from_a_personal_account_still_resolves():
    body = ("Sending from my phone, please log this.\n\n"
            "---------- Forwarded message ----------\n"
            "From: Anita Desai <anita@tatapower.com>\n"
            "To: Amiish Shah <amiish.shah@procamlogistics.com>\n"
            "Subject: Enquiry - warehousing at Bhiwandi\n\n"
            "We need 20,000 sq ft of warehousing at Bhiwandi. "
            "Please quote. Reach me on 9820055667.\n")
    _, kw = lead_of(msg("Fwd: Enquiry - warehousing at Bhiwandi", body,
                        "amiish.personal@gmail.com", "Amiish"))
    assert kw["email"] == "anita@tatapower.com"


def test_bare_forward_with_no_covering_note():
    body = ("---------- Forwarded message ----------\n"
            "From: Meera Joshi <meera@lntecc.com>\n"
            "To: Amiish Shah <amiish.shah@procamlogistics.com>\n"
            "Subject: Quotation for ODC movement Hazira to Kandla\n\n"
            "Please share rates for ODC movement from Hazira to Kandla, "
            "approx 90 MT. Call 9825011223.\n")
    extracted, kw = lead_of(msg("Fwd: Quotation for ODC movement", body, EMPLOYEE))
    assert extracted["forward_note"] == ""
    assert kw["email"] == "meera@lntecc.com"


# ── 2. Nothing is dropped — everything becomes a Lead ─────────────────
def test_direct_email_to_the_leads_inbox_is_captured():
    """Not a forward — still a lead, attributed to whoever sent it."""
    extracted, kw = lead_of(msg(
        "Quotation required for 2 containers Mumbai to Chennai",
        "Please quote for 2 containers from Mumbai to Chennai. "
        "Call me on 9820011223.",
        "someone@randomtrading.com", "Someone"))
    assert extracted["is_forward"] is False
    assert kw["email"] == "someone@randomtrading.com"
    assert kw["company"] == "Randomtrading"


def test_thin_content_is_captured_with_a_triage_tag():
    """No cargo/RFQ/route/phone signal — captured anyway, tagged for triage."""
    body = ("---------- Forwarded message ----------\n"
            "From: Sunil Mehta <sunil@newclientcorp.com>\n"
            "To: Amiish Shah <amiish.shah@procamlogistics.com>\n"
            "Subject: Introduction\n\n"
            "Hi Amiish, good speaking today. Sharing my details as discussed.\n"
            "Sunil Mehta, Newclient Corp\n")
    extracted, kw = lead_of(msg("FW: Introduction", body, EMPLOYEE))
    assert extracted["skip_reason"]          # a label, not a rejection
    assert kw["email"] == "sunil@newclientcorp.com"


def test_newsletter_forwarded_in_is_still_captured():
    """The employee decided it belongs in the CRM. It goes in the CRM."""
    body = ("---------- Forwarded message ----------\n"
            "From: Breakbulk News <newsletter@breakbulk.news>\n"
            "To: Amiish Shah <amiish.shah@procamlogistics.com>\n"
            "Subject: Breakbulk Weekly - project cargo market update\n\n"
            "In this issue: heavy lift rates, container demand.\n"
            "Unsubscribe | View in browser\n")
    extracted, kw = lead_of(msg("Fwd: Breakbulk Weekly", body, EMPLOYEE))
    assert kw["email"] == "newsletter@breakbulk.news"
    assert extracted["skip_reason"]          # tagged for triage, not dropped


def test_auto_reply_forwarded_in_is_still_captured():
    body = ("---------- Forwarded message ----------\n"
            "From: Ramesh Gupta <ramesh@clientco.com>\n"
            "To: Amiish Shah <amiish.shah@procamlogistics.com>\n"
            "Subject: Automatic reply: Your quotation\n\n"
            "I am out of office until 15 September.\n")
    extracted, kw = lead_of(msg("Fwd: Automatic reply: Your quotation", body,
                                EMPLOYEE))
    assert kw["email"] == "ramesh@clientco.com"
    assert extracted["skip_reason"].startswith("auto-reply")


# ── 3. The forwarding employee is never the contact ───────────────────
def test_forwarder_address_in_covering_note_does_not_win():
    body = ("Please create this lead. Reply to me at "
            "amiish.shah@procamlogistics.com if anything is unclear.\n\n"
            "---------- Forwarded message ----------\n"
            "From: Deepak Iyer <deepak@bhelindia.com>\n"
            "To: Amiish Shah <amiish.shah@procamlogistics.com>\n"
            "Subject: RFQ - generator transport Trichy to Chennai\n\n"
            "Kindly quote for moving 2 generators from Trichy to Chennai. "
            "Contact 9840011223.\n")
    _, kw = lead_of(msg("FW: RFQ - generator transport", body, EMPLOYEE,
                        "Amiish Shah"))
    assert kw["email"] == "deepak@bhelindia.com"


def test_unresolvable_forward_captures_the_lead_but_blanks_the_contact():
    """No original sender to find — the lead is still created, but the
    employee must NOT end up as the contact. Flagged for review instead."""
    body = ("Hi team, got a call from a prospect about a Mumbai to Pune "
            "trailer movement. Adding it here.\n\n"
            "-----Original Message-----\n"
            "Sent: Monday, 1 September 2026 09:14\n"
            "Subject: trailer requirement\n\n"
            "Please quote for 3 trailers.\n")
    _, kw = lead_of(msg("FW: trailer requirement", body, EMPLOYEE, "Amiish Shah"))
    assert kw["email"] is None, kw["email"]
    merged = json.loads(kw["email_extracted_json"])
    assert merged["needs_review"] is True
    # The body is preserved so a human can pick the prospect out of it.
    assert "trailer" in kw["notes"].lower()


def test_internal_mail_with_no_forward_structure_blanks_the_contact():
    """An employee writing their own mail in: captured, but no contact is
    invented from a stray address in a signature."""
    body = ("Team, reminder that the Q3 review is on Friday.\n\n"
            "Regards,\nAmiish\n"
            "Procam Logistics | partners: rates@somevendor.com\n")
    _, kw = lead_of(msg("Q3 review", body, EMPLOYEE, "Amiish Shah"))
    assert kw["email"] is None
    assert json.loads(kw["email_extracted_json"])["needs_review"] is True


def test_internal_to_internal_forward_blanks_the_contact():
    body = ("---------- Forwarded message ----------\n"
            "From: Ops Desk <ops@procamgroup.in>\n"
            "To: Amiish Shah <amiish.shah@procamlogistics.com>\n"
            "Subject: Truck placement for tomorrow\n\n"
            "Please confirm the trailer placement for the Mundra shipment.\n")
    _, kw = lead_of(msg("FW: Truck placement for tomorrow", body, EMPLOYEE))
    assert kw["email"] is None


def test_external_reply_chain_is_not_mistaken_for_a_forward():
    """A prospect replying in quotes a `From:` header — the quoted Procam
    employee must not be promoted over the actual sender."""
    body = ("Thanks, please quote for 3 trailers Mumbai to Pune.\n\n"
            "From: Amiish Shah <amiish.shah@procamlogistics.com>\n"
            "Sent: Monday, 1 September 2026 09:14\n"
            "Subject: Re: rates\n\n"
            "Happy to help, sharing our rate card.\n")
    extracted, kw = lead_of(msg("Re: rates", body,
                                "buyer@externaltrading.com", "Buyer"))
    assert extracted["is_forward"] is False
    assert kw["email"] == "buyer@externaltrading.com"


# ── 4. Company name comes from the organisation, not the mail relay ───
def test_company_ignores_bulk_sending_subdomains():
    from email_ingest.parser import _company_from_domain as C
    assert C("publishing@email.mckinsey.com") == "Mckinsey"
    assert C("executive_education@mail.exed.hbs.edu") == "Hbs"
    assert C("shrm.membership@e.shrm.org") == "Shrm"
    assert C("info@email.meetup.com") == "Meetup"


def test_company_handles_multi_part_public_suffixes():
    from email_ingest.parser import _company_from_domain as C
    assert C("raibin.wilson@savas.co.in") == "Savas"
    assert C("someone@example.ac.uk") == "Example"


def test_company_unchanged_for_ordinary_domains():
    from email_ingest.parser import _company_from_domain as C
    assert C("gaurav.tandon@jakson.com") == "Jakson"
    assert C("dhiraj.ubale@siemens.com") == "Siemens"
    assert C("mithilesh.mishra@hlag.com") == "Hlag"
    assert C("someone@gmail.com") == ""          # personal domains stay blank


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL  {name}: {e}")
            except Exception as e:                       # noqa: BLE001
                fails += 1
                print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{'FAILURES: %d' % fails if fails else 'all tests passed'}")
    sys.exit(1 if fails else 0)
