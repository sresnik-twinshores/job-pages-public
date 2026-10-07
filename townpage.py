#!/usr/bin/env python3
"""
townpage.py — create a service-area page for a town the client works in but has no page for.

Triggered from a draft that came back with a `no-town-page` finding: the sender worked in a
town inside the service area that nobody has written a page for yet. The approver gets a
button, and one tap turns a real job into the town page plus the job page.

Deliberately uses the WordPress REST API rather than the theme's PHP location data. The
data-driven pages look richer, but adding an entry means editing a live theme file from a
web request, and it would mean putting FTPS credentials on the receiver. Not worth it.

Facts come from OpenStreetMap where they can (county, neighbouring places); the model only
writes prose around them. Local claims a model invents are exactly the kind that read fine
and are wrong.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

import jobgen
import publish as wp_publish
import vertical

UA = wp_publish.UA
TIMEOUT = 30

# Census needs FIPS codes; client configs carry postal abbreviations.
STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09",
    "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17",
    "IN": "18", "IA": "19", "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24",
    "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29", "MT": "30", "NE": "31",
    "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38",
    "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53", "WV": "54",
    "WI": "55", "WY": "56",
}


def census_stats(town_name: str, region: str, log=print,
                 cache_dir: Optional[Path] = None) -> Dict[str, int]:
    """Median home value and population for the place, from the Census ACS 5-year
    API. Real, citable numbers or nothing — the stat blocks never carry a guess.
    One fetch per state, cached; the match strips CDP/village/town/city suffixes."""
    fips = STATE_FIPS.get((region or "").strip().upper())
    if not fips:
        return {}
    key = os.environ.get("CENSUS_API_KEY", "").strip()
    if not key:
        log("  census: CENSUS_API_KEY not set — skipping stats (free key: "
            "api.census.gov/data/key_signup.html)")
        return {}
    cache = (cache_dir or Path(tempfile.gettempdir())) / f"census-acs5-{fips}.json"
    data = None
    try:
        data = json.loads(cache.read_text())
    except Exception:
        pass
    if not data:
        try:
            r = requests.get(
                "https://api.census.gov/data/2023/acs/acs5",
                params={"get": "NAME,B25077_001E,B01003_001E",
                        "for": "place:*", "in": f"state:{fips}", "key": key},
                timeout=TIMEOUT, headers={"User-Agent": UA})
            r.raise_for_status()
            data = r.json()
            try:
                cache.write_text(json.dumps(data))
            except Exception:
                pass
        except Exception as e:
            log(f"  ! census lookup failed: {e}")
            return {}
    want = town_name.strip().lower()
    for row in data[1:]:
        base = row[0].split(",")[0].strip().lower()
        for suf in (" cdp", " village", " town", " city", " borough"):
            if base.endswith(suf):
                base = base[: -len(suf)]
        if base.strip() == want:
            out: Dict[str, int] = {}
            for key, idx in (("median_home_value", 1), ("population", 2)):
                try:
                    v = int(row[idx])
                    if v > 0:          # ACS uses large negative sentinels for N/A
                        out[key] = v
                except (TypeError, ValueError):
                    pass
            return out
    log(f"  census: no place match for {town_name!r} in state {fips}")
    return {}


def _tile_xy(lat: float, lon: float, zoom: int):
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def hero_map(lat: float, lon: float, out_path: Path, log=print,
             width: int = 1376, height: int = 768, zoom: int = 14) -> bool:
    """Stitch OpenStreetMap tiles into a hero image of the actual area — a real
    map of the real place, not invented scenery. The theme's scrim handles text
    contrast. OSM attribution goes in the image caption (licence requirement)."""
    try:
        from PIL import Image
    except ImportError:
        return False
    xc, yc = _tile_xy(lat, lon, zoom)
    px0, py0 = xc * 256 - width / 2, yc * 256 - height / 2
    tx0, ty0 = int(px0 // 256), int(py0 // 256)
    tx1, ty1 = int((px0 + width) // 256), int((py0 + height) // 256)
    canvas = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256), "#e8e4de")
    try:
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                r = requests.get(f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png",
                                 timeout=TIMEOUT, headers={"User-Agent": UA})
                if r.status_code != 200:
                    log(f"  ! map tile {zoom}/{tx}/{ty} -> {r.status_code}")
                    return False
                canvas.paste(Image.open(io.BytesIO(r.content)).convert("RGB"),
                             ((tx - tx0) * 256, (ty - ty0) * 256))
    except Exception as e:
        log(f"  ! map hero failed: {e}")
        return False
    left, top = int(px0 - tx0 * 256), int(py0 - ty0 * 256)
    canvas.crop((left, top, left + width, top + height)).save(out_path, "JPEG", quality=82)
    return True


def nearby_places(lat: float, lon: float, limit: int = 8) -> List[str]:
    """Neighbouring settlements from OSM — real names, not remembered ones."""
    q = f"""[out:json][timeout:20];
      (node["place"~"^(town|village|hamlet|suburb)$"](around:9000,{lat},{lon}););
      out body {limit * 3};"""
    try:
        r = requests.post("https://overpass-api.de/api/interpreter", data={"data": q},
                          timeout=TIMEOUT, headers={"User-Agent": UA})
        els = r.json().get("elements", [])
        seen, out = set(), []
        for e in els:
            n = (e.get("tags") or {}).get("name")
            if n and n not in seen:
                seen.add(n)
                out.append(n)
        return out[:limit]
    except Exception:
        return []


def generate(cfg: Dict[str, Any], town_name: str, county: str,
             neighbours: List[str], model: str = jobgen.MODEL_DEFAULT,
             log=print) -> Dict[str, Any]:
    import anthropic
    client = anthropic.Anthropic()

    pack = vertical.pack_for(cfg, log)
    system = pack.area_prompt(cfg)

    brief = {
        "town": town_name,
        "county": county,
        "neighbouring_places_from_openstreetmap": neighbours,
        "services_offered": [s["label"] for s in cfg["services"]][:12],
        "licences": cfg.get("licences", ""),
        "business": cfg["business_name"],
    }
    r = client.messages.create(
        model=model, max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": json.dumps(brief, indent=2)}],
        output_config={"format": {"type": "json_schema", "schema": pack.area_schema}},
        thinking={"type": "adaptive"},
    )
    text = next(b.text for b in r.content if b.type == "text")
    out = json.loads(text)
    out["_usage"] = {"in": r.usage.input_tokens, "out": r.usage.output_tokens}
    log(f"  town page written for {town_name} ({len(out['confidence_notes'])} things to verify)")
    return out


def build_html(cfg: Dict[str, Any], town_name: str, data: Dict[str, Any],
               receiver: str, client_id: str,
               hero_img: str = "", stats_block: str = "") -> str:
    site = cfg["site"].rstrip("/")
    svc = cfg["services"][:6]
    links = "".join(
        f'<li><a href="{site}{s["url"]}">{s["label"]}</a></li>' for s in svc)
    lic = (f'<p class="town-licences">{cfg["licences"]}</p>'
           if cfg.get("licences") and "TODO" not in cfg["licences"] else "")
    tokens = {
        "intro": data["intro"],
        "local_context": data["local_context"],
        "services_line": data["services_line"],
        "area_name": town_name,
        "links": links,
        "licences_block": lic,
        "hero_img": hero_img,
        "stats_block": stats_block,
        "site": site,
        "receiver": receiver,
        "client_id": client_id,
    }
    doc = vertical.pack_for(cfg).area_page_template.rstrip("\n")
    for k, val in tokens.items():
        doc = doc.replace("{{" + k + "}}", val)
    return doc


def add_area_index_link(wp: Dict[str, Any], hub_slug: str, parent_id: int,
                        slug: str, town_name: str, log=print) -> bool:
    """Add the new area page to the pill links on its parent index page.

    A page that publishes without appearing on the index is invisible to visitors.
    The container is matched by class; a theme that names it differently sets
    wordpress.area_index_container_class. A missing container is logged and skipped
    rather than failing the page create — the page itself is the thing that matters.
    """
    base = wp["base"].rstrip("/")
    api = base + "/wp-json/wp/v2/"
    auth = wp_publish._auth(wp)
    head = wp_publish._headers()
    klass = wp.get("area_index_container_class", "town-pills")
    try:
        r = requests.get(f"{api}pages/{parent_id}", params={"context": "edit"},
                         auth=auth, timeout=TIMEOUT, headers=head)
        r.raise_for_status()
        raw = r.json()["content"]["raw"]
        href = f"/{hub_slug}/{slug}/"
        if href in raw:
            return True
        m = re.search(rf'(<div class="{re.escape(klass)}">)(.*?)(</div>)', raw, re.S)
        if not m:
            log(f"  ! no .{klass} container on the area index page — link not added")
            return False
        inner = m.group(2).rstrip() + f'\n<a href="{href}">{town_name}</a>\n'
        new_raw = raw[:m.start()] + m.group(1) + inner + m.group(3) + raw[m.end():]
        r = requests.post(f"{api}pages/{parent_id}", auth=auth, timeout=TIMEOUT,
                          headers=head, json={"content": new_raw})
        r.raise_for_status()
        log(f"  area index updated: {href}")
        return True
    except Exception as e:
        log(f"  ! area index update failed: {e}")
        return False


def create(cfg: Dict[str, Any], wp: Dict[str, Any], town_name: str, slug: str,
           county: str, latlon: Optional[List[float]], receiver: str, client_id: str,
           model: str = jobgen.MODEL_DEFAULT, log=print) -> Dict[str, Any]:
    base = wp["base"].rstrip("/")
    api = base + "/wp-json/wp/v2/"
    auth = wp_publish._auth(wp)
    head = wp_publish._headers()

    # already there?
    r = requests.get(api + "pages", params={"slug": slug, "status": "publish,draft"},
                     auth=auth, timeout=TIMEOUT, headers=head)
    if r.status_code == 200 and r.json():
        return {"ok": False, "reason": "exists", "id": r.json()[0]["id"],
                "url": r.json()[0].get("link")}

    hub = wp.get("service_areas_parent_slug", vertical.pack_for(cfg).hub["area_parent_slug"])
    r = requests.get(api + "pages", params={"slug": hub}, auth=auth, timeout=TIMEOUT, headers=head)
    parent = r.json()[0]["id"] if r.status_code == 200 and r.json() else 0
    if not parent:
        return {"ok": False, "reason": f"no /{hub}/ parent page found"}

    neigh = nearby_places(latlon[0], latlon[1]) if latlon else []
    data = generate(cfg, town_name, county, neigh, model, log)

    # Hero: a real map of the real place, uploaded to the client's media library.
    hero_img = ""
    if latlon:
        tmp = Path(tempfile.gettempdir()) / f"area-hero-{slug}.jpg"
        if hero_map(latlon[0], latlon[1], tmp, log):
            try:
                m = wp_publish.upload_photo(
                    wp, tmp, f"Map of the {town_name} area",
                    f"The {town_name} area. Map data © OpenStreetMap contributors.", log)
                hero_img = (f'<img class="page-hero-bg" src="{m["source_url"]}" '
                            f'alt="Map of the {town_name} area" width="1376" height="768">')
            except Exception as e:
                log(f"  ! hero upload failed: {e}")

    # Stats: only what the Census actually says, plus the county we already know.
    stats = census_stats(town_name, cfg.get("region", ""), log)
    items: List[tuple] = []
    if stats.get("median_home_value"):
        items.append((f"${round(stats['median_home_value'] / 1000)}k",
                      "median home value (U.S. Census ACS)"))
    if stats.get("population"):
        items.append((f"{stats['population']:,}", "residents (U.S. Census ACS)"))
    if county:
        items.append((county.replace(" County", ""), "county"))
    stats_block = ""
    if items:
        cells = "".join(f'<div class="stat"><b>{v}</b><span>{l}</span></div>'
                        for v, l in items)
        stats_block = f'<div class="explorer-stats" style="margin-top:18px">{cells}</div>'

    body = {
        "title": data["title"],
        "slug": slug,
        "status": wp.get("town_page_status", "draft"),
        "parent": parent,
        "excerpt": data["meta_description"],
        "content": build_html(cfg, town_name, data, receiver.rstrip("/"), client_id,
                              hero_img=hero_img, stats_block=stats_block),
    }
    tmpl = wp.get("location_template", "")
    if tmpl:
        body["template"] = tmpl

    r = requests.post(api + "pages", auth=auth, timeout=TIMEOUT, headers=head, json=body)
    if r.status_code not in (200, 201):
        return {"ok": False, "reason": f"{r.status_code} {r.text[:200]}"}
    out = r.json()
    log(f"  created {out.get('link')} (status {out.get('status')})")

    # A live page earns its spot on the index immediately. A draft stays off it —
    # linking to an unpublished page would 404 for every visitor.
    if out.get("status") == "publish":
        add_area_index_link(wp, hub, parent, slug, town_name, log)

    return {"ok": True, "id": out["id"], "url": out.get("link"),
            "status": out.get("status"), "slug": slug,
            "verify": data.get("confidence_notes", [])}
