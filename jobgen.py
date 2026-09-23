#!/usr/bin/env python3
"""
jobgen.py — turn crew photos + one sentence into a publishable job page.

Prototype for the SMS/app intake pipeline. This is the GENERATION step only:
no SMS transport, no WordPress publishing. It proves the part that was uncertain —
whether the output is good enough to put on a client site.

  python jobgen.py --client example-co \
      --photos ./sample-job \
      --text "did 9 double hungs in hampton bays today, customer hated the street noise"

Add --dry-run to validate config + photos without spending anything.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import datetime as _dt
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MODEL_DEFAULT = "claude-opus-5"

# $ per 1M tokens (input, output). Source: Claude API pricing.
PRICES = {
    "claude-opus-5":   (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

MAX_EDGE = 1568          # API downsamples above this anyway; sending more is wasted upload
JPEG_QUALITY = 85
SUPPORTED_IN = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _load_env() -> None:
    """Read KEY=VALUE from job-pages/.env if present. Lets the receiver run as a service
    without the key living in a shell profile. Real environment always wins."""
    p = Path(__file__).parent / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()



# ----------------------------------------------------------------- images ---
def _require_pillow():
    try:
        from PIL import Image  # noqa
        return True
    except ImportError:
        sys.exit("Pillow is required.  pip install pillow")


def exif_latlon(path: Path) -> Optional[Tuple[float, float]]:
    """Pull GPS out of a photo, if the phone recorded it. Never fatal."""
    try:
        from PIL import Image
        from PIL.ExifTags import GPSTAGS
        with Image.open(path) as im:
            exif = getattr(im, "_getexif", lambda: None)()
        if not exif or 34853 not in exif:
            return None
        gps = {GPSTAGS.get(k, k): v for k, v in exif[34853].items()}

        def deg(v, ref):
            d, m, s = (float(x) for x in v)
            val = d + m / 60.0 + s / 3600.0
            return -val if ref in ("S", "W") else val

        if "GPSLatitude" not in gps or "GPSLongitude" not in gps:
            return None
        return (
            round(deg(gps["GPSLatitude"], gps.get("GPSLatitudeRef", "N")), 5),
            round(deg(gps["GPSLongitude"], gps.get("GPSLongitudeRef", "E")), 5),
        )
    except Exception:
        return None


def prep_image(path: Path, out_dir: Path, idx: int) -> Dict[str, Any]:
    """Resize, re-encode as JPEG (which drops EXIF, including GPS), return b64 + meta."""
    from PIL import Image, ImageOps

    gps = exif_latlon(path)
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)      # honour rotation before we discard EXIF
        im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > MAX_EDGE:
            scale = MAX_EDGE / float(max(w, h))
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY)   # no exif= -> GPS stripped
        data = buf.getvalue()

    out_dir.mkdir(parents=True, exist_ok=True)
    safe = out_dir / f"photo-{idx}.jpg"
    safe.write_bytes(data)

    return {
        "index": idx,
        "source": str(path),
        "clean_path": str(safe),
        "gps": gps,
        "bytes": len(data),
        "b64": base64.standard_b64encode(data).decode("utf-8"),
    }


def collect_photos(spec: List[str]) -> List[Path]:
    out: List[Path] = []
    for s in spec:
        p = Path(s).expanduser()
        if p.is_dir():
            out.extend(sorted(q for q in p.iterdir() if q.suffix.lower() in SUPPORTED_IN))
        elif p.is_file():
            out.append(p)
        else:
            sys.exit(f"Not found: {s}")
    heic = [p for p in out if p.suffix.lower() in (".heic", ".heif")]
    if heic:
        print(f"  ! skipping {len(heic)} HEIC file(s) — install pillow-heif or let MMS convert to JPEG")
    return [p for p in out if p.suffix.lower() in SUPPORTED_IN]



# ----------------------------------------------------------------- geocode ---
# Where the reverse-geocode cache lives. On Railway this resolves to the mounted volume.
GEO_REV_CACHE = Path(__file__).parent / "jobs" / "_geo_rev.json"
# Cloudflare 403s the default python-requests UA. Identify by name; override per agency.
UA = os.environ.get(
    "JOB_PAGES_UA", "JobPages/1.0 (+https://github.com/job-pages/job-pages)")


def reverse_geocode(lat: float, lon: float, log=print) -> Optional[Dict[str, Any]]:
    """Coordinates -> place names, cached. Rounded to ~110m for the cache key: we only ever
    need the town, and it keeps one job's photos from being several lookups."""
    key = f"{round(lat, 3)},{round(lon, 3)}"
    try:
        cache = json.loads(GEO_REV_CACHE.read_text()) if GEO_REV_CACHE.exists() else {}
    except Exception:
        cache = {}
    if key in cache:
        return cache[key]

    url = ("https://nominatim.openstreetmap.org/reverse?"
           + urllib.parse.urlencode({"lat": lat, "lon": lon, "format": "json", "zoom": 16}))
    place = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        a = d.get("address", {}) or {}
        place = {
            "hamlet": (a.get("hamlet") or a.get("village") or a.get("suburb")
                       or a.get("neighbourhood") or ""),
            "town": a.get("town") or a.get("city") or a.get("municipality") or "",
            "county": a.get("county", ""),
            "state": a.get("state", ""),
            "display": d.get("display_name", "")[:160],
        }
        time.sleep(1.1)   # Nominatim asks for <=1 request/second
    except Exception as e:
        log(f"  ! reverse geocode failed for {key}: {e}")

    cache[key] = place
    try:
        GEO_REV_CACHE.parent.mkdir(parents=True, exist_ok=True)
        GEO_REV_CACHE.write_text(json.dumps(cache))
    except Exception:
        pass
    return place


def town_from_gps(cfg: Dict[str, Any], latlon: Tuple[float, float],
                  log=print) -> Dict[str, Any]:
    """Map coordinates onto one of the client's town pages.

    Returns the matched slug where possible, the raw place names either way. The raw names
    matter even when nothing matches: that is how an out-of-area job gets caught by its
    coordinates rather than by whatever the crew happened to type.
    """
    place = reverse_geocode(latlon[0], latlon[1], log) or {}
    names = [n for n in (place.get("hamlet"), place.get("town")) if n]
    if not names:
        return {"slug": "", "label": "", "place": place, "names": []}

    def norm(n: str) -> str:
        # OSM returns administrative names like "Town of Huntington"; the page is "Huntington"
        return re.sub(r"^(town|village|city|hamlet) of\s+", "", n.strip().lower())

    by_label = {t["label"].lower(): t["slug"] for t in cfg["towns"]}
    aliases = {k.lower(): v for k, v in (cfg.get("town_aliases") or {}).items()}
    for n in names:
        low = norm(n)
        if low in by_label:
            return {"slug": by_label[low], "label": n, "place": place, "names": names}
        if low in aliases:
            slug = aliases[low]
            lbl = next((t["label"] for t in cfg["towns"] if t["slug"] == slug), slug)
            return {"slug": slug, "label": lbl, "place": place, "names": names,
                    "via_alias": n}
    return {"slug": "", "label": "", "place": place, "names": names}


# ---------------------------------------------------------------- schemas ---
def schema_observe() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "photos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "shot_type": {"type": "string", "enum": ["before", "after", "during", "detail", "unclear"]},
                        "subject": {"type": "string", "enum": ["window", "door", "siding", "trim", "interior", "exterior", "other"]},
                        "visible": {"type": "array", "items": {"type": "string"},
                                     "description": "Only what is literally visible. No inference about brand, price, or performance."},
                        "product_description": {"type": "string",
                                                 "description": "Plain description of the product shown, e.g. 'white vinyl double-hung with colonial grids'. Empty string if not determinable."},
                        "count_visible": {"type": ["integer", "null"],
                                           "description": "Number of units clearly countable in this photo, else null."},
                        "usable": {"type": "boolean", "description": "Sharp, well-lit and relevant enough to publish."},
                        "quality_notes": {"type": "string"}
                    },
                    "required": ["index", "shot_type", "subject", "visible", "product_description",
                                 "count_visible", "usable", "quality_notes"],
                    "additionalProperties": False
                }
            },
            "overall_evidence": {"type": "array", "items": {"type": "string"},
                                  "description": "Facts supported across photos."},
            "cannot_determine": {"type": "array", "items": {"type": "string"},
                                  "description": "Things a reader would want that the photos do NOT show."}
        },
        "required": ["photos", "overall_evidence", "cannot_determine"],
        "additionalProperties": False
    }


def schema_page(cfg: Dict[str, Any], n_photos: int) -> Dict[str, Any]:
    services = [s["slug"] for s in cfg["services"]]
    towns = [t["slug"] for t in cfg["towns"]]
    return {
        "type": "object",
        "properties": {
            # enums are the geo/service guard: an out-of-area town is unrepresentable
            "service": {"type": "string", "enum": services},
            # The page to LINK to. The client has pages for only some of the towns they work
            # in, so this is the nearest/parent page, not necessarily where the job was.
            "town": {"type": "string", "enum": towns},
            # Where the work ACTUALLY happened, free text. May be a town with no page.
            "town_name": {"type": "string",
                           "description": "The real town or hamlet the job was in, as it should "
                                          "read on the page. Often the same as the town page; "
                                          "use the true name when it differs."},
            "service_confidence": {"type": "number"},
            "town_confidence": {"type": "number"},
            "h1": {"type": "string"},
            "title_tag": {"type": "string"},
            "meta_description": {"type": "string"},
            "slug": {"type": "string"},
            "body_html": {"type": "string",
                           "description": "2-4 short <p> paragraphs, optional one <h2>. No inline styles, no headings above h2."},
            "photos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "caption": {"type": "string"},
                        "alt": {"type": "string"}
                    },
                    "required": ["index", "caption", "alt"],
                    "additionalProperties": False
                }
            },
            "internal_links": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}, "anchor": {"type": "string"}},
                    "required": ["url", "anchor"],
                    "additionalProperties": False
                }
            },
            "facts_used": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "fact": {"type": "string"},
                        "source": {"type": "string", "enum": ["photo", "crew_text", "gps", "client_config"]}
                    },
                    "required": ["fact", "source"],
                    "additionalProperties": False
                },
                "description": "Every substantive claim in the page and where it came from."
            },
            "missing_info": {"type": "array", "items": {"type": "string"}},
            "followup_question": {"type": ["string", "null"],
                                   "description": "One SMS-length question that would most improve the page, or null."},
            "compliance_flags": {"type": "array", "items": {"type": "string"}},
            "quality_score": {"type": "number", "description": "0-1. Be harsh: thin input means a low score."}
        },
        "required": ["service", "town", "town_name", "service_confidence", "town_confidence", "h1", "title_tag",
                     "meta_description", "slug", "body_html", "photos", "internal_links", "facts_used",
                     "missing_info", "followup_question", "compliance_flags", "quality_score"],
        "additionalProperties": False
    }


# ------------------------------------------------------------------ calls ---
def call(client, model: str, system: str, content: Any, schema: Dict[str, Any],
         effort: Optional[str] = None) -> Tuple[Dict[str, Any], Any]:
    oc: Dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
    if effort:
        oc["effort"] = effort
    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config=oc,
        thinking={"type": "adaptive"},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text), resp.usage


SYS_OBSERVE = """You are looking at photos from a home-improvement crew who just finished a job.

Report ONLY what is literally visible. This is evidence collection, not marketing.

Hard rules:
- Never name a manufacturer, brand, model, price, warranty, or energy rating. You cannot see those.
- Never guess at performance ("more efficient", "better insulated"). Not visible.
- If a photo is blurry, dark, or shows nothing useful, mark usable=false and say why.
- count_visible is only for units you can actually count in that single frame.
"""


def sys_write(cfg: Dict[str, Any]) -> str:
    v = cfg["voice"]
    banned = ", ".join(v["banned_style"])
    return f"""You write short project pages for {cfg['business_name']}'s website. Each page documents one real job.

VOICE
{v['summary']}
Reading level: {v['reading_level']}
Never use these words or constructions: {banned}
No em dashes. Vary sentence length. Do not open with "When it comes to" or any variant.

THE ONE RULE THAT MATTERS
Every substantive claim must trace to an observation from the photos, the crew's own words, or
the GPS location. If you did not receive it, it does not go on the page. Do not invent the
manufacturer, the product line, the price, the duration, the customer's name, the energy savings,
the warranty, or how many people were on the crew. A short honest page beats a padded one.

COMPLIANCE — these create real legal exposure, never write them:
- Never say or imply the company manufactures its own product. It is a factory-direct licensee.
- No tax credits. No rebates. No percentage or dollar savings claims. No energy-savings projections.
- No financing, monthly payments, APR or 0% offers of any kind.
- No superlatives about awards or being best/#1.
- No roofing. That service was discontinued.
If the crew's text pushes you toward any of the above, drop it and add a note to compliance_flags.

STRUCTURE
- h1: specific and local. Include the real count and product type when known, plus the town.
- title_tag: <= {cfg['quality_gate']['title_max_chars']} chars. meta_description: <= {cfg['quality_gate']['meta_max_chars']} chars.
- body_html: 2-4 short paragraphs. What the home needed, what went in, what changed for the owner.
  Concrete over adjectival. If you only have thin material, write less, and lower quality_score.
- captions describe that specific photo. alt text is literal and useful to a screen reader.
- internal_links: choose only from the URLs supplied. Include the town page and the service page.
- gps_resolved_town comes from the photos' own GPS. Where it is present it is more reliable
  than a crew's spelling; if it disagrees with what the crew typed, prefer the GPS and note
  the disagreement in missing_info.
- The client works across a much wider area than the towns that have pages. `town_name` is
  where the job really was and is what the copy, h1 and schema should say. `town` is only the
  existing page to link to - the nearest or parent one. When they differ that is normal, not
  an error: say the real place, link to the closest page, and note it in missing_info.
- Use hamlet_to_town_page when it has an entry for the place.
- quality_score: be harsh. One usable photo and four words from the crew is not a 0.8.
"""


# ----------------------------------------------------------------- guards ---
def run_guards(cfg: Dict[str, Any], page: Dict[str, Any], obs: Dict[str, Any],
               gps_town: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []
    gate = cfg["quality_gate"]

    blob = " ".join([
        page.get("h1", ""), page.get("title_tag", ""), page.get("meta_description", ""),
        page.get("body_html", ""),
        " ".join(p.get("caption", "") + " " + p.get("alt", "") for p in page.get("photos", [])),
    ])
    text_only = re.sub(r"<[^>]+>", " ", blob)

    for rule in cfg["compliance_blocklist"]:
        m = re.search(rule["pattern"], text_only)
        if m:
            issues.append({"level": "BLOCK", "check": "compliance",
                           "detail": f'matched "{m.group(0).strip()}" — {rule["reason"]}'})

    fb = cfg["forbidden_towns"]
    for name in fb["names"]:
        if re.search(r"\b" + re.escape(name) + r"\b", text_only, re.I):
            issues.append({"level": "BLOCK", "check": "geo",
                           "detail": f'mentions "{name}" — {fb["reason"]}'})

    towns = {t["slug"]: t for t in cfg["towns"]}
    services = {s["slug"]: s for s in cfg["services"]}
    if page.get("town") not in towns:
        issues.append({"level": "BLOCK", "check": "geo", "detail": f'town "{page.get("town")}" not in service area'})
    if page.get("service") not in services:
        issues.append({"level": "BLOCK", "check": "service", "detail": f'unknown service "{page.get("service")}"'})

    usable = sum(1 for p in obs.get("photos", []) if p.get("usable"))
    if usable < gate["min_usable_photos"]:
        issues.append({"level": "HOLD", "check": "photos",
                       "detail": f"{usable} usable photo(s), need {gate['min_usable_photos']}"})

    words = len(re.findall(r"\w+", re.sub(r"<[^>]+>", " ", page.get("body_html", ""))))
    if words < gate["min_body_words"]:
        issues.append({"level": "HOLD", "check": "thin-content",
                       "detail": f"{words} words, need {gate['min_body_words']} — noindex or hold as draft"})

    if page.get("quality_score", 0) < gate["min_quality_score"]:
        issues.append({"level": "HOLD", "check": "quality",
                       "detail": f"model scored this {page.get('quality_score')}, gate is {gate['min_quality_score']}"})

    if len(page.get("title_tag", "")) > gate["title_max_chars"]:
        issues.append({"level": "WARN", "check": "title", "detail": f'{len(page["title_tag"])} chars'})
    if len(page.get("meta_description", "")) > gate["meta_max_chars"]:
        issues.append({"level": "WARN", "check": "meta", "detail": f'{len(page["meta_description"])} chars'})

    valid_urls = {t["url"] for t in cfg["towns"]} | {s["url"] for s in cfg["services"]} | \
                 {s["pillar"] for s in cfg["services"]}
    for link in page.get("internal_links", []):
        if link.get("url") not in valid_urls:
            issues.append({"level": "WARN", "check": "link",
                           "detail": f'{link.get("url")} is not a known page'})

    # Coordinates beat prose. But "outside the service area" and "we have no page for that
    # town yet" are different things - only the first is a violation.
    sa = cfg.get("service_area") or {}
    if gps_town and gps_town.get("names"):
        place = gps_town.get("place") or {}
        gps_names = " ".join(gps_town["names"]).lower()
        county = (place.get("county") or "").strip()

        excluded = next((n for n in sa.get("exclude_names", fb["names"])
                         if re.search(r"\b" + re.escape(n) + r"\b", gps_names)), None)
        counties = sa.get("include_counties") or []
        out_of_county = bool(counties and county and county not in counties)

        if excluded:
            issues.append({"level": "BLOCK", "check": "geo-gps",
                           "detail": f'photo GPS resolves to "{gps_town["names"][0]}" '
                                     f'({excluded}) — {sa.get("exclude_reason", fb["reason"])}'})
        elif out_of_county:
            issues.append({"level": "BLOCK", "check": "geo-gps",
                           "detail": f'photo GPS is in {county}, outside the service area '
                                     f'({", ".join(counties)})'})
        else:
            gslug = gps_town.get("slug")
            if gslug and page.get("town") and gslug != page["town"]:
                issues.append({"level": "WARN", "check": "geo-mismatch",
                               "detail": f'page links to {page["town"]}, photo GPS says {gslug} '
                                         f'({", ".join(gps_town["names"])}) — confirm'})
            elif not gslug:
                # In the service area, just no page for it. That is a content gap worth
                # knowing about, not a reason to hold the job.
                issues.append({"level": "INFO", "check": "no-town-page",
                               "detail": f'{gps_town["names"][0]} is in {county or "the service area"} '
                                         f'but has no town page — linking to '
                                         f'{page.get("town","?")}. Worth creating one.'})

    # Same finding, reached from the text rather than from coordinates. Most jobs arrive
    # without GPS, so without this the missing-page case is only caught on the minority
    # of photos that still carry EXIF.
    tn = (page.get("town_name") or "").strip()
    if tn and not any(i["check"] == "no-town-page" for i in issues):
        labels = {t["label"].lower() for t in cfg["towns"]}
        excluded = any(re.search(r"\b" + re.escape(n) + r"\b", tn.lower())
                       for n in (sa.get("exclude_names") or fb["names"]))
        if tn.lower() not in labels and not excluded:
            linked = next((t["label"] for t in cfg["towns"] if t["slug"] == page.get("town")),
                          page.get("town", "?"))
            issues.append({"level": "INFO", "check": "no-town-page",
                           "detail": f'{tn} has no town page — linking to {linked}. '
                                     f'Worth creating one.'})

    for f in page.get("compliance_flags", []):
        issues.append({"level": "WARN", "check": "self-flagged", "detail": f})

    return issues


def _place_name(cfg, page, town) -> str:
    """"<Town>, <ST>" when the client config sets a region, otherwise just "<Town>"."""
    name = (page.get("town_name") or town["label"]).strip()
    region = (cfg.get("region") or "").strip()
    return f"{name}, {region}" if region else name


def verdict(issues: List[Dict[str, str]]) -> str:
    """INFO is informational only and never changes the outcome."""
    if any(i["level"] == "BLOCK" for i in issues):
        return "BLOCKED"
    if any(i["level"] == "HOLD" for i in issues):
        return "HOLD-FOR-REVIEW"
    return "READY-FOR-APPROVAL"


# ---------------------------------------------------------------- output ---
def build_schema_org(cfg: Dict[str, Any], page: Dict[str, Any], town: Dict, svc: Dict) -> Dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "Service",
        "name": page["h1"],
        "serviceType": svc["label"],
        "provider": {"@type": "HomeAndConstructionBusiness", "name": cfg["business_name"],
                      "telephone": cfg["phone"], "url": cfg["site"]},
        # State/province comes from the client config. There is no sensible default, so a
        # config without "region" yields the bare town rather than a wrong state.
        "areaServed": {"@type": "Place", "name": _place_name(cfg, page, town)},
        "description": page["meta_description"],
    }


def write_preview(out: Path, cfg, page, obs, issues, photos, schema_org, usage_note,
                  content_hash: str = "") -> Path:
    town = {t["slug"]: t for t in cfg["towns"]}.get(page.get("town"), {"label": page.get("town")})
    colour = {"BLOCKED": "#b3261e", "HOLD-FOR-REVIEW": "#8a6100", "READY-FOR-APPROVAL": "#1a6b34"}
    v = verdict(issues)

    rows = ""
    for i in issues:
        rows += (f'<tr><td class="lv {i["level"]}">{i["level"]}</td>'
                 f'<td>{html.escape(i["check"])}</td><td>{html.escape(i["detail"])}</td></tr>')
    if not rows:
        rows = '<tr><td colspan="3">No issues.</td></tr>'

    cap = {p["index"]: p for p in page.get("photos", [])}
    figs = ""
    for ph in photos:
        c = cap.get(ph["index"], {})
        figs += (f'<figure><img src="{html.escape(os.path.basename(ph["clean_path"]))}" alt="{html.escape(c.get("alt",""))}">'
                 f'<figcaption>{html.escape(c.get("caption",""))}'
                 f'<span class="alt">alt: {html.escape(c.get("alt",""))}</span></figcaption></figure>')

    facts = "".join(f'<li>{html.escape(f["fact"])} <span class="src">{f["source"]}</span></li>'
                    for f in page.get("facts_used", []))
    missing = "".join(f"<li>{html.escape(m)}</li>" for m in page.get("missing_info", [])) or "<li>none</li>"

    needs_town = "true" if any(i["check"] == "no-town-page" for i in issues) else "false"
    town_name_js = html.escape(page.get("town_name", ""), quote=True)
    doc = f"""<!doctype html><meta charset="utf-8">
<title>Job page draft — {html.escape(page.get('h1',''))}</title>
<style>
 body{{font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:860px;margin:0 auto;padding:24px;color:#1a1a1a}}
 .verdict{{background:{colour.get(v,'#333')};color:#fff;padding:12px 16px;border-radius:8px;font-weight:600}}
 .serp{{border:1px solid #ddd;border-radius:8px;padding:14px;margin:18px 0;background:#fafafa}}
 .serp .t{{color:#1a0dab;font-size:19px}} .serp .u{{color:#0b7d2f;font-size:13px}} .serp .d{{color:#4d5156;font-size:14px}}
 table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:14px}}
 td,th{{border:1px solid #e3e3e3;padding:7px 9px;text-align:left;vertical-align:top}}
 .lv{{font-weight:700;white-space:nowrap}} .BLOCK{{color:#b3261e}} .HOLD{{color:#8a6100}} .WARN{{color:#666}} .INFO{{color:#1a6b34}}
 figure{{margin:0 0 18px}} img{{max-width:100%;border-radius:8px;display:block}}
 figcaption{{font-size:14px;color:#444;padding-top:6px}}
 .alt{{display:block;color:#888;font-size:12px;font-family:ui-monospace,monospace}}
 .src{{font-size:11px;background:#eee;padding:1px 6px;border-radius:9px;color:#555}}
 .page{{border:2px dashed #ccd;padding:20px;border-radius:10px;margin:18px 0}}
 h1{{font-size:27px;line-height:1.25}} pre{{background:#f6f6f6;padding:12px;border-radius:8px;overflow:auto;font-size:12px}}
 .meta{{color:#666;font-size:13px}}
</style>
<div class="verdict">{v} &nbsp;·&nbsp; quality {page.get('quality_score')} &nbsp;·&nbsp; {usage_note}</div>

<div id="approve-bar" style="display:none;margin:16px 0;padding:14px;border:1px solid #d7d7d7;border-radius:10px;background:#fff">
 <button id="approve-btn" style="background:#1a6b34;color:#fff;border:0;padding:12px 22px;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer">Approve &amp; publish</button>
 <button id="town-btn" style="display:none;background:#fff;color:#1a6b34;border:1.5px solid #1a6b34;padding:12px 18px;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;margin-left:10px"></button>
 <span id="approve-msg" style="margin-left:14px;color:#444"></span>
</div>
<script>
(function () {{
  var t = new URLSearchParams(location.search).get('t');
  if (!t) return;                               // no token, no buttons
  var H = '{content_hash}';                     // the version being looked at
  var bar = document.getElementById('approve-bar');
  var btn = document.getElementById('approve-btn');
  var msg = document.getElementById('approve-msg');
  bar.style.display = 'block';

  // Offer the town page only when this job flagged one as missing.
  var NEEDS_TOWN = {needs_town};
  var TOWN_NAME = "{town_name_js}";
  var tbtn = document.getElementById('town-btn');
  if (NEEDS_TOWN && TOWN_NAME) {{
    tbtn.textContent = 'Create ' + TOWN_NAME + ' service-area page';
    tbtn.style.display = 'inline-block';
    tbtn.addEventListener('click', function () {{
      tbtn.disabled = true; msg.textContent = 'Writing the ' + TOWN_NAME + ' page\u2026';
      fetch('new-town?t=' + encodeURIComponent(t), {{method: 'POST'}})
        .then(function (r) {{ return r.json(); }})
        .then(function (d) {{
          if (d.ok) {{
            msg.innerHTML = 'Created as a draft. <a href="' + d.url + '" target="_blank">Open it</a>'
              + (d.verify && d.verify.length ? ' \u2014 ' + d.verify.length + ' detail(s) to verify.' : '');
          }} else {{
            msg.textContent = 'Failed: ' + (d.reason || 'unknown'); tbtn.disabled = false;
          }}
        }})
        .catch(function (e) {{ msg.textContent = 'Failed: ' + e; tbtn.disabled = false; }});
    }});
  }}
  btn.addEventListener('click', function () {{
    btn.disabled = true; msg.textContent = 'Publishing…';
    fetch('approve?t=' + encodeURIComponent(t) + '&h=' + encodeURIComponent(H), {{method: 'POST'}})
      .then(function (r) {{ return r.json(); }})
      .then(function (d) {{
        if (d.ok) {{
          msg.innerHTML = d.url
            ? 'Published as a WordPress draft. <a href="' + d.url + '" target="_blank">Open in WordPress</a>'
            : 'Approved.';
        }} else {{
          if (d.reason === 'stale') {{
            msg.innerHTML = 'This draft changed after you opened it. '
              + '<a href="">Reload</a> and review the new version before publishing.';
          }} else {{
            msg.textContent = 'Failed: ' + (d.error || d.reason || 'unknown');
            btn.disabled = false;
          }}
        }}
      }})
      .catch(function (e) {{ msg.textContent = 'Failed: ' + e; btn.disabled = false; }});
  }});
}})();
</script>

<h2>As it would appear in search</h2>
<div class="serp">
 <div class="t">{html.escape(page.get('title_tag',''))}</div>
 <div class="u">{cfg['site']}/projects/{html.escape(page.get('slug',''))}/</div>
 <div class="d">{html.escape(page.get('meta_description',''))}</div>
</div>

<h2>Checks</h2>
<table><tr><th>level</th><th>check</th><th>detail</th></tr>{rows}</table>

<h2>The page</h2>
<div class="page">
 <p class="meta">{html.escape(town.get('label',''))} · {html.escape(page.get('service',''))}</p>
 <h1>{html.escape(page.get('h1',''))}</h1>
 {page.get('body_html','')}
 {figs}
 <p class="meta">Links: {' · '.join(html.escape(l['anchor']) + ' → ' + html.escape(l['url']) for l in page.get('internal_links', []))}</p>
</div>

<h2>Where every claim came from</h2><ul>{facts}</ul>
<h2>What the crew did not tell us</h2><ul>{missing}</ul>
<h2>Follow-up text to send</h2>
<p>{html.escape(page.get('followup_question') or '(none needed)')}</p>
<h2>Schema</h2><pre>{html.escape(json.dumps(schema_org, indent=2))}</pre>
"""
    p = out / "preview.html"
    p.write_text(doc, encoding="utf-8")
    return p


# ------------------------------------------------------------------- main ---
def generate_job(cfg: Dict[str, Any], files: List[Path], crew_text: str,
                 out: Path, model: str = MODEL_DEFAULT, log=print) -> Dict[str, Any]:
    """Photos + crew text -> reviewed draft. Used by the CLI and the webhook receiver."""
    import anthropic

    out.mkdir(parents=True, exist_ok=True)
    photos = [prep_image(p, out, i) for i, p in enumerate(files)]
    gps = [p["gps"] for p in photos if p["gps"]]

    client = anthropic.Anthropic()
    pin, pout = PRICES.get(model, PRICES[MODEL_DEFAULT])
    spend = 0.0

    # Pass 1 - see. Vision only, no writing.
    content: List[Dict[str, Any]] = []
    for p in photos:
        content.append({"type": "text", "text": f"Photo {p['index']}:"})
        content.append({"type": "image", "source": {"type": "base64",
                                                     "media_type": "image/jpeg", "data": p["b64"]}})
    content.append({"type": "text", "text": f"Report what is visible in these {len(photos)} photos."})
    obs, u1 = call(client, model, SYS_OBSERVE, content, schema_observe(), effort="low")
    spend += u1.input_tokens / 1e6 * pin + u1.output_tokens / 1e6 * pout
    log(f"  pass 1: {sum(1 for p in obs['photos'] if p['usable'])}/{len(photos)} photos usable")

    # Pass 2 - write. Never sees the raw images, so it cannot embellish.
    gps_town = town_from_gps(cfg, gps[0], log) if gps else {}
    if gps_town:
        log(f"  gps: {gps_town.get('names')} -> "
            f"{gps_town.get('slug') or 'no matching town page'}")

    brief = {
        "crew_text": crew_text,
        "photo_gps": gps or None,
        "gps_resolved_town": {
            "slug": gps_town.get("slug", ""), "label": gps_town.get("label", ""),
            "place_names": gps_town.get("names", []),
        } if gps_town else None,
        "observations": obs,
        "available_service_pages": [{"slug": s["slug"], "label": s["label"], "url": s["url"],
                                      "pillar": s["pillar"]} for s in cfg["services"]],
        "available_town_pages": [{"slug": t["slug"], "label": t["label"], "url": t["url"]}
                                  for t in cfg["towns"]],
        "licences": cfg["licences"],
        "hamlet_to_town_page": cfg.get("town_aliases", {}),
    }
    page, u2 = call(client, model, sys_write(cfg),
                    [{"type": "text", "text": json.dumps(brief, indent=2)}],
                    schema_page(cfg, len(photos)))
    spend += u2.input_tokens / 1e6 * pin + u2.output_tokens / 1e6 * pout

    issues = run_guards(cfg, page, obs, gps_town)
    v = verdict(issues)
    town = {t["slug"]: t for t in cfg["towns"]}.get(page["town"], {"label": page["town"]})
    svc = {s["slug"]: s for s in cfg["services"]}.get(page["service"], {"label": page["service"]})
    schema_org = build_schema_org(cfg, page, town, svc)
    usage_note = (f"{u1.input_tokens + u2.input_tokens:,} in / "
                  f"{u1.output_tokens + u2.output_tokens:,} out · ${spend:.3f}")

    # Identifies exactly this version of the page. Approval carries it back so a draft that
    # changed after the reviewer read it cannot be published on their behalf.
    content_hash = hashlib.sha256(
        json.dumps(page, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

    obs["_gps"] = list(gps[0]) if gps else None      # kept so a town page can reuse it
    result = {"verdict": v, "generated": _dt.datetime.now().isoformat(timespec="seconds"),
              "model": model, "crew_text": crew_text, "cost_usd": round(spend, 4),
              "content_hash": content_hash,
              "page": page, "observations": obs, "issues": issues, "schema_org": schema_org}
    (out / "draft.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    write_preview(out, cfg, page, obs, issues, photos, schema_org, usage_note, content_hash)
    result["out_dir"] = str(out)
    result["town_label"] = town.get("label", "")
    result["service_label"] = svc.get("label", "")
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--photos", nargs="+", required=True, help="photo files or a directory")
    ap.add_argument("--text", default="", help="what the crew texted in")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--out", default="out")
    ap.add_argument("--dry-run", action="store_true", help="validate without calling the API")
    args = ap.parse_args()

    here = Path(__file__).parent
    cfg_path = here / "clients" / f"{args.client}.json"
    if not cfg_path.exists():
        sys.exit(f"No config at {cfg_path}")
    cfg = json.loads(cfg_path.read_text())

    _require_pillow()
    files = collect_photos(args.photos)
    if not files:
        sys.exit("No usable photos found.")

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if os.path.isabs(args.out) else here / args.out
    out = out / f"{args.client}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    print(f"\n  {len(files)} photo(s) · client {cfg['business_name']}")
    print(f'  crew text: "{args.text or "(none)"}"')

    if args.dry_run:
        photos = [prep_image(p, out, i) for i, p in enumerate(files)]
        gps = [p["gps"] for p in photos if p["gps"]]
        kb = sum(p["bytes"] for p in photos) // 1024
        print(f"  prepared {kb} KB, GPS on {len(gps)}/{len(photos)} (stripped from published copies)")
        print(f"\n  dry run — config valid, {len(cfg['services'])} services / {len(cfg['towns'])} towns")
        print(f"  photos written to {out}\n")
        return

    r = generate_job(cfg, files, args.text, out, args.model)
    page, issues = r["page"], r["issues"]
    print(f"\n  {r['verdict']}   quality {page['quality_score']}   ${r['cost_usd']:.3f}")
    print(f"  {page['h1']}")
    print(f"  /projects/{page['slug']}/   →  {r['town_label']} · {r['service_label']}")
    for i in issues:
        print(f"    [{i['level']}] {i['check']}: {i['detail']}")
    if page.get("followup_question"):
        print(f'  would text back: "{page["followup_question"]}"')
    print(f"\n  open {out / 'preview.html'}\n")


if __name__ == "__main__":
    main()
