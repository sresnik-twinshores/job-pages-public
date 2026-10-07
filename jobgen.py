#!/usr/bin/env python3
"""
jobgen.py — turn field photos + one sentence into a publishable job page.

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

import vertical

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
    "JOB_PAGES_UA", "JobPages/1.0")   # set JOB_PAGES_UA to identify YOUR deployment to site owners


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
    coordinates rather than by whatever the sender happened to type.
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


# Schemas and prompts live in the client's vertical pack (verticals/<id>/),
# loaded through vertical.pack_for(cfg). Engine code owns the mechanism only.


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

    # Pack compliance floor first, then the client's own rules. The floor cannot
    # be removed by a client config — only added to.
    for rule in vertical.pack_for(cfg).compliance_rules(cfg):
        m = re.search(rule["pattern"], text_only)
        if m:
            issues.append({"level": "BLOCK", "check": "compliance",
                           "detail": f'matched "{m.group(0).strip()}" — {rule["reason"]}'})

    # Rules that only apply part of the year, e.g. a seasonal county ordinance. The job is
    # generated within hours of the work, so today's month stands in for the job's month.
    month = _dt.date.today().month
    for rule in cfg.get("seasonal_blocklist", []):
        if month not in rule["months"]:
            continue
        m = re.search(rule["pattern"], text_only)
        if m:
            issues.append({"level": "BLOCK", "check": "compliance-seasonal",
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
    # The document itself — every heading, button label and layout choice — is the
    # vertical pack's. The engine only computes the values and fills the slots.
    tokens = {
        "h1_esc": html.escape(page.get("h1", "")),
        "verdict_colour": colour.get(v, "#333"),
        "verdict": v,
        "quality": f"{page.get('quality_score')}",
        "usage_note": usage_note,
        "content_hash": content_hash,
        "needs_town": needs_town,
        "town_name_js": town_name_js,
        "title_tag_esc": html.escape(page.get("title_tag", "")),
        "site": cfg["site"],
        "slug_esc": html.escape(page.get("slug", "")),
        "meta_esc": html.escape(page.get("meta_description", "")),
        "rows": rows,
        "town_label_esc": html.escape(town.get("label", "")),
        "service_esc": html.escape(page.get("service", "")),
        "body_html": page.get("body_html", ""),
        "figs": figs,
        "links_line": " · ".join(html.escape(l["anchor"]) + " → " + html.escape(l["url"])
                                 for l in page.get("internal_links", [])),
        "facts": facts,
        "missing": missing,
        "followup_esc": html.escape(page.get("followup_question") or "(none needed)"),
        "schema_org_esc": html.escape(json.dumps(schema_org, indent=2)),
    }
    doc = vertical.pack_for(cfg).preview_template
    for k, val in tokens.items():
        doc = doc.replace("{{" + k + "}}", val)
    p = out / "preview.html"
    p.write_text(doc, encoding="utf-8")
    return p


# ------------------------------------------------------------------- main ---
def generate_job(cfg: Dict[str, Any], files: List[Path], crew_text: str,
                 out: Path, model: str = MODEL_DEFAULT, log=print) -> Dict[str, Any]:
    """Photos + sender text -> reviewed draft. Used by the CLI and the webhook receiver."""
    import anthropic

    out.mkdir(parents=True, exist_ok=True)
    pack = vertical.pack_for(cfg, log)
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
    obs, u1 = call(client, model, pack.observe_prompt, content, pack.observe_schema, effort="low")
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
    page, u2 = call(client, model, pack.write_prompt(cfg),
                    [{"type": "text", "text": json.dumps(brief, indent=2)}],
                    pack.page_schema(cfg, len(photos)))
    spend += u2.input_tokens / 1e6 * pin + u2.output_tokens / 1e6 * pout

    issues = run_guards(cfg, page, obs, gps_town)
    v = verdict(issues)
    town = {t["slug"]: t for t in cfg["towns"]}.get(page["town"], {"label": page["town"]})
    svc = {s["slug"]: s for s in cfg["services"]}.get(page["service"], {"label": page["service"]})
    # Region comes from the client config; no sensible default exists, so a config
    # without "region" yields the bare place name rather than a wrong state.
    schema_org = pack.jsonld(cfg, page, svc, {"place_name": _place_name(cfg, page, town)})
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
    ap.add_argument("--text", default="", help="what the sender texted in")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--out", default="out")
    ap.add_argument("--dry-run", action="store_true", help="validate without calling the API")
    args = ap.parse_args()

    here = Path(__file__).parent
    cfg_path = here / "clients" / f"{args.client}.json"
    if not cfg_path.exists():
        sys.exit(f"No config at {cfg_path}")
    # Explicit UTF-8: configs carry licence lines and voice text with non-ASCII
    # characters, and Windows otherwise decodes them as cp1252.
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    _require_pillow()
    files = collect_photos(args.photos)
    if not files:
        sys.exit("No usable photos found.")

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if os.path.isabs(args.out) else here / args.out
    out = out / f"{args.client}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    print(f"\n  {len(files)} photo(s) · client {cfg['business_name']}")
    print(f'  sender text: "{args.text or "(none)"}"')

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
    hub_slug = vertical.pack_for(cfg).hub["slug"]
    print(f"\n  {r['verdict']}   quality {page['quality_score']}   ${r['cost_usd']:.3f}")
    print(f"  {page['h1']}")
    # "->" not an arrow glyph: Windows consoles default to cp1252 and die on U+2192.
    print(f"  /{hub_slug}/{page['slug']}/   ->  {r['town_label']} - {r['service_label']}")
    for i in issues:
        print(f"    [{i['level']}] {i['check']}: {i['detail']}")
    if page.get("followup_question"):
        print(f'  would text back: "{page["followup_question"]}"')
    print(f"\n  open {out / 'preview.html'}\n")


if __name__ == "__main__":
    main()
