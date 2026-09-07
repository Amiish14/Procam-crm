"""Server-side URL prefixing.

The portal is served at /CRM behind nginx.  A redirect to a bare path —
`redirect('/companies')`, or a 302 to `/login` — resolves at the domain
root, where nginx knows nothing about it. The browser leaves the CRM
entirely, which is what took the Competitors page down.

Templates get `url_prefix` from a context processor; this is the same
thing for code that redirects.
"""
import os


def prefix():
    return (os.environ.get('URL_PREFIX') or '').rstrip('/')


def prefixed(path=''):
    """Prefix an internal path. Idempotent, and leaves absolute URLs and
    in-page anchors alone."""
    p = str(path or '').strip()
    if not p:
        return prefix() + '/'
    if p.startswith(('http://', 'https://', '//', '#', 'mailto:', 'tel:')):
        return p
    pre = prefix()
    if pre and (p == pre or p.startswith(pre + '/')):
        return p
    return pre + '/' + p.lstrip('/')


def login_url():
    return prefixed('/login')
