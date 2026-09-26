# The client config

`$JOB_PAGES/clients/<id>.json` is everything the pipeline knows about one client. `intake.py`
writes most of it from their live site; the rest is a human's job and is marked `TODO`.

---

## What intake fills in, and what it refuses to

**Discovered from the live site** — real page slugs, so internal links cannot be dead:

- `services` — from `/windows/*`, `/doors/*`, `/siding/*` style paths
- `towns` — from `/service-areas/*`
- `business_name`, `site`

**Seeded from `BRAND-BRIEF.md`** if one is supplied: `voice.summary`, and any compliance
rules it can find, parked under `_compliance_from_brand_brief` for reference.

**Left as `TODO` deliberately:**

| Field | Why a human must do it |
|---|---|
| `compliance_blocklist[].reason` | Each rule is a legal position. An invented FTC guardrail is worse than an obviously missing one |
| `licences` | Licence numbers must appear in advertising; wrong ones are worse than none |
| `forbidden_towns` / `service_area` | Where the client may legally work. Nobody can infer this |

Never resolve a `TODO` by guessing. Ask.

---

## Service area vs town pages — two different things

This distinction matters and was originally conflated.

**`service_area`** — where the client may legally work.

```json
"service_area": {
  "include_counties": ["<County A>", "<County B>"],
  "exclude_towns": ["<Town the licence does not cover>"],
  "exclude_names": ["<town>", "<hamlet>", "<hamlet>"],
  "exclude_reason": "Licence #<number> does not cover <place>."
}
```

`exclude_names` is what the geo check actually matches on, and **an empty list blocks
nothing**. A client whose licence is statewide may legitimately have no exclusions — but
say so in `exclude_reason` rather than leaving it `TODO`, so the next person knows the
emptiness was a decision and not an omission.

Outside it → **BLOCK**. Checked against photo GPS as well as text, so a crew naming a town
inside the area while standing in one outside it is still caught.

**`towns`** — only the towns that have a page. A job in the service area with no page is
**fine**: the page names the real town and links to the nearest one, and an `INFO` finding
offers to create the missing page.

### What the enum does and does not do

`town` is a schema enum, but it only chooses **which existing page to link to**. Where the
job actually happened is `town_name`, which is free text. So a job in a town with no page
is **not blocked**: the copy names the real place, links to the nearest page, and raises an
`INFO` finding — and `verdict()` is explicit that INFO never changes the outcome. The review
screen then offers a one-click **"Create <Town> service-area page"** button.

What *does* block is `service_area.exclude_names`, checked against the text and against
photo GPS. An empty `exclude_names` blocks nothing at all.

Keep the town list complete anyway — every missing page is a link pointing somewhere less
relevant than it could — but do not expect a missing town to announce itself by failing.
Diff the list against the live site before going live.

```bash
# towns that exist as pages but are missing from the config
python3 - <<'PY'
import json, requests
UA={"User-Agent":"YourAgency-JobPages/1.0"}
pages=[]
for pg in range(1,5):
    r=requests.get("https://<site>/wp-json/wp/v2/pages",headers=UA,
                   params={"per_page":100,"page":pg,"_fields":"slug,link"},timeout=30)
    if r.status_code!=200 or not r.json(): break
    pages+=r.json()
live={p["slug"] for p in pages if "/service-areas/" in p["link"] and p["slug"]!="service-areas"}
mine={t["slug"] for t in json.load(open("clients/<id>.json"))["towns"]}
print("missing from config:", sorted(live-mine))
print("in config but not a real page:", sorted(mine-live))
PY
```

Run this against a real client config. On the first site it was tried, it found four
towns the hand-built list had missed.

**`town_aliases`** maps hamlets with no page of their own onto the town page that covers
them — `"centerport": "huntington"`. The copy still names the hamlet.

---

## Compliance

```json
{"pattern": "(?i)\\brebate", "reason": "No rebate amounts without written verification."}
```

Regexes run over the finished page text after generation, independent of the model. A match
is a `BLOCK`.

**Write patterns that distinguish a claim from a description.** An early rule matched the
bare word `roof` and blocked a siding job because a roof was visible in a photo. The fix
matched the service, not the noun:

```
\broofing\b|\bnew roof\b|\bre-?roof\w*\b|\broof (replacement|repair|install)\b
```

❌ `\broof\b` — blocks "the siding runs up to the roofline"
✅ the above — allows description, blocks the offer

Test both directions before trusting a new rule.

---

## `region`

```json
"region": "FL"
```

The state or province. It does two jobs:

1. The Service schema's `areaServed` ("Cape Coral, FL").
2. **The map geocode.** Town centroids are looked up as "<town>, <region>, USA".

The second one is why a missing region is worse than it looks. Town names repeat across
states — there is a Naples in both Florida and New York, a Portland in Oregon and Maine.
Without a region the lookup is ambiguous, and until this was fixed the region was hardcoded
to one state, which meant another client's pins landed there silently. No error, just a
plausible pin in the wrong half of the country.

There is no default: a config without `region` emits the bare town rather than guessing a
state. Set it during onboarding — it is two characters and it is wrong on every page and
every map pin if it is missing or inherited from another client.

## The quality gate

```json
"quality_gate": {"min_usable_photos": 2, "min_body_words": 90,
                 "min_quality_score": 0.6, "title_max_chars": 60, "meta_max_chars": 155}
```

0.6 is calibrated so a crew line with a count, a type and a town clears it. Real scores:
photos only ≈0.34, plus a town ≈0.45, plus a count and window type ≈0.62.

**Do not lower it to make pages pass.** A held draft is the designed outcome for a thin job —
it is what stops 200 near-identical husks accumulating and cannibalising each other. It is
much easier to loosen later than to clean up published thin pages.

---

## Voice

`voice.summary` drives the writing; `banned_style` is a list of AI tells to refuse
(`unlock`, `elevate`, `in today's world`, `seamless`, `when it comes to`). Take these from
the brand brief rather than inventing them.
