#!/usr/bin/env python3
"""
hub_page.py — generate the HTML for the /projects/ hub page.

Deliberately self-contained: the whole thing (grid + map) lives in the WordPress page's
own content, pulling from the receiver's JSON feed. No theme template, so no functions.php
edit and no FTPS deploy — which on this host is the difference between minutes and hours.

Leaflet + OpenStreetMap tiles: no API key, no billing, no Google dependency.

  python hub_page.py --client example-co --feed https://.../feed/example-co/projects.json
"""
from __future__ import annotations
import argparse

# Markup only. No inline JS: WordPress runs wpautop over post content and injects <br>
# and <p> into script bodies, which silently breaks them. Behaviour lives in hub.js,
# served by the receiver, where nothing can rewrite it.
PAGE_HTML = """<!-- wp:html -->
<div class="jp-hub">
  <div id="jp-map" aria-label="Map of recent project locations"></div>
  <ul class="jp-grid" id="jp-grid"></ul>
  <div class="jp-empty" id="jp-empty" hidden>Recent projects will appear here.</div>
</div>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="__BASE__/hub/__CLIENT__/hub.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="__BASE__/hub/__CLIENT__/hub.js"></script>
<!-- /wp:html -->"""

HUB_CSS = """:root{--jp-line:#e3e3e3}
.jp-hub{--jp-line:#e3e3e3}
#jp-map{height:420px;border-radius:12px;margin:0 0 28px;border:1px solid var(--jp-line,#e3e3e3);z-index:0}
.jp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:22px;margin:0;padding:0;list-style:none}
.jp-card{border:1px solid var(--jp-line,#e3e3e3);border-radius:12px;overflow:hidden;background:#fff}
.jp-card a{text-decoration:none;color:inherit;display:block}
.jp-card img{width:100%;height:170px;object-fit:cover;display:block}
.jp-card .jp-body{padding:13px 15px 16px}
.jp-card h3{font-size:16px;line-height:1.35;margin:0 0 6px}
.jp-town{font-size:13px;text-transform:uppercase;letter-spacing:.04em;opacity:.65;margin:0 0 8px}
.jp-sum{font-size:14px;line-height:1.5;opacity:.8;margin:0}
.jp-empty{padding:30px;text-align:center;opacity:.7;border:1px dashed var(--jp-line,#e3e3e3);border-radius:12px}
@media(max-width:600px){#jp-map{height:300px}}
.jp-town-projects .jp-grid{margin-top:1.25rem}
.jp-town-projects .jp-town-all{margin-top:1.25rem}"""

HUB_JS = """(function () {
  var FEED = "__FEED__";
  var grid = document.getElementById('jp-grid');
  var empty = document.getElementById('jp-empty');
  if (!grid) return;

  function esc(s) { return String(s || '').replace(/[&<>"]/g, function (c) {
    return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]; }); }

  fetch(FEED).then(function (r) { return r.json(); }).then(function (d) {
    var items = (d.projects || []).filter(function (p) { return p.url; });
    if (!items.length) { empty.hidden = false; return; }

    items.forEach(function (p) {
      var li = document.createElement('li');
      li.className = 'jp-card';
      li.innerHTML = '<a href="' + esc(p.url) + '">'
        + (p.thumb ? '<img src="' + esc(p.thumb) + '" alt="' + esc(p.title) + '" loading="lazy">' : '')
        + '<div class="jp-body">'
        + (p.town ? '<p class="jp-town">' + esc(p.town) + '</p>' : '')
        + '<h3>' + esc(p.title) + '</h3>'
        + '<p class="jp-sum">' + esc(p.summary) + '</p>'
        + '</div></a>';
      grid.appendChild(li);
    });

    var pts = items.filter(function (p) { return p.lat && p.lon; });
    var mapEl = document.getElementById('jp-map');
    if (!pts.length || typeof L === 'undefined') { if (mapEl) mapEl.style.display = 'none'; return; }
    var map = L.map('jp-map', {scrollWheelZoom: false});
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 17, attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);

    var byTown = {};
    pts.forEach(function (p) { (byTown[p.town] = byTown[p.town] || []).push(p); });
    var bounds = [];
    Object.keys(byTown).forEach(function (town) {
      var list = byTown[town], p = list[0];
      bounds.push([p.lat, p.lon]);
      var html = '<strong>' + esc(town) + '</strong><br>' + list.map(function (j) {
        return '<a href="' + esc(j.url) + '">' + esc(j.title) + '</a>';
      }).join('<br>');
      L.marker([p.lat, p.lon]).addTo(map).bindPopup(html);
    });
    map.fitBounds(bounds, {padding: [40, 40], maxZoom: 11});
  }).catch(function () { if (empty) empty.hidden = false; });
})();"""



TOWN_JS = """(function () {
  var host = document.querySelector('[data-jp-town]');
  if (!host) return;
  var town = (host.getAttribute('data-jp-town') || '').trim().toLowerCase();
  var grid = host.querySelector('.jp-grid');
  var FEED = "__FEED__";

  function esc(s) { return String(s || '').replace(/[&<>"]/g, function (c) {
    return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]; }); }

  fetch(FEED).then(function (r) { return r.json(); }).then(function (d) {
    var items = (d.projects || []).filter(function (p) {
      return p.url && (p.town || '').trim().toLowerCase() === town;
    }).slice(0, 6);
    // No projects in this town yet - leave the section hidden rather than show an
    // empty heading. The page is edge-cached for 30 days, so this fills in on its own
    // as jobs land, without a purge.
    if (!items.length) return;

    items.forEach(function (p) {
      var li = document.createElement('li');
      li.className = 'jp-card';
      li.innerHTML = '<a href="' + esc(p.url) + '">'
        + (p.thumb ? '<img src="' + esc(p.thumb) + '" alt="' + esc(p.title) + '" loading="lazy">' : '')
        + '<div class="jp-body"><h3>' + esc(p.title) + '</h3>'
        + '<p class="jp-sum">' + esc(p.summary) + '</p></div></a>';
      grid.appendChild(li);
    });
    host.hidden = false;
  }).catch(function () { /* leave hidden */ });
})();"""


def build_town_js(feed_url: str) -> str:
    return TOWN_JS.replace("__FEED__", feed_url)


def build(base: str, client_id: str) -> str:
    return (PAGE_HTML.replace("__BASE__", base.rstrip("/"))
                     .replace("__CLIENT__", client_id))


def build_js(feed_url: str) -> str:
    return HUB_JS.replace("__FEED__", feed_url)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--client", required=True)
    ap.add_argument("--out", default="hub-page.html")
    a = ap.parse_args()
    open(a.out, "w").write(build(a.base, a.client))
    print(f"wrote {a.out}")
