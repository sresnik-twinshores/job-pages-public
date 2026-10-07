#!/usr/bin/env python3
"""
publish.py — put an approved draft on the client's WordPress site.

Job pages are published as CHILD PAGES of the pack's hub parent, not a custom post type.
That gives the URL we want (/<hub-slug>/<slug>/) through the REST API alone — no CPT
registration, so no functions.php edit, which on this host means no FTPS deploy.

Cache note: a brand-new URL is not in Cloudflare's edge (404s are BYPASS, verified against
the live site), so the page is visible immediately. What goes stale is the town/service
pages and the sitemap — those need the weekly manual purge.
"""
from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

import vertical

TIMEOUT = 45

# Cloudflare (in front of this host, and most WP sites) 403s the default python-requests
# User-Agent outright. Identify the integration by name instead.
# Cloudflare 403s the default python-requests UA. Identify by name; override per agency.
UA = os.environ.get(
    "JOB_PAGES_UA", "JobPages/1.0")   # set JOB_PAGES_UA to identify YOUR deployment to site owners


def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {"User-Agent": UA, "Accept": "application/json"}
    if extra:
        h.update(extra)
    return h


class PublishError(RuntimeError):
    pass


def _auth(wp: Dict[str, Any]) -> Tuple[str, str]:
    pw = os.environ.get(wp.get("app_password_env", ""), "")
    if not pw:
        raise PublishError(f"env var {wp.get('app_password_env')!r} is empty or unset")
    if not wp.get("user"):
        raise PublishError("wp.user is not configured")
    return (wp["user"], pw)


def _api(wp: Dict[str, Any], path: str) -> str:
    return wp["base"].rstrip("/") + "/wp-json/wp/v2/" + path.lstrip("/")


def find_or_create_parent(wp: Dict[str, Any], hub: Dict[str, Any], log=print) -> int:
    """The hub parent page. Created once, then reused. Slug, title and copy come
    from the vertical pack's hub config; the client's wordpress block overrides."""
    slug = wp.get("parent_slug", hub["slug"])
    r = requests.get(_api(wp, "pages"), params={"slug": slug, "status": "publish,draft"},
                     auth=_auth(wp), timeout=TIMEOUT, headers=_headers())
    r.raise_for_status()
    hits = r.json()
    if hits:
        log(f"  parent /{slug}/ exists (id {hits[0]['id']})")
        return hits[0]["id"]

    log(f"  creating parent page /{slug}/")
    r = requests.post(_api(wp, "pages"), auth=_auth(wp), timeout=TIMEOUT,
                      headers=_headers(), json={
        "title": wp.get("parent_title", hub["parent_title"]),
        "slug": slug,
        "status": "publish",
        "content": hub["parent_content"],
    })
    if r.status_code not in (200, 201):
        raise PublishError(f"could not create parent page: {r.status_code} {r.text[:300]}")
    return r.json()["id"]


def upload_photo(wp: Dict[str, Any], path: Path, alt: str, caption: str, log=print) -> Dict[str, Any]:
    ctype = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    # A descriptive filename is a small, free SEO win over photo-0.jpg
    fname = re.sub(r"[^a-z0-9]+", "-", alt.lower()).strip("-")[:70] or path.stem
    fname = f"{fname}{path.suffix or '.jpg'}"
    r = requests.post(
        _api(wp, "media"), auth=_auth(wp), timeout=TIMEOUT,
        headers=_headers({"Content-Disposition": f'attachment; filename="{fname}"',
                          "Content-Type": ctype}),
        data=path.read_bytes(),
    )
    if r.status_code not in (200, 201):
        raise PublishError(f"media upload failed: {r.status_code} {r.text[:300]}")
    m = r.json()
    # alt_text has to be set in a second call; it is ignored on the binary upload
    requests.post(_api(wp, f"media/{m['id']}"), auth=_auth(wp), timeout=TIMEOUT,
                  headers=_headers(),
                  json={"alt_text": alt, "caption": caption, "title": alt[:120]})
    log(f"  uploaded {fname} (id {m['id']})")
    return m


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def retag_photo(wp: Dict[str, Any], media_id: int, alt: str, caption: str,
                log=print) -> Dict[str, Any]:
    """Re-point an existing attachment's alt/caption at freshly generated text.

    An update regenerates every caption, but the bytes are usually the same photos. Editing
    the attachment in place keeps one copy in the media library instead of a duplicate per
    revision, and keeps the URL the live page already references.
    """
    r = requests.post(_api(wp, f"media/{media_id}"), auth=_auth(wp), timeout=TIMEOUT,
                      headers=_headers(),
                      json={"alt_text": alt, "caption": caption, "title": alt[:120]})
    if r.status_code not in (200, 201):
        raise PublishError(f"media retag failed: {r.status_code} {r.text[:300]}")
    log(f"  reused media {media_id}")
    return r.json()


def build_html(page: Dict[str, Any], media: List[Dict[str, Any]],
               schema_org: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    pack = vertical.pack_for(cfg)
    cap = {p["index"]: p for p in page.get("photos", [])}
    figures = []
    for i, m in enumerate(media):
        c = cap.get(i, {})
        figures.append(
            f'<figure class="wp-block-image size-large">'
            f'<img src="{m["source_url"]}" alt="{c.get("alt","")}" '
            f'class="wp-image-{m["id"]}" loading="lazy"/>'
            f'<figcaption>{c.get("caption","")}</figcaption></figure>')

    site = cfg["site"].rstrip("/")
    links = page.get("internal_links") or []
    links_html = (" · ".join(f'<a href="{site}{l["url"]}">{l["anchor"]}</a>' for l in links)
                  if links else "")
    schema_tag = f'<script type="application/ld+json">{json.dumps(schema_org)}</script>'

    # The published page's LAYOUT belongs to the vertical, exactly like the
    # preview and area templates. The engine computes values; the pack decides
    # what the page looks like. Packs without a wp_page template get the
    # original plain layout below.
    if pack.wp_page_template:
        towns = {t["slug"]: t for t in cfg.get("towns", [])}
        town_label = towns.get(page.get("town"), {}).get("label", page.get("town_name", ""))
        tokens = {
            "h1_esc": html.escape(page.get("h1", "")),
            "kicker_esc": html.escape(f'{pack.hub.get("parent_title", "")} · '
                                      f'{page.get("town_name") or town_label}'),
            "hero_photo_url": media[0]["source_url"] if media else "",
            "gallery_figs": "\n".join(figures[1:] if media else figures),
            "body_html": page.get("body_html", ""),
            "links_html": (f'<p class="job-page-links">{links_html}</p>' if links_html else ""),
            "schema_tag": schema_tag,
        }
        doc = pack.wp_page_template
        for k, v in tokens.items():
            doc = doc.replace("{{" + k + "}}", v)
        return doc

    # Legacy layout. Order is still the vertical's call via hub.json.
    if pack.hub.get("photos_first"):
        parts = figures + [page["body_html"]]
    else:
        parts = [page["body_html"]] + figures
    if links_html:
        parts.append(f'<p class="job-page-links">{links_html}</p>')
    parts.append(schema_tag)
    return "\n\n".join(parts)


def publish_job(cfg: Dict[str, Any], wp: Dict[str, Any], job_dir: Path, log=print,
                update_page_id: Optional[int] = None,
                prior_media: Optional[List[Dict[str, Any]]] = None,
                allow_blocked: bool = False) -> Dict[str, Any]:
    """Create the page, or - with update_page_id - rewrite an existing one in place.

    An update never changes the live URL. The generator rewrites the headline freely, and a
    new headline means a new slug; letting that through would move a page that already has
    links and rankings pointing at it. Title and body change, the address does not.

    allow_blocked is the human override for a false-positive BLOCK: a reviewer who has
    read the page and the matched rule can publish anyway. It is never set by automation -
    only the approve endpoint passes it, and only on an explicit second action.
    """
    draft = json.loads((job_dir / "draft.json").read_text())
    page = draft["page"]
    if draft["verdict"] == "BLOCKED" and not allow_blocked:
        raise PublishError("refusing to publish a BLOCKED draft")
    if draft["verdict"] == "BLOCKED":
        log("  ! BLOCK OVERRIDDEN by reviewer — publishing a draft the guards rejected")

    reuse = {m["sha"]: m for m in (prior_media or [])
             if isinstance(m, dict) and m.get("sha") and m.get("id")}

    media: List[Dict[str, Any]] = []
    cap = {p["index"]: p for p in page.get("photos", [])}
    for i, p in enumerate(sorted(job_dir.glob("photo-*.jpg"))):
        c = cap.get(i, {})
        sha = sha256_file(p)
        prev = reuse.get(sha)
        if prev:
            m = retag_photo(wp, prev["id"], c.get("alt", ""), c.get("caption", ""), log)
        else:
            m = upload_photo(wp, p, c.get("alt", ""), c.get("caption", ""), log)
        m["sha"] = sha
        media.append(m)

    body = {
        "title": page["h1"],
        "content": build_html(page, media, draft["schema_org"], cfg),
        "excerpt": page["meta_description"],
    }
    if media:
        body["featured_media"] = media[0]["id"]

    if update_page_id:
        # No slug, no status, no parent: an update rewrites content on a page that already
        # has a URL and a publication state a human chose.
        url = _api(wp, f"pages/{update_page_id}")
    else:
        body["slug"] = page["slug"]
        body["status"] = wp.get("publish_status", "draft")   # 'draft' until explicitly trusted
        body["parent"] = find_or_create_parent(wp, vertical.pack_for(cfg).hub, log)
        if wp.get("page_template"):
            body["template"] = wp["page_template"]
        url = _api(wp, "pages")

    r = requests.post(url, auth=_auth(wp), timeout=TIMEOUT, headers=_headers(), json=body)
    if r.status_code not in (200, 201):
        verb = "page update" if update_page_id else "page create"
        raise PublishError(f"{verb} failed: {r.status_code} {r.text[:400]}")
    out = r.json()
    log(f"  {'updated' if update_page_id else 'published'}: {out.get('link')} "
        f"(status {out.get('status')})")
    return {"id": out["id"], "url": out.get("link"), "status": out.get("status"),
            "media": [m.get("source_url") for m in media],
            "media_meta": [{"id": m.get("id"), "url": m.get("source_url"),
                            "sha": m.get("sha")} for m in media]}


def refresh_status(wp: Dict[str, Any], page_id: int) -> Optional[Dict[str, Any]]:
    """Re-read a page from WordPress. Someone hitting Publish in wp-admin happens outside
    this system, so the stored status would otherwise be stale forever."""
    try:
        r = requests.get(_api(wp, f"pages/{page_id}"), params={"context": "edit"},
                         auth=_auth(wp), timeout=TIMEOUT, headers=_headers())
        if r.status_code != 200:
            return None
        d = r.json()
        out = {"status": d.get("status"), "url": d.get("link")}
        # backfill the thumbnail for jobs published before media urls were recorded
        fm = d.get("featured_media")
        if fm:
            m = requests.get(_api(wp, f"media/{fm}"), auth=_auth(wp),
                             timeout=TIMEOUT, headers=_headers())
            if m.status_code == 200:
                src = m.json().get("source_url")
                if src:
                    out["media"] = [src]
        return out
    except Exception:
        return None


def ping_indexnow(wp: Dict[str, Any], url: str, log=print) -> bool:
    """Direct ping to Bing/IndexNow. Not affected by the Cloudflare edge cache."""
    key = os.environ.get(wp.get("indexnow_key_env", ""), "")
    if not key or not url:
        return False
    try:
        host = wp["base"].split("//", 1)[-1].strip("/")
        r = requests.get("https://api.indexnow.org/indexnow", timeout=20,
                         headers=_headers(), params={
            "url": url, "key": key, "keyLocation": f"{wp['base'].rstrip('/')}/{key}.txt"})
        log(f"  indexnow: {r.status_code}")
        return r.status_code in (200, 202)
    except Exception as e:
        log(f"  ! indexnow failed: {e}")
        return False
