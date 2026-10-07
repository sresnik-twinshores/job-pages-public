#!/usr/bin/env python3
"""
vertical.py — load and validate vertical packs.

A vertical pack is a folder under verticals/ that supplies everything
vertical-specific: the two generation prompts, the pass-1 and pass-2 schemas,
the JSON-LD template, the compliance floor, the hub wiring, and the HTML
templates (draft preview, area page). Engine code supplies the mechanism —
batching, guards, approval, publishing — and must contain no vertical
vocabulary; if a string names the domain, it belongs in a pack.

Clients select a pack in their config:

    "vertical": "home-services@1"

The version is pinned so a pack update never silently changes a live client's
behaviour — migrating a client to a new pack version is a deliberate act.

Design rules this module enforces:

- **Fail loud at load, not at the first text.** An unresolvable enum slot, an
  unrendered placeholder, or a malformed manifest raises PackError with the
  pack and field named. webhook_receiver runs validate_client() over every
  configured client at boot and refuses to start on any problem.
- **The compliance floor is a floor.** compliance_rules() returns the pack's
  baseline rules plus the client's own; a client config can add rules but has
  no way to remove the pack's.
- **Packs are declarative.** JSON and text templates only — no code is loaded
  from a pack.

Template conventions:
- prompt/HTML templates use {{placeholder}}, substituted from an explicit
  context; an unresolved {{...}} after rendering is an error.
- JSON schema templates may use "$client.<key>" as an enum value; it resolves
  to a list derived from the client config, and an empty result is an error
  (an empty enum is rejected by the API and every job would fail at pass 2).
- the JSON-LD template uses "$<context>.<field>" string references resolved
  against {client, page, service, computed}.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).parent
PACKS_DIR = HERE / "verticals"

# Grace default for configs written before packs existed. A config without
# "vertical" gets this pack and a loud warning; the field becomes required in
# Phase 2, at which point this constant (the one vertical name allowed in
# engine code) is removed.
DEFAULT_REF = "home-services@1"

_MANIFEST_REQUIRED = (
    "id", "version", "label", "required_intake_fields", "publish_exact_location",
    "strip_photo_gps", "map_pin", "prompts", "schemas", "jsonld",
    "compliance_floor", "hub", "templates",
)


class PackError(RuntimeError):
    pass


def _render(template: str, ctx: Dict[str, str], where: str) -> str:
    out = template
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", v)
    leftover = re.findall(r"\{\{[a-z0-9_]+\}\}", out)
    if leftover:
        raise PackError(f"{where}: unresolved placeholders {sorted(set(leftover))}")
    return out


def _resolve_slots(node: Any, slots: Dict[str, List[str]], where: str) -> Any:
    """Replace "$client.<key>" strings in a schema template with real lists."""
    if isinstance(node, dict):
        return {k: _resolve_slots(v, slots, where) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_slots(v, slots, where) for v in node]
    if isinstance(node, str) and node.startswith("$client."):
        key = node[len("$client."):]
        if key not in slots:
            raise PackError(f"{where}: unknown schema slot {node!r}")
        if not slots[key]:
            raise PackError(
                f"{where}: {node!r} resolved to an empty list — an empty enum is "
                f"rejected by the API and every job would fail at pass 2")
        return list(slots[key])
    return node


def _resolve_refs(node: Any, ctx: Dict[str, Any], where: str) -> Any:
    """Replace "$<context>.<field>" strings in the JSON-LD template."""
    if isinstance(node, dict):
        return {k: _resolve_refs(v, ctx, where) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(v, ctx, where) for v in node]
    if isinstance(node, str) and re.fullmatch(r"\$[a-z]+\.[a-z0-9_.]+", node):
        root, _, path = node[1:].partition(".")
        if root not in ctx:
            raise PackError(f"{where}: unknown reference context {node!r}")
        v: Any = ctx[root]
        for part in path.split("."):
            if not isinstance(v, dict) or part not in v:
                raise PackError(f"{where}: reference {node!r} not found")
            v = v[part]
        return v
    return node


class Pack:
    def __init__(self, path: Path, manifest: Dict[str, Any]):
        self.path = path
        self.manifest = manifest
        self.id: str = manifest["id"]
        self.version: int = manifest["version"]
        self.ref = f"{self.id}@{self.version}"

        def _text(rel: str) -> str:
            return (path / rel).read_text(encoding="utf-8")

        def _json(rel: str) -> Any:
            return json.loads(_text(rel))

        self.observe_prompt: str = _text(manifest["prompts"]["observe"])
        self._write_tmpl: str = _text(manifest["prompts"]["write"])
        self._area_tmpl: str = _text(manifest["prompts"]["area"])
        self.observe_schema: Dict[str, Any] = _json(manifest["schemas"]["observe"])
        self._page_schema_tmpl: Dict[str, Any] = _json(manifest["schemas"]["page"])
        self.area_schema: Dict[str, Any] = _json(manifest["schemas"]["area"])
        self._jsonld_tmpl: Dict[str, Any] = _json(manifest["jsonld"])
        self.compliance_floor: List[Dict[str, str]] = _json(manifest["compliance_floor"])["blocklist"]
        self.hub: Dict[str, Any] = _json(manifest["hub"])
        self.preview_template: str = _text(manifest["templates"]["preview"])
        self.area_page_template: str = _text(manifest["templates"]["area_page"])
        # Optional: a layout for the PUBLISHED page body. Packs without one get
        # the engine's plain body+figures layout (the original behaviour).
        wp_page = manifest["templates"].get("wp_page")
        self.wp_page_template: str = _text(wp_page) if wp_page else ""

    # ---- client-resolved surfaces -------------------------------------------
    def write_prompt(self, cfg: Dict[str, Any]) -> str:
        v = cfg["voice"]
        return _render(self._write_tmpl, {
            "business_name": cfg["business_name"],
            "voice_summary": v["summary"],
            "reading_level": v["reading_level"],
            "banned_style": ", ".join(v["banned_style"]),
            "title_max_chars": str(cfg["quality_gate"]["title_max_chars"]),
            "meta_max_chars": str(cfg["quality_gate"]["meta_max_chars"]),
        }, f"{self.ref} prompts.write")

    def area_prompt(self, cfg: Dict[str, Any]) -> str:
        v = cfg["voice"]
        return _render(self._area_tmpl, {
            "business_name": cfg["business_name"],
            "voice_summary": v["summary"],
            "banned_style": ", ".join(v["banned_style"]),
        }, f"{self.ref} prompts.area")

    def page_schema(self, cfg: Dict[str, Any], n_photos: int = 0) -> Dict[str, Any]:
        slots = {
            "service_slugs": [s["slug"] for s in cfg.get("services") or []],
            "town_slugs": [t["slug"] for t in cfg.get("towns") or []],
        }
        return _resolve_slots(deepcopy(self._page_schema_tmpl), slots,
                              f"{self.ref} schemas.page")

    def jsonld(self, cfg: Dict[str, Any], page: Dict[str, Any],
               service: Dict[str, Any], computed: Dict[str, Any]) -> Dict[str, Any]:
        return _resolve_refs(deepcopy(self._jsonld_tmpl),
                             {"client": cfg, "page": page, "service": service,
                              "computed": computed},
                             f"{self.ref} jsonld")

    def compliance_rules(self, cfg: Dict[str, Any]) -> List[Dict[str, str]]:
        return list(self.compliance_floor) + list(cfg["compliance_blocklist"])


_cache: Dict[str, Pack] = {}
_warned_no_vertical: set = set()


def load_pack(ref: str) -> Pack:
    if ref in _cache:
        return _cache[ref]
    m = re.fullmatch(r"([a-z0-9][a-z0-9-]*)@(\d+)", ref.strip())
    if not m:
        raise PackError(f"bad vertical reference {ref!r} — expected '<pack-id>@<version>'")
    pack_id, version = m.group(1), int(m.group(2))
    path = PACKS_DIR / pack_id
    if not (path / "pack.json").exists():
        known = sorted(p.name for p in PACKS_DIR.iterdir()
                       if (p / "pack.json").exists()) if PACKS_DIR.exists() else []
        raise PackError(f"no vertical pack {pack_id!r} under verticals/ — known: {known}")
    manifest = json.loads((path / "pack.json").read_text(encoding="utf-8"))
    missing = [k for k in _MANIFEST_REQUIRED if k not in manifest]
    if missing:
        raise PackError(f"{pack_id}: pack.json missing {missing}")
    if manifest["id"] != pack_id:
        raise PackError(f"{pack_id}: pack.json says id={manifest['id']!r}")
    if int(manifest["version"]) != version:
        raise PackError(
            f"{pack_id}: this tree has version {manifest['version']}, config pins @{version} — "
            f"deploy the matching pack version or migrate the client deliberately")
    try:
        pack = Pack(path, manifest)
    except FileNotFoundError as e:
        raise PackError(f"{pack_id}: referenced pack file missing — {e}") from e
    _cache[ref] = pack
    return pack


def pack_for(cfg: Dict[str, Any], log=print) -> Pack:
    ref = (cfg.get("vertical") or "").strip()
    if not ref:
        cid = cfg.get("client_id", "?")
        if cid not in _warned_no_vertical:
            _warned_no_vertical.add(cid)
            log(f"! config for {cid!r} has no 'vertical' field — assuming {DEFAULT_REF} "
                f"(grace default; the field becomes required in Phase 2)")
        ref = DEFAULT_REF
    return load_pack(ref)


def validate_client(cfg: Dict[str, Any], pack: Pack) -> List[str]:
    """Everything that must hold for this client to run on this pack.
    Returns problems; empty list means good. Run at receiver boot — a config
    that fails here used to fail days later as a sender-facing 'something broke'."""
    problems: List[str] = []
    for field in pack.manifest["required_intake_fields"]:
        if not cfg.get(field):
            problems.append(f"missing or empty required field {field!r}")
    if problems:
        return problems  # the checks below assume the fields exist

    try:
        pack.page_schema(cfg)
    except PackError as e:
        problems.append(str(e))
    try:
        pack.write_prompt(cfg)
        pack.area_prompt(cfg)
    except (PackError, KeyError) as e:
        problems.append(f"prompt rendering failed: {e}")

    for i, rule in enumerate(pack.compliance_rules(cfg)):
        try:
            re.compile(rule["pattern"])
        except (re.error, KeyError, TypeError) as e:
            problems.append(f"compliance rule #{i} invalid: {e}")
    for i, rule in enumerate(cfg.get("seasonal_blocklist") or []):
        try:
            re.compile(rule["pattern"])
            if not rule.get("months"):
                problems.append(f"seasonal rule #{i} has no months")
        except (re.error, KeyError, TypeError) as e:
            problems.append(f"seasonal rule #{i} invalid: {e}")
    return problems
