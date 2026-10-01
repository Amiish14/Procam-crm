# External Intelligence — Administrator's Guide

Release 5 (Groups H, I and J) adds four things: project intelligence
with a de-duplicated timeline, competitor activity by vertical and
service, a vendor register, and vessel and India port-call
intelligence.

This guide covers what each source needs before it can collect
anything, the exact status label it reports until then, how an
administrator configures a source, and how two articles about one
project become one timeline rather than two projects.

---

## 1. The honesty rule

**An adapter existing is not a subscription existing.**

Every adapter in `app/intel/adapters.py` is implemented in full — it
fetches, parses and emits items. Most of them are nevertheless inert,
because the data behind them is sold, licensed or permissioned. An
adapter in that state:

* returns **zero items**, never an example or a placeholder;
* returns a **reason** naming exactly what is missing;
* records the attempt in `intel_source_runs` with status
  `unavailable`, so "there was no news" and "there is no feed" can be
  told apart on a dashboard.

Nothing in this release seeds a sample project, vessel or port call.
An empty register is the correct, honest state of a CRM that has not
been given the data. The screens say so in words, rather than leaving
a blank table that reads as "there is nothing out there".

Confidence is recorded on every intelligence row, and it is one of:

| Code | Meaning |
|---|---|
| `confirmed` | A named, checkable source says so. Stamps `last_verified_at`. |
| `reported` | One public source, not yet corroborated. |
| `unverified` | Heard, not yet sourced. The only level that may be recorded with no source link. |

---

## 2. Adapter status reference

The status label below is the exact string the Sources page and
`GET /api/intel/sources` return. It is not paraphrased anywhere.

| Adapter key | Label | Status | External dependency |
|---|---|---|---|
| `manual` | Manual entry | **OPERATIONAL** | None. A person supplies the source reference and the facts, and signs for them. |
| `news_items` | CRM news inbox | **OPERATIONAL** | None beyond the trade bulletins the business already receives into the leads mailbox. Reads the existing `news_items` table; it can only surface what has already arrived. |
| `rss` | RSS / Atom feed | **IMPLEMENTED — EXTERNAL DATA SOURCE REQUIRED** | A feed URL. Trade and tender feeds covering Indian project announcements are commercial subscriptions, and the publisher must also permit automated reading. |
| `json_api` | JSON API | **IMPLEMENTED — CREDENTIAL REQUIRED** | A paid API subscription, plus its key in the environment variable the source config names. |
| `port_calls` | AIS / port-call feed | **IMPLEMENTED — EXTERNAL DATA SOURCE REQUIRED** | An AIS or port-community data subscription covering the Indian ports in question, licensed for CRM use, plus its key in the named environment variable. |
| `vendor_directory` | Vendor / vessel register | **IMPLEMENTED — EXTERNAL DATA SOURCE REQUIRED** | A vessel register or carrier directory subscription (fleet particulars, DWT, gear, lift capacity), licensed for internal CRM use. |

Two adapters are operational because they depend on nothing outside the
CRM. The other four are complete code waiting on a commercial decision.
Until that decision is taken, **every row in these registers is one a
colleague entered, with the source they entered it from** — which is a
perfectly good way to run the registers, and the Manual entry adapter
exists for exactly that.

### Where the labels come from

`app/intel/adapters.py` defines them as constants:

```python
STATUS_OPERATIONAL          = 'OPERATIONAL'
STATUS_SOURCE_REQUIRED      = 'IMPLEMENTED — EXTERNAL DATA SOURCE REQUIRED'
STATUS_CREDENTIAL_REQUIRED  = 'IMPLEMENTED — CREDENTIAL REQUIRED'
```

An adapter reports `OPERATIONAL` only when `available()` returns true —
that is, when every key in its `requires` list is configured and, for a
key ending `_env`, the named environment variable is actually set in the
running process. A half-configured source therefore reports the
dependency, not success.

---

## 3. Configuring a source

Sources live in the existing `public_sources` table. How an adapter runs
one lives in the new `intel_source_configs` table beside it, so the
competitor-monitoring stream's table keeps the shape it has.

**Screen:** Administration → Intelligence sources
(`/admin/intelligence/sources`). Requires the `admin.master` permission —
the same one that guards Master Data, Data Quality and Data Mapping.

To add a source:

1. **Name** it, and give its **URL**.
2. Choose the **adapter** (`rss`, `json_api`, `port_calls`,
   `vendor_directory`, `manual`, `news_items`).
3. Choose what it **feeds**: `project`, `competitor`, `vendor` or
   `port_call`.
4. Fill in the **config**, as JSON. See the table below.
5. Tick **Enabled** only once the config is complete. A disabled source
   is never polled.
6. Press **Save source**. The screen immediately shows the adapter's
   status and, if it cannot run, the reason.
7. Press **Run now** against the saved source to collect once and see
   the outcome in *Recent runs*.

### Config keys

| Adapter | Key | What it holds |
|---|---|---|
| `rss` | `feed_url` | The feed address. |
| `json_api`, `port_calls`, `vendor_directory` | `endpoint` | The request URL. |
| | `api_key_env` | **The NAME of an environment variable**, e.g. `INTEL_PROJECTS_API_KEY`. Never the key itself. |
| | `auth_header` | Optional. Defaults to `Authorization`. |
| | `auth_scheme` | Optional. Defaults to `Bearer`; set it to `""` for providers that want a bare header value. |
| | `since_param` | Optional query parameter for incremental collection. |
| | `items_path` | Optional dotted path to the list in the response, e.g. `data.results`. |
| | `field_map` | Optional map of our field names onto the provider's, e.g. `{"title": "headline", "published": "pub_date"}`. |

### Credentials are never stored in the database

The API refuses a config containing `api_key`, `token`, `password` or
`secret` and tells you where the value belongs. Put the value in the
server environment (the systemd unit's `Environment=` or the `.env`
file the service reads), and name the variable in `api_key_env`.

A config that names a variable the running process does not have reports
`IMPLEMENTED — CREDENTIAL REQUIRED` and names the missing variable, so
the fix is unambiguous. Restart the service after adding a variable.

---

## 4. De-duplication — one project, one timeline

The failure this prevents: three bulletins report the same refinery
expansion in one week and the CRM ends up with three projects, each with
one update, none of which looks important.

### The key

Every sighting is reduced to three normalised parts, stored on
`intel_project_facts` as `name_key`, `owner_key` and `location_key`:

* **name** — lower-cased, punctuation stripped, and the words that carry
  no identity removed (`project`, `plant`, `limited`, `pvt`, `phase`,
  `expansion`, `india`, …). Roman numerals become digits, so "Phase-II"
  and "phase 2" agree. Simple plurals are reduced, so "petrochemicals"
  and "petrochemical" agree.
* **owner** — the same, with company suffixes (`limited`, `ltd`,
  `group`, `industries`, …) removed.
* **location** — the same, with `dist`, `district`, `taluka`, `village`
  and similar removed.

The parts are sorted word sets, so word order does not matter.

### The match

An incoming item matches an existing project when:

* the full `name|owner|location` key matches exactly; **or**
* the **name** keys match and neither the owner nor the location
  *contradicts*.

"Contradicts" is deliberately narrow. A blank never contradicts — a
bulletin that did not name the owner is not evidence of a different
owner. Otherwise one value must contain the other, so "Jamnagar" and
"Jamnagar Gujarat" agree while "Mundra" and "Dahej" do not.

On a match the item becomes a `ProjectUpdate` on the existing project and
`intel_project_facts.update_count` goes up. It never becomes a second
project.

Two projects with the same name in the same place but **different named
owners** stay two projects. So do Phase I and Phase II: a phase number is
identity, not noise.

### Seeing the same article twice

`intel_raw_items.fingerprint` is unique. It is built from the source's
own identifier when it gives one, else the URL, else the title and
publication date — deliberately **not** the body, because a publisher who
corrects a typo has not published a second story. Re-reading a feed
therefore produces `duplicate`, and nothing is written.

### What a later sighting may change

* A **blank** field is filled in.
* A field already held is replaced only by a sighting of **equal or
  greater confidence**. An unverified rumour cannot overwrite a
  confirmed fact — but it is still added to the timeline, so it is
  rejected in the open rather than hidden.
* **Equipment suppliers accumulate.** A second article naming one more
  supplier is an addition, not a correction.
* The **stage** only ever advances, along the lifecycle
  announced → land → clearance → EPC → equipment order → supplier →
  manufacturing → shipment → commissioning. Each maps onto the project
  stage vocabulary the pre-sales screens already use, and a change
  writes a `project_stage_history` row.
* The **keys** are rewritten only when they gain information, so a later
  article that omits the owner cannot erase it and split the timeline.

### Fixing a wrong merge or a wrong split

A wrong **split** (two rows that are one project) is corrected by
dismissing the duplicate with *Mark not relevant* and the reason "already
held as project #N". It is archived rather than deleted, so the next
bulletin about it matches the same key and gets the same answer.

A wrong **merge** is rarer by design — the normaliser is deliberately
conservative, because a missed merge shows two projects that somebody can
fix, while a wrong merge hides one project inside another. If one does
happen, capture the mis-filed item again with a distinguishing owner or
location in the payload.

---

## 5. What is captured per project

The §12.1 fields live on `intel_project_facts`: owner/group, industry,
location, value, announcement date, stage, land status, clearances, EPC,
PMC, technology provider, equipment suppliers, expected logistics needs,
timeline, source reference, latest update, assigned BD owner, vertical
and opportunity status.

Every record also keeps the **source reference**, **source URL**,
**publication date**, **captured date**, **confidence**, **last verified
at/by** and the **extracted entities** taken from the source.

**A blank means nobody has said — not that the answer is no.** Nothing is
inferred to fill a field in, and the detail page says this above the
table.

### Actions, all audited

| Action | Audit event | Notes |
|---|---|---|
| Create lead | `intel.project.create_lead` | Goes through `app/services/lead_assignment.py`, so the assignment history row and the notifications happen exactly as they do everywhere else. A project that already made a lead is refused rather than making a twin. |
| Assign owner | `intel.project.assign` | Sets the project's PIC and the fact's BD owner together. |
| Follow | `intel.project.follow` | For people who do not own it but want to hear. |
| Add account | `intel.project.add_account` | Through the existing pre-sales linking service. |
| Add contact | `intel.project.add_contact` | As above. |
| Mark not relevant | `intel.project.not_relevant` | A reason is required; it is what stops the next bulletin raising it again. |

### Who sees what

A project **with a BD owner** is visible per the Access Matrix, exactly
like a lead. A project with **no owner** is visible to anyone signed in:
it is public-source intelligence about somebody else's plant, not a
customer record, and hiding unowned projects is how they stay unowned.

---

## 6. Competitor activity

`intel_competitor_activity` adds the four axes the existing
`competitor_intelligence` table has no columns for: **vertical**,
**service**, **industry** and **geography**, plus an activity type.

Every vocabulary is a Master Data list. A value Master Data does not
hold is **refused**, with a message saying to add it there first — it
never becomes free text that no filter will find again.

Anything recorded as stronger than `unverified` must name its source.

`GET /api/intel/competitor-activity/summary?axis=service` counts entries
per value of one axis, for the summary tiles.

---

## 7. Vendors, vessels and port calls

| Register | Table | Vocabulary |
|---|---|---|
| Vendors | `vendor_profiles` | `vendor_category` |
| Vessel capability | `vessels` | `vessel_type` |
| India port calls | `vessel_port_calls` | `india_port` |

The vendor categories seeded are: shipping line; MPV/breakbulk owner or
operator; RoRo owner or operator; heavy-lift owner or operator; container
line; port agent; vessel agent; transporter; heavy-haul operator; crane
hire; SPMT operator; rigging contractor; warehouse; customs partner;
airline; air cargo agent; overseas partner.

The ports seeded are Mumbai (INBOM), JNPT/Nhava Sheva (INNSA), Chennai
(INMAA), Paradip (INPRT), Mundra (INMUN), Kandla/Deendayal (INIXY),
Kolkata (INCCU) and Haldia (INHAL).

**All of these are Master Data rows, not code.** Adding a ninth port, or
a category such as "project forwarder", is done in
Administration → Master Data and takes effect in the capture forms, the
filters and the exports at the same moment. Retiring a value removes it
everywhere at once.

A vessel search by minimum lift **excludes** vessels whose maximum lift
nobody has recorded: "we do not know" is not "it can".

---

## 8. Operating it

### Running collection

* **By hand:** Administration → Intelligence sources → *Run now*.
* **On a timer:** call `app.intel.projects.run_all(purpose='project')`
  inside an application context from a systemd timer. Every run, whether
  it collected anything or not, writes an `intel_source_runs` row.

### Monitoring

Watch `intel_source_runs` for:

* `status = 'unavailable'` — a source is configured but cannot run.
  The reason names the missing feed URL or environment variable.
* `status = 'error'` or an `ok` run whose reason names an exception —
  the source is configured but the other end is down or has changed.
* Nothing at all for a source that should be on a timer — the timer is
  not running.

### Schema

The migration is `scripts/2026_10_09_intelligence.py`. It adds nine
tables, no columns, and alters nothing:

```
python scripts/2026_10_09_intelligence.py --check      # dry-run
python scripts/2026_10_09_intelligence.py              # apply
python scripts/2026_10_09_intelligence.py --down --yes # roll back
```

It also registers six Master Data lists and seeds their values. Rolling
back drops the nine tables but **leaves the Master Data lists alone**: an
administrator may have edited them, and dropping a list somebody curated
is not a rollback. The projects themselves and their timelines survive a
rollback too — they live in `projects` and `project_updates`, which this
script never touches.
