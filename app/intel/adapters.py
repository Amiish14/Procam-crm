"""
Where intelligence comes from — and whether that source actually works.

An adapter is the only way an external item enters the CRM. Each one
answers three questions:

    requires      what an administrator must supply before it can run
    available()   whether that has been supplied — (ok, reason)
    fetch(since)  the items, or an empty list and `last_reason`

The honesty rule
----------------
Writing an adapter does not create a subscription. Most of the sources
this release anticipates — project-announcement feeds, tender portals,
AIS and port-call data — are paid, licensed or permissioned. Those
adapters are implemented in full and then report themselves as

    IMPLEMENTED — EXTERNAL DATA SOURCE REQUIRED
    IMPLEMENTED — CREDENTIAL REQUIRED

until the subscription or the key exists. A disabled adapter returns
**zero items** and a reason. It never returns an example, a placeholder
or a demonstration row: on a screen those are indistinguishable from
intelligence, and somebody would act on one.

Two adapters are genuinely operational, because they depend on nothing
outside the CRM: `manual`, where a person pastes the source link and the
facts, and `news_items`, which reads the news the existing email ingest
has already captured into the CRM's own `news_items` table.

Credentials
-----------
No secret is stored in the database. A source's config names the
environment variable that carries the key (``{"api_key_env":
"INTEL_PROJECTS_API_KEY"}``) and the adapter reads the process
environment. A config that names a variable the process does not have is
reported as not available, by name, so the fix is obvious.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime

log = logging.getLogger(__name__)

# ── status labels, used verbatim in the UI and the guide ─────────────
STATUS_OPERATIONAL = 'OPERATIONAL'
STATUS_SOURCE_REQUIRED = 'IMPLEMENTED — EXTERNAL DATA SOURCE REQUIRED'
STATUS_CREDENTIAL_REQUIRED = 'IMPLEMENTED — CREDENTIAL REQUIRED'

#: Network calls are bounded so a dead feed cannot hold a worker.
HTTP_TIMEOUT = 20
#: Most feeds we would ever subscribe to return far fewer than this; the
#: cap is here so a misconfigured endpoint cannot flood the raw table.
MAX_ITEMS = 200


@dataclass
class RawItem:
    """One item as the adapter saw it, before anything is made of it.

    `payload` carries whatever structure the source offered — owner,
    location, value, stage. Nothing is guessed to fill it in: a field the
    source did not state stays absent, and the screens show it blank.
    """
    source_key: str
    title: str
    external_id: str = ''
    url: str = ''
    body: str = ''
    publication_date: date | None = None
    payload: dict = field(default_factory=dict)
    confidence: str = 'reported'
    source_id: int | None = None
    source_label: str = ''

    def fingerprint(self) -> str:
        """What makes this item the same item on the next run.

        The source's own id when it gives one, else the URL, else the
        title and publication date. Deliberately not the body: a
        publisher who corrects a typo has not published a second story.
        """
        basis = (self.external_id or self.url
                 or f'{self.title}|{self.publication_date or ""}')
        return hashlib.sha256(
            f'{self.source_key}|{basis}'.strip().lower().encode('utf-8')
        ).hexdigest()


class SourceAdapter:
    """Base class. Subclasses set `key`, `label`, `requires` and fetch."""

    key = ''
    label = ''
    #: Config keys an administrator must fill in. A key ending `_env`
    #: names an environment variable rather than holding a value.
    requires: tuple = ()
    #: The status label this adapter reports when it cannot run.
    unavailable_status = STATUS_SOURCE_REQUIRED
    #: What has to be bought, licensed or granted. Shown to admins.
    dependency = ''
    #: project | competitor | vendor | port_call — what it can feed.
    purposes: tuple = ('project',)

    def __init__(self, config=None, source=None):
        self.config = dict(config or {})
        #: The `public_sources` row this adapter was configured against.
        self.source = source
        #: Why the last fetch returned nothing. Never left stale: every
        #: fetch sets it, to '' when it succeeded.
        self.last_reason = ''

    # ── availability ─────────────────────────────────────────────────
    def missing(self):
        """Config keys that are not usable yet, with why."""
        gaps = []
        for key in self.requires:
            value = (self.config.get(key) or '').strip() if isinstance(
                self.config.get(key), str) else self.config.get(key)
            if not value:
                gaps.append(f'{key} is not configured')
            elif key.endswith('_env') and not os.environ.get(str(value), ''):
                gaps.append(f'the environment variable {value} is not set')
        return gaps

    def available(self):
        """(bool, reason). The reason is empty only when it can run."""
        gaps = self.missing()
        if gaps:
            return False, (f'{self.unavailable_status}: {self.label} cannot '
                           f'run because ' + '; and '.join(gaps) + '.')
        return True, ''

    def status(self):
        ok, _reason = self.available()
        return STATUS_OPERATIONAL if ok else self.unavailable_status

    # ── collection ───────────────────────────────────────────────────
    def fetch(self, since=None):
        """Items published since `since`. [] and `last_reason` when not.

        Subclasses override `_collect`; this wrapper is what guarantees
        that an unavailable or broken source yields nothing rather than
        an exception or an invention.
        """
        ok, reason = self.available()
        if not ok:
            self.last_reason = reason
            return []
        try:
            items = list(self._collect(since) or [])[:MAX_ITEMS]
        except Exception as exc:
            # A source that is down is not a source that said nothing.
            # Both give zero items; only one of them is worth an alert.
            log.warning('intel adapter %s failed: %s', self.key, exc)
            self.last_reason = (f'{self.label} could not be read: '
                                f'{type(exc).__name__}: {exc}')
            return []
        self.last_reason = '' if items else (
            f'{self.label} returned nothing published since '
            f'{since or "the beginning"}.')
        return items

    def _collect(self, since):                      # pragma: no cover
        raise NotImplementedError

    # ── helpers for subclasses ───────────────────────────────────────
    def _secret(self, config_key):
        return os.environ.get(str(self.config.get(config_key) or ''), '')

    def _get(self, url, headers=None):
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()

    def describe(self):
        ok, reason = self.available()
        return {
            'key': self.key, 'label': self.label,
            'requires': list(self.requires),
            'purposes': list(self.purposes),
            'dependency': self.dependency,
            'status': self.status(),
            'available': ok,
            'reason': reason,
        }


# ─────────────────────────────────────────────────────────────────────
# Operational: depend on nothing outside the CRM
# ─────────────────────────────────────────────────────────────────────
class ManualAdapter(SourceAdapter):
    """A person pastes the source link and the facts.

    Always available, and the only adapter that is guaranteed to be:
    somebody reading a trade magazine is a source that needs no licence.
    `fetch` returns nothing because a person supplies items as they find
    them; `item()` builds the RawItem the ingest then handles exactly as
    it would an automated one, so manual entries de-duplicate against
    feed entries about the same project.
    """

    key = 'manual'
    label = 'Manual entry'
    requires = ()
    dependency = ('None. A person supplies the source reference and the '
                  'facts, and signs for them.')
    purposes = ('project', 'competitor', 'vendor', 'port_call')

    def available(self):
        return True, ''

    def status(self):
        return STATUS_OPERATIONAL

    def _collect(self, since):
        # Nothing to poll: manual intelligence arrives through the UI.
        return []

    @staticmethod
    def item(*, title, body='', url='', publication_date=None, payload=None,
             confidence='reported', source_label='Manual entry',
             external_id=''):
        """Turn what a person typed into the same shape a feed produces."""
        return RawItem(
            source_key='manual', title=(title or '').strip(),
            body=(body or '').strip(), url=(url or '').strip(),
            publication_date=publication_date, payload=dict(payload or {}),
            confidence=confidence or 'reported',
            source_label=source_label or 'Manual entry',
            external_id=(external_id or '').strip())


class NewsItemAdapter(SourceAdapter):
    """The news the CRM already holds.

    `news_items` is filled by the existing email ingest from the trade
    bulletins the business already subscribes to. Reading it needs no new
    subscription, so this adapter is operational — but it can only ever
    surface what has already arrived. It invents nothing and, in a
    database with no news, returns nothing.
    """

    key = 'news_items'
    label = 'CRM news inbox'
    requires = ()
    dependency = ('None beyond the trade bulletins the business already '
                  'receives into the leads mailbox.')
    purposes = ('project',)

    def available(self):
        return True, ''

    def status(self):
        return STATUS_OPERATIONAL

    def _collect(self, since):
        from app import NewsItem

        q = NewsItem.query.filter(NewsItem.status != 'deleted')
        if since:
            cutoff = since.date() if isinstance(since, datetime) else since
            q = q.filter(NewsItem.published_date >= cutoff)
        rows = q.order_by(NewsItem.id.desc()).limit(MAX_ITEMS).all()
        return [RawItem(
            source_key=self.key, external_id=f'news:{row.id}',
            title=row.title or '', body=row.summary or '',
            url=row.url or '', publication_date=row.published_date,
            payload={'industry': row.category or ''},
            # The bulletin said it; nobody has checked it.
            confidence='reported',
            source_label=row.source or 'CRM news inbox') for row in rows]


# ─────────────────────────────────────────────────────────────────────
# Implemented, inert without an external dependency
# ─────────────────────────────────────────────────────────────────────
_TAG = re.compile(r'<[^>]+>')
_WS = re.compile(r'\s+')


def _text(node):
    if node is None:
        return ''
    return _WS.sub(' ', _TAG.sub(' ', node.text or '')).strip()


class RssAdapter(SourceAdapter):
    """A generic RSS or Atom feed.

    Structurally complete: it fetches, parses both dialects and emits
    RawItems. It is inert until an administrator supplies a feed URL,
    and most feeds worth having — project-announcement and tender
    services — are paid, which is why the status says so.
    """

    key = 'rss'
    label = 'RSS / Atom feed'
    requires = ('feed_url',)
    unavailable_status = STATUS_SOURCE_REQUIRED
    dependency = ('A feed URL. Trade and tender feeds that cover Indian '
                  'project announcements are commercial subscriptions; '
                  'the publisher must also permit automated reading.')
    purposes = ('project', 'competitor')

    #: Feed date formats, most specific first.
    _DATE_FORMATS = ('%a, %d %b %Y %H:%M:%S %z', '%a, %d %b %Y %H:%M:%S %Z',
                     '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ',
                     '%Y-%m-%d %H:%M:%S', '%Y-%m-%d')

    @classmethod
    def _parse_date(cls, raw):
        raw = (raw or '').strip()
        for fmt in cls._DATE_FORMATS:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    def _collect(self, since):
        import xml.etree.ElementTree as ET

        raw = self._get(self.config['feed_url'],
                        headers={'User-Agent': 'Procam-CRM-intel/1.0'})
        root = ET.fromstring(raw)
        ns = {'a': 'http://www.w3.org/2005/Atom'}
        entries = root.findall('.//item') or root.findall('.//a:entry', ns)

        out = []
        for entry in entries:
            title = _text(entry.find('title')) or _text(entry.find('a:title', ns))
            link = _text(entry.find('link'))
            if not link:
                node = entry.find('a:link', ns)
                link = (node.get('href') if node is not None else '') or ''
            body = (_text(entry.find('description'))
                    or _text(entry.find('a:summary', ns))
                    or _text(entry.find('a:content', ns)))
            published = self._parse_date(
                _text(entry.find('pubDate'))
                or _text(entry.find('a:published', ns))
                or _text(entry.find('a:updated', ns)))
            guid = (_text(entry.find('guid'))
                    or _text(entry.find('a:id', ns)) or link)
            if not title:
                continue
            if since and published:
                cutoff = since.date() if isinstance(since, datetime) else since
                if published < cutoff:
                    continue
            out.append(RawItem(
                source_key=self.key, external_id=guid, title=title,
                body=body, url=link, publication_date=published,
                confidence='reported',
                source_id=getattr(self.source, 'id', None),
                source_label=(getattr(self.source, 'name', '')
                              or self.config.get('feed_url', ''))))
        return out


class JsonApiAdapter(SourceAdapter):
    """A generic JSON API behind a key.

    `items_path` is a dotted path to the list in the response, and
    `field_map` maps our field names onto the provider's, so a new
    provider is configuration rather than code.
    """

    key = 'json_api'
    label = 'JSON API'
    requires = ('endpoint', 'api_key_env')
    unavailable_status = STATUS_CREDENTIAL_REQUIRED
    dependency = ('A paid API subscription, plus its key held in the '
                  'environment variable the source config names. No key '
                  'is ever stored in the database.')
    purposes = ('project', 'competitor', 'vendor', 'port_call')

    #: Sent as `Authorization: <scheme> <key>` unless the config says
    #: otherwise; some providers want a bare header instead.
    DEFAULT_SCHEME = 'Bearer'

    def _headers(self):
        key = self._secret('api_key_env')
        header = self.config.get('auth_header') or 'Authorization'
        scheme = self.config.get('auth_scheme', self.DEFAULT_SCHEME)
        value = f'{scheme} {key}'.strip() if scheme else key
        return {header: value, 'Accept': 'application/json',
                'User-Agent': 'Procam-CRM-intel/1.0'}

    @staticmethod
    def _dig(data, path):
        for part in [p for p in (path or '').split('.') if p]:
            if isinstance(data, dict):
                data = data.get(part)
            else:
                return None
        return data

    def _url(self, since):
        url = self.config['endpoint']
        param = self.config.get('since_param')
        if since and param:
            sep = '&' if '?' in url else '?'
            url = f'{url}{sep}{param}={since}'
        return url

    def _collect(self, since):
        body = self._get(self._url(since), headers=self._headers())
        data = json.loads(body.decode('utf-8', 'replace'))
        rows = self._dig(data, self.config.get('items_path', '')) \
            if self.config.get('items_path') else data
        if isinstance(rows, dict):
            rows = rows.get('items') or rows.get('results') or []
        if not isinstance(rows, list):
            raise ValueError('the response did not contain a list of items')

        fmap = dict(self.config.get('field_map') or {})
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            def pick(name, default=''):
                return row.get(fmap.get(name, name), default)
            title = str(pick('title') or '').strip()
            if not title:
                continue
            out.append(RawItem(
                source_key=self.key,
                external_id=str(pick('id') or ''), title=title,
                body=str(pick('summary') or pick('description') or ''),
                url=str(pick('url') or ''),
                publication_date=RssAdapter._parse_date(
                    str(pick('published') or '')),
                payload={k: row.get(v) for k, v in fmap.items()
                         if v in row and k not in
                         ('title', 'id', 'summary', 'url', 'published')},
                confidence=str(pick('confidence') or 'reported'),
                source_id=getattr(self.source, 'id', None),
                source_label=(getattr(self.source, 'name', '')
                              or self.config.get('endpoint', ''))))
        return out


class PortCallAdapter(JsonApiAdapter):
    """AIS / port-call data for Indian ports.

    Same mechanics as the JSON API adapter; separate because the
    dependency is different in kind. AIS and port-community data are
    licensed per user and per port, and redistribution is usually
    forbidden, so this is the adapter most likely to stay inert.
    """

    key = 'port_calls'
    label = 'AIS / port-call feed'
    requires = ('endpoint', 'api_key_env')
    unavailable_status = STATUS_SOURCE_REQUIRED
    dependency = ('An AIS or port-community data subscription covering '
                  'the Indian ports in question, licensed for CRM use, '
                  'plus its key in the named environment variable.')
    purposes = ('port_call',)


class VendorDirectoryAdapter(JsonApiAdapter):
    """A carrier, operator or agency directory.

    Vessel particulars and fleet lists come from register and directory
    products that are licensed, not public, even where a web page shows
    one ship at a time.
    """

    key = 'vendor_directory'
    label = 'Vendor / vessel register'
    requires = ('endpoint', 'api_key_env')
    unavailable_status = STATUS_SOURCE_REQUIRED
    dependency = ('A vessel register or carrier directory subscription '
                  '(fleet particulars, DWT, gear and lift capacity), '
                  'licensed for internal CRM use.')
    purposes = ('vendor',)


# ── registry ─────────────────────────────────────────────────────────
ADAPTERS = (ManualAdapter, NewsItemAdapter, RssAdapter, JsonApiAdapter,
            PortCallAdapter, VendorDirectoryAdapter)

_BY_KEY = {cls.key: cls for cls in ADAPTERS}


def adapter_keys():
    return [cls.key for cls in ADAPTERS]


def get(key, config=None, source=None):
    """An adapter instance, or None when the key is not one we have."""
    cls = _BY_KEY.get((key or '').strip())
    return None if cls is None else cls(config=config, source=source)


def configured():
    """Every enabled source, as (config row, adapter) pairs.

    A source whose adapter key no longer exists is skipped rather than
    crashing the page — the Sources screen lists it as unrecognised.
    """
    from app.models.intel import IntelSourceConfig
    from app.models.public_source import PublicSource

    out = []
    rows = IntelSourceConfig.query.filter_by(is_enabled=True).all()
    for cfg in rows:
        source = PublicSource.query.get(cfg.source_id)
        if source is not None and not source.is_active:
            continue
        adapter = get(cfg.adapter_key, config=cfg.config, source=source)
        if adapter is not None:
            out.append((cfg, adapter))
    return out


def status_report():
    """What is actually running — the answer the Sources page shows.

    One entry per adapter: its status label, what it needs, and how many
    sources an administrator has pointed at it. `manual` and
    `news_items` are operational; the rest report their dependency until
    it exists.
    """
    from app.models.intel import IntelSourceConfig

    counts, enabled = {}, {}
    try:
        for cfg in IntelSourceConfig.query.all():
            counts[cfg.adapter_key] = counts.get(cfg.adapter_key, 0) + 1
            if cfg.is_enabled:
                enabled[cfg.adapter_key] = enabled.get(cfg.adapter_key, 0) + 1
    except Exception:
        # Before the migration has run there are no configs; that is not
        # an error, it is an empty answer.
        counts, enabled = {}, {}

    report = []
    for cls in ADAPTERS:
        probe = cls(config={})
        ok, reason = probe.available()
        report.append({
            'key': cls.key, 'label': cls.label,
            'requires': list(cls.requires),
            'purposes': list(cls.purposes),
            'dependency': cls.dependency,
            # Unconfigured instances of a credentialled adapter report
            # the dependency; a configured one is re-checked per source.
            'status': STATUS_OPERATIONAL if ok else cls.unavailable_status,
            'available_unconfigured': ok,
            'reason': reason,
            'sources_configured': counts.get(cls.key, 0),
            'sources_enabled': enabled.get(cls.key, 0),
        })
    return report
