#!/usr/bin/env python3
"""
townpage.py — create a service-area page for a town the client works in but has no page for.

Triggered from a draft that came back with a `no-town-page` finding: the crew worked in a
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

import json
from typing import Any, Dict, List, Optional

import requests

import jobgen
import publish as wp_publish

UA = wp_publish.UA
TIMEOUT = 30


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


SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Page title, e.g. 'Windows, Doors & Siding in Yaphank, NY'"},
        "meta_description": {"type": "string"},
        "intro": {"type": "string", "description": "2-3 sentences. What the company does in this town."},
        "local_context": {"type": "string",
                           "description": "One paragraph on housing stock and conditions that "
                                          "genuinely affect the work here. Only what you are "
                                          "confident about; vaguer is better than wrong."},
        "services_line": {"type": "string", "description": "One sentence naming the services offered."},
        "confidence_notes": {"type": "array", "items": {"type": "string"},
                              "description": "Anything asserted that a human should verify."},
    },
    "required": ["title", "meta_description", "intro", "local_context",
                 "services_line", "confidence_notes"],
    "additionalProperties": False,
}


def generate(cfg: Dict[str, Any], town_name: str, county: str,
             neighbours: List[str], model: str = jobgen.MODEL_DEFAULT,
             log=print) -> Dict[str, Any]:
    import anthropic
    client = anthropic.Anthropic()

    system = f"""You write a service-area page for {cfg['business_name']}.

{cfg['voice']['summary']}
Never use: {', '.join(cfg['voice']['banned_style'])}. No em dashes.

THE RULE THAT MATTERS: only write local detail you are genuinely confident about. A page that
says "homes here range from postwar capes to newer colonials" and is right beats one naming a
specific neighbourhood, landmark or statistic that is wrong. Anything you are less than sure
of goes in confidence_notes instead of the page.

COMPLIANCE - these carry legal exposure, never write them:
no tax credits, no rebates, no savings percentages or dollar figures, no financing or monthly
payments, no superlatives about awards, no claim the company manufactures its own product.
Do not invent review counts, years in business, or job numbers."""

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
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        thinking={"type": "adaptive"},
    )
    text = next(b.text for b in r.content if b.type == "text")
    out = json.loads(text)
    out["_usage"] = {"in": r.usage.input_tokens, "out": r.usage.output_tokens}
    log(f"  town page written for {town_name} ({len(out['confidence_notes'])} things to verify)")
    return out


def build_html(cfg: Dict[str, Any], town_name: str, data: Dict[str, Any],
               receiver: str, client_id: str) -> str:
    site = cfg["site"].rstrip("/")
    svc = cfg["services"][:6]
    links = "".join(
        f'<li><a href="{site}{s["url"]}">{s["label"]}</a></li>' for s in svc)
    lic = (f'<p class="town-licences">{cfg["licences"]}</p>'
           if cfg.get("licences") and "TODO" not in cfg["licences"] else "")
    return f"""<p>{data['intro']}</p>

<p>{data['local_context']}</p>

<h2>What we install in {town_name}</h2>
<p>{data['services_line']}</p>
<ul>{links}</ul>
{lic}

<!-- job-pages: recent work in this town, filled from the live feed -->
<section class="section jp-town-projects" data-jp-town="{town_name}" hidden>
  <h2>Recent projects in {town_name}</h2>
  <ul class="jp-grid"></ul>
  <p class="jp-town-all"><a href="{site}/projects/">See all recent projects</a></p>
</section>
<link rel="stylesheet" href="{receiver}/hub/{client_id}/hub.css">
<script src="{receiver}/hub/{client_id}/town.js" defer></script>"""


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

    hub = wp.get("service_areas_parent_slug", "service-areas")
    r = requests.get(api + "pages", params={"slug": hub}, auth=auth, timeout=TIMEOUT, headers=head)
    parent = r.json()[0]["id"] if r.status_code == 200 and r.json() else 0
    if not parent:
        return {"ok": False, "reason": f"no /{hub}/ parent page found"}

    neigh = nearby_places(latlon[0], latlon[1]) if latlon else []
    data = generate(cfg, town_name, county, neigh, model, log)

    body = {
        "title": data["title"],
        "slug": slug,
        "status": wp.get("town_page_status", "draft"),
        "parent": parent,
        "excerpt": data["meta_description"],
        "content": build_html(cfg, town_name, data, receiver.rstrip("/"), client_id),
    }
    tmpl = wp.get("location_template", "")
    if tmpl:
        body["template"] = tmpl

    r = requests.post(api + "pages", auth=auth, timeout=TIMEOUT, headers=head, json=body)
    if r.status_code not in (200, 201):
        return {"ok": False, "reason": f"{r.status_code} {r.text[:200]}"}
    out = r.json()
    log(f"  created {out.get('link')} (status {out.get('status')})")
    return {"ok": True, "id": out["id"], "url": out.get("link"),
            "status": out.get("status"), "slug": slug,
            "verify": data.get("confidence_notes", [])}
