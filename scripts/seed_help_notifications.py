"""
Put the notification user guide into /help.

The guide lives where people already look for help rather than in a
file they would have to be sent. Three articles, upserted by slug so
running this again edits them instead of adding a second copy.

    .venv/bin/python scripts/seed_help_notifications.py --check
    .venv/bin/python scripts/seed_help_notifications.py
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402
from app.models.help_content import HelpArticle           # noqa: E402

SECTION = 'Email and reports'

ARTICLES = [
    dict(
        slug='original-rfq-email',
        title='Getting the client’s original email',
        display_order=10,
        what_it_is=(
            'When an enquiry arrives at leads@procamgroup.in the CRM now '
            'keeps the message itself, not just a summary of it — the '
            'original email as a .eml file, and every document attached '
            'to it.'),
        when_to_use=(
            'Whenever you need the request as the client wrote it: to '
            'read the full wording, to see the drawings or the BOQ, or '
            'to forward it to a colleague or a vendor.'),
        how_to_use=(
            'Open the lead. Under the summary card you will see the '
            'attached files, and below them a red button reading '
            '"Original email (.eml)". Click it and the message downloads; '
            'double-click the downloaded file and it opens in Outlook '
            'with its sender, its date, its formatting and its '
            'attachments intact. From there you can forward or reply to '
            'it as if it had come to you directly.'),
        what_happens_next=(
            'Nothing is sent to the client by opening it. Downloading '
            'the original is a read — the client sees nothing.'),
        common_mistakes=(
            'If the button is not there, nothing was captured for that '
            'lead. That is normal for leads created before this was '
            'switched on, and for leads that did not arrive by email. '
            'Ask an administrator to run the back-fill rather than '
            'chasing a colleague for a forward.'),
    ),
    dict(
        slug='notification-preferences',
        title='Choosing what the CRM tells you',
        display_order=20,
        what_it_is=(
            'Your own settings for email from the CRM: whether you get '
            'it, how often, when it is allowed to arrive, and which '
            'individual notifications you would rather not have.'),
        when_to_use=(
            'When the CRM is telling you too much, at the wrong time, or '
            'not enough.'),
        how_to_use=(
            'Go to /me/notifications.\n\n'
            '• Email — turn it off and everything still arrives on '
            'the bell inside the CRM; you simply stop being emailed.\n'
            '• How often — "As it happens", or batched, where '
            'several notifications in a few minutes arrive as one email. '
            'Batched is worth it on a day when a lot is assigned at '
            'once.\n'
            '• Quiet hours — nothing lands between the hours you '
            'set (21:00 to 07:00 by default). Email raised in that window '
            'is held, not dropped, and arrives in the morning.\n'
            '• Reports — the daily brief and the weekly reports '
            'can each be turned off.\n'
            '• The list at the bottom turns off individual '
            'notifications.'),
        what_happens_next=(
            'The next notification follows the new settings. Nothing '
            'already queued is cancelled.'),
        common_mistakes=(
            'A few notifications ignore quiet hours on purpose — a '
            'quote already past its deadline, for instance. They are '
            'named on the page. Muting a notification silences the email, '
            'not the bell: the record still appears in your inbox inside '
            'the CRM.'),
    ),
    dict(
        slug='daily-and-weekly-reports',
        title='The daily brief, the review pack and the Friday freeze',
        display_order=30,
        what_it_is=(
            'Four scheduled emails. The daily action brief at 08:30, the '
            'end-of-day list of what is still open at 19:00, the review '
            'pack on Thursday afternoon, and the Friday freeze.'),
        when_to_use=(
            'The brief is the morning list. The Thursday pack is what to '
            'read before the review, so the meeting is not spent reading. '
            'The Friday freeze is the set of numbers the review works '
            'from.'),
        how_to_use=(
            'They arrive by email; you do not have to do anything. Every '
            'item links straight to the record. The brief only lists what '
            'you can see — it is built from your own board, so it can '
            'never show you somebody else’s records.'),
        what_happens_next=(
            'The Friday freeze writes the week’s figures down. From '
            'then on the review reads the written numbers, so a figure '
            'quoted in the meeting cannot change because somebody edited '
            'a lead while it was being discussed. The live board carries '
            'on moving; the frozen table does not.'),
        common_mistakes=(
            'A report with nothing in it is not sent, so no email on a '
            'quiet morning means an empty board, not a broken job. If a '
            'number in the pack differs from the screen, check which you '
            'are reading: "Open pipeline" is every live lead, "Needs '
            'attention" is only the ones the board is raising today.'),
    ),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        for spec in ARTICLES:
            row = HelpArticle.query.filter_by(slug=spec['slug']).first()
            verb = 'WOULD update' if row else 'WOULD create'
            if args.check:
                print(f'  {verb}  {spec["slug"]}  — {spec["title"]}')
                continue
            if row is None:
                row = HelpArticle(slug=spec['slug'])
                db.session.add(row)
            for key, value in spec.items():
                if key != 'slug':
                    setattr(row, key, value)
            row.section = SECTION
            row.role_visibility = []          # everyone
            row.is_active = True
            print(f'  + {spec["slug"]}')
        if args.check:
            print('\n== DRY-RUN — nothing written ==')
            return
        db.session.commit()
        print(f'\n{len(ARTICLES)} article(s) in "{SECTION}".')


if __name__ == '__main__':
    main()
