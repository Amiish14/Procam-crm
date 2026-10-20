# How work gets into the TMS: the two intake formats

Two ways a job exists in this system. A freight job arrives as a
**Handover Pack** — one file, one shipment, previewed by a person and then
imported. Every other job is **typed**, project first, then the job, then
the client order.

This is a description of what the code does today, not a specification of
what it should do. Where the two differ the difference is written down
rather than tidied away.

Status, as of this writing: `FF_JOBS` is off in production, the Handover
Pack importer has no screen, and `job_ff_detail` and `ff_handover_packs`
both hold **0 rows**. Part A is therefore how the importer *will* behave on
the day it is switched on, verified by running it, not how anything has
behaved so far. Part B is in use every day.

---

# Part A — the FF Handover Pack

## How a pack arrives

Two formats, one reader. `app/services/ff_handover.py:read()` turns either
into the same dictionary, so nothing after that point knows which arrived.

| Format | Detected by | Shape |
|---|---|---|
| JSON | anything that is not a workbook | one object, or a list whose **first element** is used and the rest silently dropped |
| Excel | filename ends `.xlsx` / `.xlsm`, or the bytes start `PK` | four sheets, two columns: field name in column A, value in column B |

The Excel reader walks **every sheet in the workbook** and keeps any row
whose column A is a field name it knows. Sheet names are not checked. Two
consequences worth knowing before you fill one in:

- Where the same field appears on more than one sheet, **the last sheet
  wins**. The shipped template carries four filled `Example …` sheets after
  the blank ones, so the template read as-is imports as the last example,
  `AIR IMPORT` / `HP-2026-0104`. The front sheet says to copy one example
  and delete the others; that instruction is the only thing enforcing it.
- A blank cell is absent, not empty. A field you clear does not overwrite
  anything; it simply is not in the pack.

A date cell is converted to `YYYY-MM-DD`. Everything else arrives as
whatever openpyxl read — a number stays a number, text stays text.

## The pack is identified by its bytes

`fingerprint()` is the SHA-256 of the file as received. The same file sent
twice is refused at preview and again at import. A pack that was **edited**
and re-sent is a different fingerprint and is allowed through — that is
deliberate, a corrected pack has to be importable.

Uniqueness is enforced by a partial index on `ff_handover_packs`, over
`status = 'IMPORTED'` only, so a duplicate can still be *previewed* — the
step whose whole job is to tell somebody it is a duplicate.

## Two presses, never one

| Step | Function | Creates |
|---|---|---|
| Preview | `preview(raw, filename, business, user)` | one `ff_handover_packs` row, `status='PREVIEW'`, with the problems found written to `notes`. Nothing else. |
| Import | `import_pack(pack, fields, business=, user=, client=)` | the project, job, freight detail and client order. Returns `(ok, message)` and **refuses rather than half-creates**. |

The caller commits. `import_pack` only flushes.

What is stored in `payload` is the **fields that were read**, as JSON, not
the file — a workbook is not text and storing its bytes breaks on the first
non-UTF-8 byte. The fingerprint is still of the original bytes, so the same
file is recognised whichever format it came in.

## Every field

Required means the import refuses without it. Type is what the reader does
to the value, not what the spreadsheet holds.

### Sheet `Shipment`

| Field | Type | Required | Allowed values | Lands in |
|---|---|---|---|---|
| `reference` | text, whitespace-normalised | **yes** | any | `ff_handover_packs.reference` (80), and builds `projects.code`, `jobs.pcn_job_no`, `client_pos.po_number` |
| `customer` | text | **yes** | must already match a `clients.name`, case-insensitive | nothing directly — it *selects* the client; a pack never creates one |
| `customer_code` | text | no | any | **nowhere** — read, kept in `payload`, never stored |
| `mode` | text, upper-cased | **yes** | `SEA`, `AIR` | `job_ff_detail.mode`, and into `projects.name` |
| `direction` | text, upper-cased | **yes** | `EXPORT`, `IMPORT`, `CROSS_TRADE` | `job_ff_detail.direction`, and into `projects.name` |
| `incoterm` | text | no | any — not validated against Incoterms | `job_ff_detail.incoterm` (12) |
| `incoterm_place` | text | no | any | `job_ff_detail.incoterm_place` (160) |
| `quote_reference` | text | no | any | **nowhere** |
| `tally_po_number` | text | no | any | `client_pos.client_po_number` — kept beside Procam's own number, never used as it |
| `sell_value` | number | no | any | `client_pos.po_value`; **0 when absent** |
| `currency` | text | no | any | **nowhere** — see the gaps at the end |

### Sheet `Parties`

| Field | Type | Required | Allowed values | Lands in |
|---|---|---|---|---|
| `shipper_name` | text | no | any | `job_ff_detail.shipper_name` (200) |
| `shipper_address` | text | no | any | `job_ff_detail.shipper_address` (text) |
| `consignee_name` | text | no | any | `job_ff_detail.consignee_name` (200) |
| `consignee_address` | text | no | any | `job_ff_detail.consignee_address` |
| `notify_name` | text | no | any | `job_ff_detail.notify_name` (200) |
| `notify_address` | text | no | any | `job_ff_detail.notify_address` |
| `carrier_name` | text | no | any | `job_ff_detail.carrier_name` (200) — the *name*; `carrier_vendor_id` is left null and linked by hand later |
| `overseas_agent_name` | text | no | any | `job_ff_detail.overseas_agent_name` (200) — same, `overseas_agent_vendor_id` stays null |
| `cha_mode` | text, upper-cased | no | `OWN`, `OUTSOURCED` — **not validated** | `job_ff_detail.cha_mode` (16); `cha_vendor_id` stays null |

Procam holds its own CHA licence at Chennai and Mumbai, so `OWN` is a
choice per job rather than a lookup from the port.

### Sheet `Route`

| Field | Type | Required | Allowed values | Lands in |
|---|---|---|---|---|
| `pol_code` | text | no | UN/LOCODE for sea (`INMAA`), IATA for air (`BOM`) — not validated | `job_ff_detail.pol_code` (10) |
| `pol_name` | text | no | any | `job_ff_detail.pol_name` (120), and into `jobs.name` |
| `pod_code` | text | no | as above | `job_ff_detail.pod_code` (10) |
| `pod_name` | text | no | any | `job_ff_detail.pod_name` (120), and into `jobs.name` |
| `etd` | date | no | `YYYY-MM-DD`; anything unparseable becomes **null, silently** | `job_ff_detail.etd` |
| `eta` | date | no | as above | `job_ff_detail.eta` |

### Sheet `Cargo`

| Field | Type | Required | Allowed values | Lands in |
|---|---|---|---|---|
| `hs_code` | text | no | any | `job_ff_detail.hs_code` (20) |
| `package_count` | integer | no | whole number; unparseable → null | `job_ff_detail.package_count` |
| `package_kind` | text | no | any — free text (`crates`, `cartons`, `pallets`) | `job_ff_detail.package_kind` (60) |
| `gross_weight_kg` | number | no | passed through uncoerced to `Numeric(14,3)` | `job_ff_detail.gross_weight_kg` |
| `volume_cbm` | number | no | uncoerced to `Numeric(12,3)` | `job_ff_detail.volume_cbm` |
| `container_count` | integer | no | whole number; unparseable → null | `job_ff_detail.container_count` |
| `is_dangerous` | **truthiness** | no | see caveat | `job_ff_detail.is_dangerous` |
| `dg_class` | text | no | any | `job_ff_detail.dg_class` (20) |
| `un_number` | text | no | any | `job_ff_detail.un_number` (12) |

**`is_dangerous` caveat.** It is `bool(value)`. Any non-empty value is
true, so `No`, `NO`, `n/a` and `0` as text all mean dangerous. The only
safe way to say no is to **leave the cell blank**.

### Read but stored nowhere

`customer_code`, `quote_reference`, `scope`, `currency`. They survive in
`ff_handover_packs.payload` and on no business record. `scope` is
deliberate — a job already says what it is buying through its legs
(`job_sows`), and `ff_jobs.scope_of()` derives the ticks rather than
re-keying them. The other three are not.

## What one import creates

Four rows, in this order, all under the business unit passed in.

**1. `projects`**

| Column | Value |
|---|---|
| `code` | `FF` + the normalised reference, truncated to 30; falls back to `FF<pack id>` |
| `name` | `<mode> <direction> — <reference>`, truncated to 200 |
| `client_id` | the matched client |
| `business_id` | the BU passed in |

Everything else on the project is left at its default: no `bu` code, no
`vertical`, no branch, no project head, no team. A pack-created project is
thinner than a typed one.

**2. `jobs`**

| Column | Value |
|---|---|
| `pcn_job_no` | `FF` + the normalised reference, truncated to 80 — **not** the `PRO…/JOB…` shape of a road job |
| `name` | `<pol_name or pol_code> → <pod_name or pod_code>`, or the reference if both are blank |
| `project_id` | the project just made |
| `created_by_id` | whoever pressed import |
| `status` | defaults to `LOADING` |

The reference is normalised **once**, on the way in, and both the project
code and the job number are built from that one value. The project code is
half of a job number, so a reference pasted with a stray space around its
slash would otherwise produce a job nothing can match.

**3. `job_ff_detail`** — one row, unique per job. Mapped field by field in
the tables above. Everything the pack does not carry stays null: the whole
carriage block (`booking_no`, `vessel_name`, `voyage_no`, `flight_no`,
`atd`, `ata`), all four bills of lading (`mbl_no`, `hbl_no`, `mawb_no`,
`hawb_no`), `container_numbers`, `seal_numbers`,
`chargeable_weight_kg`, every customs field (`sb_no`, `leo_at`, `igm_no`,
`be_no`, `duty_amount`, `ooc_at`, `do_released_at`) and the two closing
dates. Those are filled as the shipment moves, by hand.

**4. `client_pos`**

| Column | Value |
|---|---|
| `po_number` | `FF/<reference>`, truncated to 60 |
| `client_po_number` | `tally_po_number` from the pack |
| `po_date` | today |
| `po_value` | `sell_value`, or 0 |
| `gst_treatment` | hard-coded `'FCM'` |
| `status` | `Active` |
| `procam_gst_reg_id` | the **lowest-id active** `procam_gst_regs` row |
| `client_id` / `project_id` / `job_id` | all three set |

If there is no active GST registration the import refuses with a message
saying an administrator sets one up first — the column is NOT NULL and
without that check the import would fail on its last step having already
made a project and a job.

**Vendor PO: nothing is created.** The importer's own docstring says "and
draft vendor orders"; the code does not, and there is no `VendorPO` import
in the module. Buying the carriage, the CHA work and the haulage is a
separate act against the job afterwards, through the normal vendor-PO
screen. The three vendor columns on `job_ff_detail`
(`carrier_vendor_id`, `overseas_agent_vendor_id`, `cha_vendor_id`) are the
link back, and the import leaves all three null even when it has written
the names beside them.

## When an import is refused

In the order checked:

1. This fingerprint is already imported under another pack.
2. Re-reading the stored payload produces any validation problem.
3. `reference`, `customer`, `mode` or `direction` missing.
4. `mode` not in `SEA`, `AIR`.
5. `direction` not in `EXPORT`, `IMPORT`, `CROSS_TRADE`.
6. No client on the register whose name matches `customer`.
7. No active Procam GST registration.

A refusal leaves nothing behind. Note that `cha_mode` is **not** on this
list: a typo there is stored as typed.

## Filled examples, one per mode and direction

As JSON. The same values go in column B of the matching Excel sheets. All
four shipped in the template; `CROSS_TRADE` does **not** and is given here
because the validator accepts it.

### SEA EXPORT
```json
{
  "reference": "HP-2026-0101", "customer": "ACME ENGINEERING LTD",
  "mode": "SEA", "direction": "EXPORT",
  "incoterm": "FOB", "incoterm_place": "Chennai",
  "shipper_name": "ACME ENGINEERING LTD", "consignee_name": "HANSA GMBH",
  "carrier_name": "ONE LINE", "cha_mode": "OWN",
  "pol_code": "INMAA", "pol_name": "Chennai",
  "pod_code": "DEHAM", "pod_name": "Hamburg",
  "hs_code": "84314990", "package_count": 12, "package_kind": "crates",
  "gross_weight_kg": 18400, "volume_cbm": 42, "container_count": 2,
  "sell_value": 480000, "currency": "INR"
}
```
Makes project `FFHP-2026-0101` / "SEA EXPORT — HP-2026-0101", job
`FFHP-2026-0101` / "Chennai → Hamburg", order `FF/HP-2026-0101` at 480,000.

### SEA IMPORT
```json
{
  "reference": "HP-2026-0102", "customer": "ACME ENGINEERING LTD",
  "mode": "SEA", "direction": "IMPORT",
  "incoterm": "CIF", "incoterm_place": "Nhava Sheva",
  "shipper_name": "SHANGHAI TOOLS CO", "consignee_name": "ACME ENGINEERING LTD",
  "carrier_name": "MAERSK", "cha_mode": "OUTSOURCED",
  "pol_code": "CNSHA", "pol_name": "Shanghai",
  "pod_code": "INNSA", "pod_name": "Nhava Sheva",
  "hs_code": "82071990", "package_count": 300, "package_kind": "cartons",
  "gross_weight_kg": 9800, "volume_cbm": 28, "container_count": 1,
  "sell_value": 265000, "currency": "INR"
}
```

### AIR EXPORT
```json
{
  "reference": "HP-2026-0103", "customer": "ACME ENGINEERING LTD",
  "mode": "AIR", "direction": "EXPORT",
  "incoterm": "FCA", "incoterm_place": "Mumbai",
  "shipper_name": "ACME ENGINEERING LTD", "consignee_name": "RHEIN PARTS AG",
  "carrier_name": "LUFTHANSA CARGO", "cha_mode": "OWN",
  "pol_code": "BOM", "pol_name": "Mumbai",
  "pod_code": "FRA", "pod_name": "Frankfurt",
  "hs_code": "84821000", "package_count": 4, "package_kind": "pallets",
  "gross_weight_kg": 860, "volume_cbm": 4,
  "sell_value": 310000, "currency": "INR"
}
```
No `container_count` on an air pack; the column stays null.

### AIR IMPORT
```json
{
  "reference": "HP-2026-0104", "customer": "ACME ENGINEERING LTD",
  "mode": "AIR", "direction": "IMPORT",
  "incoterm": "DAP", "incoterm_place": "Chennai",
  "shipper_name": "SINGAPORE PRECISION", "consignee_name": "ACME ENGINEERING LTD",
  "carrier_name": "SINGAPORE AIRLINES", "cha_mode": "OWN",
  "pol_code": "SIN", "pol_name": "Singapore",
  "pod_code": "MAA", "pod_name": "Chennai",
  "hs_code": "90318000", "package_count": 2, "package_kind": "boxes",
  "gross_weight_kg": 180, "volume_cbm": 1,
  "sell_value": 142000, "currency": "INR"
}
```

### SEA CROSS_TRADE — accepted, not in the template
```json
{
  "reference": "HP-2026-0105", "customer": "ACME ENGINEERING LTD",
  "mode": "SEA", "direction": "CROSS_TRADE",
  "incoterm": "CFR", "incoterm_place": "Jebel Ali",
  "shipper_name": "SHANGHAI TOOLS CO", "consignee_name": "GULF MACHINERY LLC",
  "carrier_name": "MAERSK", "cha_mode": "OUTSOURCED",
  "pol_code": "CNSHA", "pol_name": "Shanghai",
  "pod_code": "AEJEA", "pod_name": "Jebel Ali",
  "hs_code": "84314990", "package_count": 60, "package_kind": "cartons",
  "gross_weight_kg": 7400, "volume_cbm": 22, "container_count": 1,
  "sell_value": 198000, "currency": "INR"
}
```
A cross-trade shipment passes no Indian customs: its stage list is
`BOOKED → ON_BOARD → DEPARTED → ARRIVED → DELIVERED` and nothing else.

---

# Part B — how a non-FF job is created today

## The numbering

| Thing | Shape | Who issues it |
|---|---|---|
| Project code | `PRO<serial><year>` — e.g. `PRO11032026`. Older imported rows carry the legacy `PRO<yyyymmdd><seq>` form, e.g. `PRO20250509000001` | **typed by a person**. Nothing in the TMS generates it |
| Job number | `<project code>/JOB<serial><year>` — e.g. `PRO10052026/JOB100362026` | **typed by a person** |
| Client order | `<prefix>/SPO/<FY>/<seq>` — e.g. `PLPL/SPO/2026-27/0122` | **assigned by the system**, `app/commercials/numbering.py`, one series per company, restarting each financial year |

The job number carries its project code, so the two halves must agree. Both
job-creation screens check this: `job_lookup.project_named_in()` reads the
project half out of the number and refuses the save if it names a different
project than the one you are adding the job under. The comparison reduces
both sides to letters and digits, drops a leading `Job ` or `Project `
label and drops a bracketed note such as `(DEMO)` — so
`PRO11082026 /JOB104642026 (DEMO)` and `PRO11082026/JOB104642026` are one
number.

Some jobs on record carry only the job half (`JOB105732026`) with no
project code. Those predate the check.

## Step 1 — the project

`POST /projects/new`. Permitted to Admin, Transport Head, Procam Admin, LR
Team, or anyone holding the `project_manager` RBAC role on a project.

Required — the form refuses without all four:

| Field | Note |
|---|---|
| `code` | whitespace-normalised on save (`app/services/job_numbers.py`), because the code is half of every job number under it. Unique |
| `name` | |
| `client_id` | from the client master |
| `business_id` | the company. Must be "ready" — a BU with no document series or no GST registration is refused here, not later |

Optional and worth filling: `branch_id` (must belong to that company),
`project_head_id`, `vertical`, `status` (`Active` / `On Hold` / `Closed`),
`start_date`, `end_date`, `currency`, `fiscal_year`, `phase`,
`short_description`, `long_description`, `linked_lead_number`, and the four
`client_po_*` reference fields, `project_value`, `estimated_expenses`.

On save: the named roles are assigned, a team template fills any seat left
empty, `PROJECT.CREATE` is audited, and if a mandatory workflow role is
still unassigned you are redirected to the go-live screen with a warning.
The project is created either way.

**`vertical` is a free-text box.** No dropdown, no validation. Production
holds twelve distinct values for six real verticals — `INSTALLATION` and
`Installation`, `WAREHOUSE` and `Warehouse`, `PTM-H`, `PTM-M` and
`PTM (H-M)` — plus the literal strings `Vertical` and `None` from an
import. Three separate vocabularies exist in the code (`rate_engine.sell`
has six codes, `POVertical` seven, `SBUVertical` nine) and the column obeys
none of them. The margin table normalises what it can and gives up on the
rest.

## Step 2 — the job

`POST /projects/<project_id>/jobs/new`. Permitted to the creator roles
above, or whoever manages that project.

Required:

| Field | Note |
|---|---|
| `pcn_job_no` | unique across all jobs; must name this project |
| `name` | up to 500 |

Set for you: `project_id`, `business_id` (**always the project's company**,
never chosen separately), `created_by_id`, `is_active`, and `status`
defaulting to `LOADING`.

Optional: `full_job_code`, `splendin_job_no`, `start_date`, `end_date`,
`status` from the 13 pickable values, `currency` (blank follows the
project), the cargo block (`cargo_description`, L/W/H with
`cargo_dimension_unit` in `m`/`ft`/`cm`/`mm`, `cargo_weight_kg`,
`cargo_packages`), `job_value`, `estimated_expenses`, `shipper_name`,
`consignee_name`, `supplier_cha_name`, `job_type`, `job_owner_id`, `mod`,
`discharge_port`, `final_site`, `sbu_unit`, `is_have_dsr_shipment`.

`CANCELLED` and `VOIDED` are deliberately absent from the dropdown. They
are reached through the void/cancel flow, which checks children and writes
an audit row, and never by picking from a list.

Billing is set on the job, one of two methods, and this is what an LR's
invoice amount comes from:

| `client_billing_method` | How the LR is priced |
|---|---|
| `PER_VEHICLE` | the rate for the assigned vehicle type, from `job_vehicle_rates` |
| `PER_FRT` | `frt_rate_per_unit` × the volume the LR team enters, labelled by `frt_unit` and `frt_volume_unit` |

## Step 3 — the client order

`POST /commercials/pos/new`, internal users only, manage-commercials roles.

Required:

| Field | Note |
|---|---|
| `client_id` | |
| `procam_gst_reg_id` | which Procam registration bills it; must belong to the chosen company |
| `gst_treatment` | one of six: `TRANSPORT_RCM`, `TRANSPORT_FCM`, `TRANSPORT_EXEMPT`, `GOODS_5`, `GOODS_18`, `GOODS_28`. Legacy `FCM` / `RCM` are accepted and aliased to the Transport variants |
| `po_date` | |
| `business_id` | the issuing company, which decides the series and must be "ready" |

`po_number` is **assigned, never typed**. `project_id` and `job_id` are
optional on the order — which is the gap the live-PO worklist exists to
chase. `client_po_number` is the customer's own number, kept as a
reference; entering one that is already on another order for the same
customer sends you to that order instead of creating a second.

`gst_rate` defaults from the treatment rather than being hard-coded at the
call site. Status starts at `Draft`. Lines are created from the form only
when line fields are posted; a header typed from scratch has none.

## The other ways a job comes into being

Five call sites create a `Job`. Two are the screens above. The rest:

| Where | What it does | State |
|---|---|---|
| `commercials/api/jobs/quick-add` | opens a job from the Sales PO form; same project-name check | live, but sets `status='Active'`, which is **not one of the 13 values** `JobStatus` defines |
| `admin` job quick-add | opens a job from the LR Create form; number + project only | live; sets `status=LOADING` |
| `services/framework.py` | a framework work release opens its project and job | live |
| `po_ingest` approve | would create a project and job from a scanned customer PO | **cannot run.** It calls `Job(client_id=…, description=…)`; the `jobs` table has neither column, and the `Project(…)` beside it omits the NOT NULL `code`. The branch only works when the reviewer picks an existing project and job on the form |

The PO-ingest branch is also where the generated `JOB<yyyymmdd><id>` shape
in that file comes from — it is unreachable, so no job carries it.

## Filled examples, one per vertical

Project, job and order, as they would be typed. Values are illustrative.

### PTM-H — hydraulic axle transport
```
Project   code PRO11032026   name "Stator transport — refinery expansion"
          client ACME ENGINEERING LTD   business PLPL   vertical PTM-H
          status Active   currency INR
Job       pcn_job_no PRO11032026/JOB100362026
          name "STATOR 1 // 142 MT // JETTY TO SITE"
          status TRAILER PLACED   mod Road   job_type Road
          cargo 12.400 × 4.200 × 4.800 m, 142000 kg, 1 package
          client_billing_method PER_VEHICLE   job_value 4,850,000
Order     PLPL/SPO/2026-27/0122   gst_treatment TRANSPORT_FCM (18%)
          client_po_number <the customer's own PO number>
          po_value 4,850,000   project + job both linked
```

### PTM-M — mechanical transport
```
Project   code PRO11042026   vertical PTM-M   business PLPL
Job       pcn_job_no PRO11042026/JOB100412026
          name "32 × SKID LOADS // PLANT TO PORT"
          status ROAD IN TRANSIT   mod Road
          client_billing_method PER_FRT
          frt_rate_per_unit 4200   frt_unit "per Ton"   frt_volume_unit "MT"
Order     PLPL/SPO/2026-27/0123   gst_treatment TRANSPORT_RCM (5%, client pays direct)
```
`PER_FRT` is the method where the LR team enters the volume and the invoice
amount is volume × rate.

### BLADE — wind blade transport
```
Project   code PRO11052026   vertical BLADE   business PLPL
Job       pcn_job_no PRO11052026/JOB100492026
          name "9 × 74M BLADE // PORT TO WINDFARM"
          status PRE CARRIAGE   mod Road
          cargo 74.000 × 3.600 × 4.100 m, 22000 kg, 9 packages
          client_billing_method PER_VEHICLE
Order     PLPL/SPO/2026-27/0124   gst_treatment TRANSPORT_FCM
```

### PFM — freight forwarding, typed rather than imported
```
Project   code PRO11062026   vertical PFM   business PWLPL
Job       pcn_job_no PRO11062026/JOB100522026
          name "Chennai → Hamburg // 2 × 40 HC"
          status OCEAN TRANSIT   mod Sea   job_type Sea
          discharge_port Hamburg   supplier_cha_name <the CHA's name>
Order     PW/SPO/2026-27/0007   gst_treatment GOODS_18
```
A PFM job typed this way has **no `job_ff_detail` row**: no bills of
lading, no ETD/ETA, no customs fields, nowhere to put the booking number.
That gap is what Part A exists to close.

### INSTALLATION
```
Project   code PRO11072026   vertical INSTALLATION   business PLPL
Job       pcn_job_no PRO11072026/JOB100582026
          name "ERECTION // 2 × TRANSFORMER // SUBSTATION"
          status INSTALLATION PROGRESS   job_type Installation
          final_site <the site>
Order     PLPL/SPO/2026-27/0125   gst_treatment GOODS_18
```

### WAREHOUSE
```
Project   code PRO11082026   vertical WAREHOUSE   business PLPL
Job       pcn_job_no PRO11082026/JOB100612026
          name "BONDED STORAGE // 1,800 SQ FT // 6 MONTHS"
          status IN WAREHOUSE   job_type Warehouse   sbu_unit <the facility>
Order     PLPL/SPO/2026-27/0126   gst_treatment GOODS_18
          po_validity_start / po_validity_end set, billing_frequency Monthly
```
Warehouse projects raise no LRs or hire challans, so no transport documents
hang off this job. The order and its billing frequency are the whole record.

---

# Known gaps, for whoever picks this up

Found while writing this, all read-only, none fixed here.

1. **The importer has no screen.** `preview()` and `import_pack()` have no
   caller outside the tests. The upload, the preview page and the import
   button all still have to be built, behind `FF_JOBS`.
2. **`currency` is read and dropped.** A pack marked `USD` creates a
   `client_pos` row at `po_currency` default `INR` with the foreign figure
   in it. Either carry it to the order and the job, or refuse anything but
   `INR` until it is carried.
3. **`gst_treatment` is hard-coded `'FCM'`** in the importer, which the
   six-value vocabulary aliases to `TRANSPORT_FCM` at 18%. A freight order
   is not always transport-FCM, and the house rule is that no tax
   treatment is decided in business logic. It should come off the pack or
   off the customer.
4. **The order bypasses the Sales PO series.** The importer writes
   `FF/<reference>` while every typed order gets
   `<prefix>/SPO/<FY>/<seq>` from `numbering.next_number()`. Two numbering
   schemes in one table, and the FF one carries no company.
5. **The order is created `Active`, not `Draft`,** skipping whatever
   approval a typed order passes through.
6. **The GST registration is picked by lowest id,** not by the business
   unit. A company with two registrations gets whichever was created
   first.
7. **`is_dangerous` is truthiness.** `No` in that cell means yes. It needs
   the same yes/no parsing the rest of the system uses.
8. **`cha_mode` is not validated** although `ChaMode` defines exactly two
   values, and `mode` and `direction` beside it are.
9. **The docstring promises vendor orders it does not create.** Either the
   draft vendor POs get written or the sentence goes.
10. **A pack-created project is thinner than a typed one** — no `bu` code,
    no vertical, no branch, no head, no team, so it misses the go-live
    coverage check every typed project passes.
11. **The template imports as its last example sheet.** The reader takes
    the last value for a field across all sheets, so a pack sent with the
    examples left in silently becomes `HP-2026-0104`. The reader should
    read one named sheet set, or refuse a workbook that answers a field
    twice with different values.
12. **`po_ingest` approve cannot create a project or job** (see the table
    above). It has presumably been failing on that branch for as long as
    the columns have been absent.
