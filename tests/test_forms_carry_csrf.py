"""Every POST form in the CRM carries a CSRF token.

CSRFProtect is on application-wide. A form without a token is refused
before its handler runs, with a 400 HTML page the screen cannot read —
so the button silently does nothing. This repository has shipped that
bug three times now: the Academy self-checks, the Workbench actions,
and then the notification preferences and Email Health buttons, which
went to production dead last night.

Unit tests do not catch it, because test modules turn CSRF off to test
anything else. So this one does not post: it reads the templates and
insists that every POST form has the hidden field. A template is a
cheap thing to check and this is the only check that would have caught
all three.
"""
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, 'templates')

#: Forms that legitimately carry no token. `login` has none to give —
#: the session does not exist yet — and it fetches one from the cookie
#: in JavaScript instead.
EXEMPT = {'login.html'}

_FORM = re.compile(r'<form\b[^>]*>', re.I)


def _posting(tag):
    return re.search(r'method\s*=\s*["\']post["\']', tag, re.I) is not None


def _templates():
    for base, _dirs, files in os.walk(_TEMPLATES):
        for name in files:
            if name.endswith('.html') and name not in EXEMPT:
                yield os.path.join(base, name)


def test_every_post_form_has_a_csrf_field():
    offenders = []
    for path in sorted(_templates()):
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        for match in _FORM.finditer(text):
            if not _posting(match.group(0)):
                continue
            # The token must be inside this form, before it closes.
            end = text.lower().find('</form>', match.end())
            body = text[match.start():end if end != -1 else len(text)]
            if 'csrf_token' not in body:
                offenders.append(
                    f'{os.path.relpath(path, _ROOT)}: '
                    f'{match.group(0)[:90]}')
    assert not offenders, (
        'these POST forms would be refused by CSRFProtect before their '
        'handler runs, so the button does nothing:\n  '
        + '\n  '.join(offenders))
