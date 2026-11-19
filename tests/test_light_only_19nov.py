"""CRM-UI-01: light mode only — no dark switch anywhere."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _files():
    for base in ('static', 'templates', 'app'):
        for dirpath, _dirs, names in os.walk(os.path.join(ROOT, base)):
            for name in names:
                if name.endswith(('.css', '.html', '.js')):
                    yield os.path.join(dirpath, name)


def test_nothing_follows_the_machine_into_dark():
    offenders = [p for p in _files()
                 if 'prefers-color-scheme' in open(p, errors='ignore').read()
                 and not p.endswith('tokens.css')]
    assert offenders == []
    tokens = open(os.path.join(ROOT, 'static', 'css', 'tokens.css')).read()
    assert not re.search(r'@media\s*\(\s*prefers-color-scheme', tokens)
    assert 'data-theme="dark"' not in tokens
    assert 'color-scheme: light only' in tokens
