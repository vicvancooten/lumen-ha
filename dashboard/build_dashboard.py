"""Lumen dashboard generator.

Builds a floor -> room Lovelace dashboard from a small YAML description of your home
(see house.example.yaml) and saves it to Home Assistant over the WebSocket API.

    python3 build_dashboard.py house.yaml            # generate and push
    python3 build_dashboard.py house.yaml --dry-run  # only write <url_path>.json locally
    python3 build_dashboard.py house.yaml --rehaunt  # regenerate the haunted room photos

The access token is read from $HA_TOKEN, or from the file named in
home_assistant.token_file.
"""
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Optional: dependencies vendored with `pip install --target pylib -r requirements.txt`.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "pylib"))
import websocket  # noqa: E402
import yaml  # noqa: E402

CFG = {}
HA = {}
FLOORS = []
ROOMS = {}
STATES = {}
URL_PATH = "home-dashboard"
CFG_DIR = "."


# --------------------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------------------
def ent(item):
    """Entity items can be `light.x` or `{entity: light.x, name: ..., icon: ..., color: ...}`."""
    return {"entity": item} if isinstance(item, str) else dict(item)


def ents(items):
    return [ent(i) for i in items or []]


def ids(items):
    return [e["entity"] for e in ents(items)]


def room_lights(room):
    return ids(room.get("lights"))


def all_lights():
    return [l for r in ROOMS.values() for l in room_lights(r)]


def usable(entity):
    return STATES.get(entity, {}).get("state") not in (None, "unavailable")


def friendly(entity, strip=""):
    name = STATES.get(entity, {}).get("attributes", {}).get("friendly_name", entity)
    return name.replace(strip, "").strip() if strip else name


# --------------------------------------------------------------------------------------
# Card helpers
# --------------------------------------------------------------------------------------
def grid(cols=12, rows=None):
    g = {"columns": cols}
    if rows is not None:
        g["rows"] = rows
    return {"grid_options": g}


def heading(text, icon=None, style="title", badges=None, tap=None):
    c = {"type": "heading", "heading": text, "heading_style": style}
    if icon:
        c["icon"] = icon
    if badges:
        c["badges"] = badges
    if tap:
        c["tap_action"] = {"action": "navigate", "navigation_path": tap}
    return c


def badge(entity, color=None, **kw):
    b = {"type": "entity", "entity": entity, "show_state": True, "show_icon": True}
    if color:
        b["color"] = color
    b.update({k: v for k, v in kw.items() if v})
    return b


def tile(entity, cols=6, features=None, vertical=False, name=None, icon=None, color=None,
         tap=None, rows=None, state_content=None, hide_state=False):
    c = {"type": "tile", "entity": entity, "vertical": vertical}
    for k, v in (("name", name), ("icon", icon), ("color", color), ("tap_action", tap),
                 ("state_content", state_content)):
        if v:
            c[k] = v
    if hide_state:
        c["hide_state"] = True
    if features:
        c["features"] = features
        c["features_position"] = "bottom"
    c.update(grid(cols, rows))
    return c


def tile_from(item, cols=6, **kw):
    e = ent(item)
    kw.setdefault("name", e.get("name"))
    kw.setdefault("icon", e.get("icon"))
    kw["color"] = e.get("color") or kw.get("color")
    return tile(e["entity"], cols, **kw)


def section(cards, span=None):
    s = {"type": "grid", "cards": cards}
    if span:
        s["column_span"] = span
    return s


def mini_graph(entities, name=None, hours=24, cols=12, **extra):
    c = {
        "type": "custom:mini-graph-card",
        "entities": [{"entity": e, "name": n, "color": col} for e, n, col in entities],
        "hours_to_show": hours,
        "points_per_hour": 2,
        "line_width": 3,
        "smoothing": True,
        "font_size": 75,
        "height": 110,
        "show": {"fill": "fade", "labels": False, "legend": False, "extrema": False, "icon": False},
    }
    if name:
        c["name"] = name
    c.update(extra)
    c.update(grid(cols))
    return c


def has_brightness(entity):
    modes = STATES.get(entity, {}).get("attributes", {}).get("supported_color_modes") or []
    return any(m != "onoff" for m in modes)


def nav(path):
    return {"action": "navigate", "navigation_path": path}


def scene_tap(entity):
    # The same 1.5 s fade the house automations use.
    return {"action": "perform-action", "perform_action": "scene.turn_on", "target": {"entity_id": entity},
            "data": {"transition": 1.5}}


def lights_on(entities):
    return ("{{ [" + ",".join(f"'{e}'" for e in entities) + "] | select('is_state','on') | list | count }}")


def plural_lights(entities):
    n = lights_on(entities)[3:-3]
    return ("{% set n = " + n + " %}"
            "{{ 'All lights off' if n == 0 else n ~ (' light' if n == 1 else ' lights') ~ ' on' }}")


def room_card(area_id, room, cols=12, rows=None, navigate=True, banner=False, aspect=None):
    """room-summary-card: area photo background, lights up when any light is on,
    green border on motion, up to four one-tap device toggles.

    With `banner`, a room with a photo gets a hero instead: the card turns see-through and
    the photo becomes the page background (see hero_background), with the room name large on top."""
    entities = [{"entity_id": l, "on_color": "amber"} for l in room_lights(room)]
    entities += [{"entity_id": m, "on_color": "pink"} for m in ids(room.get("media"))[:1]]
    if room.get("vacuum"):
        entities.append({"entity_id": room["vacuum"]["entity"], "on_color": "teal"})
    # Many temperature sensors are diagnostic "device temperature" entities the card
    # won't average on its own, so list them explicitly.
    temps = [room["temperature"]] if room.get("temperature") else \
        [g["entity"] for g in ents(room.get("graphs")) if "temperature" in g["entity"]]
    c = {
        "type": "custom:room-summary-card",
        "area": area_id,
        "area_name": room["name"],
        "entities": entities[:4],
        "features": ["hide_area_stats", "multi_light_background", "full_card_actions",
                     "exclude_default_entities", "hide_room_icon"],
        "sensors": [t for t in temps if usable(t)],
        "sensor_classes": ["humidity"],
        "problem": {"display": "active_only"},
        "background": {"opacity": 70},
        # Photo cards sit on a dark base in both themes so white titles stay legible.
        "styles": {
            "card": {"border-radius": "28px", "background-color": PHOTO_BASE, "--lumen-photo-base": PHOTO_BASE},
            "title": {"color": "#fff", "text-shadow": "0 1px 8px rgba(0,0,0,.6)", "font-weight": "600"},
            "sensors": {"color": "rgba(255,255,255,.85)"},
        },
    }
    if not room.get("has_picture"):
        c["features"].remove("hide_room_icon")
        c["styles"] = {"card": {"border-radius": "28px"}}
    if room.get("fridge"):
        c["sensors"] = [{"entity_id": room["fridge"]["fridge_temp"], "icon": "mdi:fridge"}]
        c["sensor_classes"] = []
    if room_lights(room):
        c["lights"] = room_lights(room)
        c["entity"] = {"entity_id": room_lights(room)[0], "tap_action": {"action": "toggle"}}
    if room.get("motion"):
        c["occupancy"] = {"entities": ids(room["motion"])}
    if navigate:
        c["actions"] = {"tap_action": nav(f"/{URL_PATH}/{room['path']}")}
    if banner:
        aspect = "7/2"
    if aspect:
        c["styles"]["card"].update({"--user-grid-aspect-ratio": aspect, "aspect-ratio": aspect})
    elif rows:
        # A fixed grid height (the home page mosaic): let the card fill it instead of staying square.
        c["styles"]["card"].update({"--user-grid-aspect-ratio": "auto", "height": "100%"})
    style = [] if banner else [ROOM_HOVER_STYLE]
    hero = banner and room.get("picture")
    if hero:
        c["features"] = [f for f in c["features"] if f not in ("full_card_actions", "multi_light_background")]
        c["background"] = {"options": ["disable"]}
        c["styles"]["card"].update({
            "background-color": "transparent", "border": "none", "box-shadow": "none"})
        c["styles"]["title"] = {
            "color": "#fff", "font-size": "clamp(30px, 4vw, 44px)", "font-weight": "700",
            "letter-spacing": "-.025em", "line-height": "1.05",
            "text-shadow": "0 2px 18px rgba(0,0,0,.45)",
        }
        style.append(HERO_STYLE)
    elif banner:
        c["features"].remove("full_card_actions")
        fade = "linear-gradient(to bottom, #000 45%, rgba(0,0,0,.55) 75%, transparent 100%)"
        c["styles"]["card"].update({
            "mask-image": fade, "-webkit-mask-image": fade,
            "border": "none", "box-shadow": "none", "background-color": "transparent",
        })
        c["styles"]["title"] = {"display": "none"}  # the header already shows the room name
        # taller photo on phones, where 7:2 is just a strip
        style.append("@media (max-width: 767px) { ha-card { --user-grid-aspect-ratio: 4/3 !important; aspect-ratio: 4/3 !important; } }")
    if room.get("haunt") and not hero:
        style.append(HAUNT_STYLE % (json.dumps(room["haunt"]), area_id))
    if style:
        c["card_mod"] = {"style": "\n".join(style)}
    c.update(grid(cols, rows))
    return c


# --------------------------------------------------------------------------------------
# Styling (card-mod) and chrome (navbar-card + kiosk-mode)
# --------------------------------------------------------------------------------------
# Frosted-glass look derived from theme variables, so it works in light and dark.
# The rise-in and hover lift match the theme's card-mod-card, so every card moves alike.
EASE = "cubic-bezier(.2, .8, .2, 1)"
GLASS = """
ha-card {
  background: color-mix(in srgb, var(--card-background-color) var(--lumen-glass, 72%), transparent) !important;
  backdrop-filter: blur(18px) saturate(140%);
  -webkit-backdrop-filter: blur(18px) saturate(140%);
  border: 1px solid var(--lumen-card-border, color-mix(in srgb, var(--primary-text-color) 9%, transparent)) !important;
  border-radius: 28px !important;
  box-shadow: var(--lumen-card-shadow, 0 10px 30px -12px rgba(0, 0, 0, .35)) !important;
  transition: translate .35s EASE, box-shadow .35s EASE, border-color .35s EASE;
}
@media (hover: hover) {
  ha-card:hover {
    translate: 0 -2px;
    border-color: var(--lumen-card-border-hover, color-mix(in srgb, var(--primary-text-color) 16%, transparent)) !important;
    box-shadow: var(--lumen-card-shadow-hover, 0 22px 44px -18px rgba(0, 0, 0, .5)) !important;
  }
}
@media (prefers-reduced-motion: reduce) { ha-card, ha-card:hover { transition: none; translate: none; } }
""".replace("EASE", EASE)

# Room photo cards: lift on hover while the photo slowly zooms, press in on tap.
PHOTO_BASE = "#15171c"
ROOM_HOVER_STYLE = """
ha-card { transition: translate .4s EASE, scale .2s EASE, box-shadow .4s EASE !important; }
/* The card's own inset is a percentage, which puts the name almost on the edge of small cards. */
.info { margin: 18px 0 0 20px !important; width: calc(100% - 20px) !important; }
/* The toggles grow with the card's height; cap them so big cards don't get balloon-sized icons. */
entity-collection {
  aspect-ratio: auto !important; width: 62px !important; height: 100% !important;
  grid-template-rows: repeat(var(--user-entities-wrap, 4), minmax(0, 52px)) !important;
  align-content: start; padding: 12px 12px 12px 0 !important;
}
.problems { margin: 0 0 16px 18px !important; }
room-background-image { position: absolute; inset: 0; transition: scale .9s EASE; }
@media (hover: hover) {
  ha-card:hover { translate: 0 -3px; box-shadow: 0 26px 48px -20px rgba(0, 0, 0, .6) !important; }
  ha-card:hover room-background-image { scale: 1.06; }
}
ha-card:active { scale: .985; }
@media (prefers-reduced-motion: reduce) {
  ha-card, room-background-image { transition: none !important; }
  ha-card:hover, ha-card:active, ha-card:hover room-background-image { translate: none; scale: none; }
}
""".replace("EASE", EASE)

# Room page hero: the photo is the page background, so the card itself is just the room name
# and toggles floating over it. No glass, no photo layer, no lift.
HERO_STYLE = """
ha-card { backdrop-filter: none !important; -webkit-backdrop-filter: none !important; }
ha-card:hover { translate: none !important; }
room-background-image { display: none; }
.sensors, .stats { color: rgba(255, 255, 255, .88) !important; text-shadow: 0 1px 10px rgba(0, 0, 0, .5); }
@media (max-width: 767px) { ha-card { --user-grid-aspect-ratio: 5/4 !important; aspect-ratio: 5/4 !important; } }
"""
GLASS_TYPES = {"tile", "weather-forecast", "custom:mini-graph-card", "markdown", "custom:mushroom-template-card"}
TITLE_STYLE = """
ha-card { backdrop-filter: none !important; -webkit-backdrop-filter: none !important; }
.title { font-size: 30px !important; font-weight: 700 !important; letter-spacing: -.02em; }
.subtitle { font-size: 15px !important; opacity: .7; }
"""
HEADING_STYLE = """
ha-card {
  --ha-heading-card-title-font-size: 17px; --ha-heading-card-title-font-weight: 650;
  backdrop-filter: none !important; -webkit-backdrop-filter: none !important;
}
"""
INTER_FONT = "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"

NAVBAR_STYLES = """
.navbar {
  --navbar-primary-color: var(--primary-color);
  --navbar-border-radius: 9999px;
}
.navbar-card.mobile.floating {
  margin-bottom: calc(max(env(safe-area-inset-bottom, 0px), 14px) + 6px) !important;
}

/* Motion: the selected pill slides over from the previous tab (--lumen-nav-shift is set by
   a location listener, see NAV_MOTION_JS) and settles with a spring. Only transform and
   opacity animate, so it stays on the compositor. */
.route { overflow: visible !important; container-type: inline-size; }
.button {
  isolation: isolate; border-radius: 9999px !important;
  transition: transform .18s cubic-bezier(.3, .7, .4, 1), background-color .25s ease;
}
.button .icon, .button .image { transition: translate .3s cubic-bezier(.22, 1.3, .36, 1); }
@media (hover: hover) {
  .navbar-card .button:not(.active):hover { background: color-mix(in srgb, var(--primary-text-color) 7%, transparent); }
  .navbar-card .button:not(.active):hover .icon, .navbar-card .button:not(.active):hover .image { translate: 0 -2px; }
  .popup-item:hover .button { transform: scale(1.06); }
}
.button:active { transform: scale(.92); }
.button.active { background: transparent !important; }
.button.active::before {
  content: ""; position: absolute; inset: 0; z-index: -1;
  border-radius: inherit;
  background: color-mix(in srgb, var(--navbar-primary-color) 26%, transparent);
  will-change: transform;
  animation: lumen-pill .55s cubic-bezier(.22, 1.2, .36, 1) both;
}
.button.active .icon, .button.active .image {
  animation: lumen-pop .45s cubic-bezier(.22, 1.4, .36, 1) both;
}
@keyframes lumen-pill {
  from { transform: translateX(calc(var(--lumen-nav-shift, 0) * 100cqw)) scale(.9); }
  to   { transform: none; }
}
@keyframes lumen-pop {
  from { transform: scale(.8); opacity: .6; }
  to   { transform: none; opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
  .button.active::before, .button.active .icon, .button.active .image { animation: none; }
  .button, .button .icon, .button .image { transition: none; }
}
.navbar-card {
  background: color-mix(in srgb, var(--card-background-color) 70%, transparent) !important;
  backdrop-filter: blur(22px) saturate(160%);
  -webkit-backdrop-filter: blur(22px) saturate(160%);
  border: 1px solid var(--lumen-card-border, color-mix(in srgb, var(--primary-text-color) 9%, transparent)) !important;
  box-shadow: var(--lumen-bar-shadow, 0 18px 40px -16px rgba(0, 0, 0, .45)) !important;
}
"""

# kiosk-mode sets --kiosk-header-height: 0px while it hides HA's toolbar. Without it (sidebar
# shown, ?disable_km, kiosk: false) HA's toolbar is back, so this header steps aside: hidden
# where style queries work, parked below HA's toolbar elsewhere.
HEADER_STYLES = """
:host { display: block; height: 64px; }            /* reserve room at the top of the page */
@container not style(--kiosk-header-height: 0px) {
  :host { height: 0; }
  .navbar { display: none !important; }
}
.navbar, .navbar.desktop.top {
  top: calc(var(--kiosk-header-height, var(--header-height, 56px)) + env(safe-area-inset-top, 0px) + 10px) !important;
  bottom: unset !important;
  left: 50% !important; right: unset !important;
  transform: translate(-50%, 0) !important;
  width: min(760px, calc(100vw - 24px)) !important;
}
.navbar-card, .navbar-card.mobile.floating, .navbar-card.desktop {
  width: 100% !important; margin: 0 !important;
  justify-content: flex-end !important; gap: 4px !important;
  padding: 6px 8px 6px 24px !important;
  border-radius: 9999px !important;
  background: color-mix(in srgb, var(--card-background-color) 70%, transparent) !important;
  backdrop-filter: blur(22px) saturate(160%);
  -webkit-backdrop-filter: blur(22px) saturate(160%);
  border: 1px solid var(--lumen-card-border, color-mix(in srgb, var(--primary-text-color) 9%, transparent)) !important;
  box-shadow: var(--lumen-bar-shadow, 0 18px 40px -16px rgba(0, 0, 0, .45)) !important;
}
.navbar-card::before {
  content: var(--page-title);
  order: 1; margin-right: auto;
  font-size: 18px; font-weight: 650; letter-spacing: -.01em;
  color: var(--primary-text-color);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.route { width: auto !important; order: 2; }
.route.back { order: 0; margin-left: -10px; }
.route .button { padding: 6px 10px !important; }
"""


SPACING = {
    "--ha-section-grid-row-gap": "12px", "--ha-section-grid-column-gap": "12px",
    "--ha-view-sections-row-gap": "40px", "--ha-view-sections-column-gap": "36px",
    "--ha-view-sections-narrow-column-gap": "14px",
}


def full_load(icon, label, url):
    """Popup item that does a real page load. kiosk-mode only re-evaluates on load, so an
    in-app navigation would keep the sidebar hidden on Settings/Profile."""
    return {"icon": icon, "label": label,
            "tap_action": {"action": "custom-js-action",
                           "code": f"[[[ window.location.assign({json.dumps(url)}); ]]]"}}


NAV_MOTION_JS = """
if (!window.__lumenNav) {
  window.__lumenNav = true;
  const tabs = %s;
  const idx = () => tabs[location.pathname] ?? -1;
  let prev = idx();
  const update = () => {
    const cur = idx();
    if (cur < 0) return;
    document.documentElement.style.setProperty('--lumen-nav-shift', prev < 0 ? 0 : prev - cur);
    prev = cur;
  };
  window.addEventListener('location-changed', update);
  window.addEventListener('popstate', update);
}
"""


# Page-wide entrance, run from the navbar (it's on every view). When a view is shown, it's held
# invisible until its layout settles, then every card on screen rises in at once, delayed by where
# it sits (top to bottom, a touch left to right), so the whole page moves as one. Doing this in
# CSS per card doesn't line up: HA paints cards before card-mod styles them, and card-mod styles
# each card at a slightly different moment. The bars only fade: moving them would break their
# fixed positioning. Also marks room pages so wide screens fit their photo to the hero band.
ENTER_JS = """
if (!window.__lumenEnter?.isConnected) {
  const walk = (node, out) => {
    for (const el of node.children) {
      if (el.localName === 'hui-card') { out.push(el); continue; }
      if (el.shadowRoot) walk(el.shadowRoot, out);
      walk(el, out);
    }
    return out;
  };
  const find = (node, name) => {
    for (const el of node.children) {
      if (el.localName === name) return el;
      const hit = (el.shadowRoot && find(el.shadowRoot, name)) || find(el, name);
      if (hit) return hit;
    }
  };
  const container = find(document, 'hui-view-container');
  if (container) {
    window.__lumenEnter = container;
    document.documentElement.style.setProperty('--lumen-rise', 'none');
    // More air than HA's defaults, scoped to this dashboard's views.
    for (const [k, v] of Object.entries(%s)) container.style.setProperty(k, v);
    const heroes = %s;
    const reduce = matchMedia('(prefers-reduced-motion: reduce)');
    const style = document.createElement('style');
    style.textContent = '@media (min-width: 768px) { hui-view-background[lumen-hero] { height: 60vh; bottom: auto; } }';
    container.getRootNode().appendChild(style);
    let run = 0;
    const enter = (view) => {
      const id = ++run;
      if (!view || reduce.matches || container.classList.contains('has-tab-bar')) return;
      view.style.opacity = '0';
      let last = '', same = 0;
      const t0 = performance.now();
      const tick = () => {
        if (id !== run) return;
        const cards = walk(view, []).filter(c => c.getBoundingClientRect().width);
        const sig = cards.map(c => { const r = c.getBoundingClientRect(); return [r.x | 0, r.y | 0, r.width | 0, r.height | 0]; }).join(';');
        same = sig && sig === last ? same + 1 : 0;
        last = sig;
        if (same < 2 && performance.now() - t0 < 600) return requestAnimationFrame(tick);
        view.style.opacity = '';
        for (const c of cards) {
          const r = c.getBoundingClientRect();
          if (r.top > innerHeight || r.bottom < 0) continue;
          const bar = c.querySelector('navbar-card');
          c.animate(bar ? [{ opacity: 0 }, { opacity: 1 }]
                        : [{ opacity: 0, translate: '0 8px' }, { opacity: 1, translate: '0 0' }],
                    { duration: 360, easing: 'cubic-bezier(.2, .8, .2, 1)', fill: 'backwards',
                      delay: bar ? 0 : Math.min(260, Math.max(0, r.top) * .25 + r.left * .03) });
        }
      };
      requestAnimationFrame(tick);
    };
    const mark = () => container.querySelector('hui-view-background')
      ?.toggleAttribute('lumen-hero', heroes.includes(location.pathname));
    new MutationObserver(changes => {
      for (const m of changes) for (const n of m.addedNodes) if (n.localName === 'hui-view') { mark(); enter(n); }
    }).observe(container, { childList: true });
    mark();
    enter(container.querySelector('hui-view'));
  }
}
"""


# Embedded Home Assistant apps (an iframe card on /api/hassio_ingress/...) only load once the browser holds an
# `ingress_session` cookie, which the frontend sets when an app panel is opened. This does the same call, at
# most every 20 minutes, from the navbar's JS so the Birds page works however you arrive at it.
INGRESS_JS = """
(function () {
  const key = '__lumenIngress', now = Date.now();
  if (window[key] && now - window[key] < 20 * 60 * 1000) return;
  window[key] = now;
  hass.callWS({type: 'supervisor/api', endpoint: '/ingress/session', method: 'post'}).then((r) => {
    document.cookie = 'ingress_session=' + r.session + ';path=/api/hassio_ingress/;SameSite=Strict'
      + (location.protocol === 'https:' ? ';Secure' : '');
  }).catch(() => { window[key] = 0; });
})();
"""



def navbar():
    def selected(paths):
        return "[[[ return " + json.dumps(paths) + ".includes(window.location.pathname) ]]]"

    # Tab index per page (rooms map to their floor's tab), used by the sliding indicator. Floors with
    # `nav: false` move to the More menu; `nav_tabs` adds tabs for other pages (e.g. Outside).
    home_pages = [f"/{URL_PATH}/{p}" for p in ("home", "energy", "vacuum", "media")]
    tabs = {p: 0 for p in home_pages}
    shown = [f for f in FLOORS if f.get("nav", True)]
    hidden = [f for f in FLOORS if not f.get("nav", True)]
    extra = CFG.get("nav_tabs") or []
    for i, f in enumerate(shown, start=1):
        tabs[f"/{URL_PATH}/{f['path']}"] = i
        tabs.update({f"/{URL_PATH}/{r['path']}": i for r in ROOMS.values() if r["floor"] == f["id"]})
    for i, t in enumerate(extra, start=len(shown) + 1):
        tabs.update({f"/{URL_PATH}/{p}": i for p in [t["path"]] + t.get("also", [])})
    if not any(birds_path() in [f"/{URL_PATH}/{p}" for p in [t["path"]] + t.get("also", [])] for t in extra):
        tabs[birds_path()] = 0
        home_pages.append(birds_path())
    haunted = [a for a, r in ROOMS.items() if r.get("haunt")]
    haunt_js = HAUNT_JS % (json.dumps(haunted), float(CFG["haunted"].get("every", 100)),
                           float(CFG["haunted"].get("duration", 1))) if haunted else ""
    heroes = [f"/{URL_PATH}/{r['path']}" for r in ROOMS.values() if r.get("picture")]
    home_selected = ("[[[ " + NAV_MOTION_JS % json.dumps(tabs) + ENTER_JS % (json.dumps(SPACING), json.dumps(heroes)) + haunt_js +
                     (INGRESS_JS if CFG.get("birdnet", {}).get("saezuri_app") else "") +
                     f"return {json.dumps(home_pages)}.includes(window.location.pathname); ]]]")
    routes = [{"url": f"/{URL_PATH}/home", "icon": "mdi:home-outline", "icon_selected": "mdi:home",
               "label": "Home", "selected": home_selected}]
    for f in shown:
        rooms = [r["path"] for r in ROOMS.values() if r["floor"] == f["id"]]
        routes.append({
            "url": f"/{URL_PATH}/{f['path']}", "icon": f["icon"], "label": f.get("short", f["name"]),
            "selected": selected([f"/{URL_PATH}/{p}" for p in [f["path"]] + rooms]),
        })
    for t in extra:
        routes.append({"url": f"/{URL_PATH}/{t['path']}", "icon": t.get("icon", "mdi:compass-outline"),
                       "label": t.get("label", t["path"].title()),
                       "selected": selected([f"/{URL_PATH}/{p}" for p in [t["path"]] + t.get("also", [])])})
    more_floors = [{"icon": f["icon"], "label": f["name"], "url": f"/{URL_PATH}/{f['path']}"} for f in hidden]
    routes.append({
        "icon": "mdi:dots-horizontal", "label": "More", "tap_action": {"action": "open-popup"},
        "popup": [
            *more_floors,
            {"icon": "mdi:lightning-bolt-outline", "label": "Energy", "url": energy_path() if CFG.get("energy", {}).get("insight") else "/energy"},
            full_load("mdi:pencil-outline", "Edit dashboard", f"/{URL_PATH}/home?disable_km&edit=1"),
            {"icon": "mdi:chart-box-outline", "label": "History", "url": "/history"},
            {"icon": "mdi:map-outline", "label": "Map", "url": "/map"},
            full_load("mdi:cog-outline", "Settings", "/config/dashboard"),
            full_load("mdi:account-circle-outline", "Profile & theme", "/profile/general"),
            full_load("mdi:dock-left", "Show sidebar", f"/{URL_PATH}/home?disable_km"),
        ],
    })
    return {
        "type": "custom:navbar-card",
        "routes": routes,
        "desktop": {"position": "bottom", "mode": "floating", "show_labels": True,
                    "show_popup_label_backgrounds": True},
        "mobile": {"mode": "floating", "show_labels": False, "show_popup_label_backgrounds": True},
        "layout": {"reflect_child_state": True,
                   "auto_padding": {"enabled": True, "mobile_px": 110, "desktop_px": 110}},
        "haptic": {"url": True, "tap_action": True},
        "styles": NAVBAR_STYLES,
    }


def header(title, back=None):
    """Floating top bar: page name, plus search / assist / notifications (and back on room pages)."""
    routes = []
    if back:
        routes.append({"icon": "mdi:chevron-left", "label": "Back", "tap_action": nav(back),
                       "selected": False})
    routes += [
        {"icon": "mdi:magnify", "label": "Search", "tap_action": {"action": "quickbar", "mode": "entities"},
         "selected": False},
        {"icon": "mdi:microphone-outline", "label": "Assist", "tap_action": {"action": "assist"},
         "selected": False},
        {"icon": "mdi:bell-outline", "label": "Notifications",
         "tap_action": {"action": "show-notifications"}, "selected": False,
         "badge": {"show": "[[[ return Object.keys(hass.states).some(e => e.startsWith('persistent_notification.')) ]]]",
                   "color": "var(--accent-color)"}},
    ]
    # The theme's card-mod element is the card's first child, hence first-of-type.
    styles = HEADER_STYLES.replace(".route.back", "div.route:first-of-type" if back else ".route.back-none")
    styles = f":host {{ --page-title: {json.dumps(title)}; }}\n" + styles
    return {
        "type": "custom:navbar-card",
        "routes": routes,
        "desktop": {"position": "top", "mode": "floating", "show_labels": False, "min_width": 768},
        "mobile": {"mode": "floating", "show_labels": False},
        "layout": {"auto_padding": {"enabled": False}},
        "haptic": {"tap_action": True},
        "styles": styles,
        **grid(36),
    }


def apply_style(node):
    """Walk the config and attach card-mod styles to every card that supports them."""
    if isinstance(node, list):
        for n in node:
            apply_style(n)
    elif isinstance(node, dict):
        t = node.get("type") if isinstance(node.get("type"), str) else None
        if node.pop("_style", None) == "plain":
            pass  # carries its own card-mod style
        elif t in GLASS_TYPES:
            node["card_mod"] = {"style": GLASS}
        elif t == "custom:mushroom-title-card":
            node["card_mod"] = {"style": TITLE_STYLE}
        elif t == "heading":
            node["card_mod"] = {"style": HEADING_STYLE}
        for k, v in node.items():
            if k != "card_mod":
                apply_style(v)


# --------------------------------------------------------------------------------------
# Haunted rooms: now and then, hair creeps out of a corner of a room photo
# --------------------------------------------------------------------------------------
HAUNT_PROMPT = (
    "Edit this photo of a room. Long, wet, jet-black human hair pours out of one of the upper corners, "
    "where the walls meet the ceiling, and hangs down in thick tangled strands, like the ghost in the "
    "film The Grudge. Keep everything else exactly as it is: same camera angle, framing, furniture and "
    "lighting. Photorealistic. Don't add people or text.")
HAUNT_PROMPT_NO_PHOTO = (
    "Photo of a dim, empty {name}, looking up into the corner where the walls meet the ceiling. Long, "
    "wet, jet-black human hair pours out of the corner and hangs down in thick tangled strands, like "
    "the ghost in the film The Grudge. Photorealistic, wide angle. Don't add people or text.")
HAUNT_CACHE = "haunted-cache.json"

# Sits between the room photo and the card content. Hidden until HAUNT_JS sets
# --lumen-haunt-<area>. It rebuilds the photo layer exactly (the card's darkening gradient
# and brightness filter, then the photo base showing through at 1 - background.opacity)
# and is fully opaque, so the only thing that changes when it shows is the hair.
HAUNT_STYLE = """
room-background-image::after {
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background:
    linear-gradient(color-mix(in srgb, var(--lumen-photo-base) calc((1 - var(--user-opacity, .7)) * 100%%), transparent) 0 0),
    var(--user-background-image-overlay, var(--default-overlay)),
    center / cover no-repeat url(%s);
  filter: var(--background-filter, none);
  opacity: var(--lumen-haunt-%s, 0);
  transition: opacity .12s;
}
"""

# Custom properties inherit through shadow roots, so one flag on <html> reaches every card
# of that room. Each room waits a random 50-150% of `every` seconds between appearances.
HAUNT_JS = """
if (!window.__lumenHaunt) {
  window.__lumenHaunt = true;
  const rooms = %s, every = %s, show = %s, root = document.documentElement.style;
  const haunt = (room) => setTimeout(() => {
    root.setProperty('--lumen-haunt-' + room, '1');
    setTimeout(() => { root.removeProperty('--lumen-haunt-' + room); haunt(room); }, show * 1000);
  }, every * (0.5 + Math.random()) * 1000);
  rooms.forEach(haunt);
}
"""


def http(url, data=None, headers=None):
    if not url.startswith("http"):
        url = HA["url"] + url
    with urllib.request.urlopen(urllib.request.Request(url, data, headers or {}), timeout=300) as r:
        return r.read(), r.headers.get_content_type()


def multipart(fields, files):
    """Encode form fields and {name: (filename, content_type, bytes)} files."""
    boundary = uuid.uuid4().hex
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                    for k, v in fields.items())
    for k, (filename, ctype, blob) in files.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{filename}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n").encode() + blob + b"\r\n"
    return body + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def haunted_photo(room, picture, hc):
    """Ask the image API for a haunted take on the room photo, or on an imagined room without one."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        if not hc.get("api_key_file"):
            sys.exit("haunted: set OPENAI_API_KEY or haunted.api_key_file")
        with open(os.path.expanduser(hc["api_key_file"])) as fh:
            key = fh.read().strip()
    api = hc.get("api_url", "https://api.openai.com/v1").rstrip("/")
    # gpt-image-2 keeps the photo's framing and aspect ratio, so the overlay lines up exactly.
    model = hc.get("model", "gpt-image-2")
    quality = hc.get("quality", "high")
    if picture:
        # HA area pictures are served as thumbnails; the model gets more detail from the original.
        picture = re.sub(r"^(/api/image/serve/[^/]+)/\d+x\d+$", r"\1/original", picture)
        # Only send the HA token to HA itself, not to an external picture URL.
        auth = {} if picture.startswith("http") else {"Authorization": f"Bearer {HA['token']}"}
        photo, ctype = http(picture, headers=auth)
        prompt = hc.get("prompt", HAUNT_PROMPT).replace("{name}", room["name"])
        body, form = multipart({"model": model, "prompt": prompt, "quality": quality},
                               {"image": ("room." + ctype.split("/")[-1], ctype, photo)})
        reply, _ = http(f"{api}/images/edits", body, {"Authorization": f"Bearer {key}", "Content-Type": form})
    else:
        prompt = hc.get("prompt", HAUNT_PROMPT_NO_PHOTO).replace("{name}", room["name"].lower())
        body = json.dumps({"model": model, "prompt": prompt, "quality": quality, "size": "1536x1024"}).encode()
        reply, _ = http(f"{api}/images/generations", body,
                        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    return base64.b64decode(json.loads(reply)["data"][0]["b64_json"])


def haunt(call, areas, dry_run):
    """Give each haunted room a generated photo, uploaded to HA's image store. Results are cached
    in haunted-cache.json and only regenerated when the room photo or prompt changes (or --rehaunt)."""
    hc = CFG.get("haunted")
    if not hc:
        return
    pictures = {a["area_id"]: a.get("picture") for a in areas}
    cache = {}
    if os.path.exists(HAUNT_CACHE):
        with open(HAUNT_CACHE) as fh:
            cache = json.load(fh)
    try:
        stored = {i["id"] for i in call("image/list")}
    except RuntimeError:
        stored = None
    for area_id in hc.get("rooms") or list(ROOMS):
        room = ROOMS.get(area_id)
        if not room:
            print(f"WARNING: haunted room {area_id!r} is not in rooms")
            continue
        picture = pictures.get(area_id)
        fingerprint = hashlib.sha256(json.dumps(
            [picture, hc.get("prompt"), hc.get("model"), hc.get("quality"), room["name"]]).encode()).hexdigest()[:16]
        hit = cache.get(area_id)
        if (hit and hit["fingerprint"] == fingerprint and "--rehaunt" not in sys.argv
                and (stored is None or hit["id"] in stored)):
            room["haunt"] = hit["url"]
            continue
        if dry_run:
            print(f"haunted: no image for {room['name']} yet; run without --dry-run to generate it")
            continue
        print(f"haunted: generating {room['name']}...")
        try:
            blob = haunted_photo(room, picture, hc)
            body, form = multipart({}, {"file": (f"haunted-{area_id}.png", "image/png", blob)})
            reply, _ = http("/api/image/upload", body,
                            {"Authorization": f"Bearer {HA['token']}", "Content-Type": form})
        except urllib.error.HTTPError as e:
            print(f"WARNING: haunting {room['name']} failed: {e} {e.read().decode(errors='replace')[:300]}")
            continue
        except (OSError, KeyError, ValueError) as e:
            print(f"WARNING: haunting {room['name']} failed: {e}")
            continue
        if hit and stored and hit["id"] in stored:
            try:
                call("image/delete", image_id=hit["id"])
            except RuntimeError:
                pass
        image_id = json.loads(reply)["id"]
        cache[area_id] = {"fingerprint": fingerprint, "id": image_id,
                          "url": f"/api/image/serve/{image_id}/original"}
        room["haunt"] = cache[area_id]["url"]
        with open(HAUNT_CACHE, "w") as fh:
            json.dump(cache, fh, indent=1)


# --------------------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------------------
def fill_row(k, width):
    """Widths (multiples of 6 columns) for k cards sharing a row of `width` columns."""
    base = max(6, width // k // 6 * 6)
    widths = [base] * k
    i = k - 1
    while sum(widths) < width:
        widths[i] += 6
        i = i - 1 if i > 0 else k - 1
    return widths


def bento(n):
    """(columns, rows) per room for a floor on the home page: the first room large, the rest in a tidy
    mosaic beside and below it, widened where needed so every row ends flush."""
    if n == 0:
        return []
    if n == 1:
        return [(36, 3)]
    rest = n - 1
    if rest <= 4:
        return [(12, 3)] + [(w, 3) for w in fill_row(rest, 24)]
    beside = rest if rest <= 8 else 8
    top = [(w, 3) for w in fill_row(4, 24)] + [(w, 3) for w in fill_row(beside - 4, 24)]
    below = []
    left = rest - beside
    while left:
        k = min(6, left)
        below += [(w, 3) for w in fill_row(k, 36)]
        left -= k
    return [(12, 6)] + top + below


def home_view(areas_by_floor):
    user = CFG.get("user_name")
    greeting = ("{% set h = now().hour %}"
                "{{ 'Good night' if h < 6 else 'Good morning' if h < 12 else "
                "'Good afternoon' if h < 18 else 'Good evening' }}" + (f", {user}" if user else ""))
    welcome = [{
        "type": "custom:mushroom-title-card",
        "title": greeting,
        "subtitle": ("{{ now().strftime('%A %-d %B') }} · " + plural_lights(all_lights()) +
                     (f"{{{{ ' · 🌙 Night mode' if is_state('{CFG['night_mode']}', 'on') else '' }}}}"
                      if CFG.get("night_mode") else "") +
                     (f"{{{{ ' · 🛡️ Alarm on' if is_state('{CFG['alarm_mode']}', 'on') else '' }}}}"
                      if CFG.get("alarm_mode") else "")),
        **grid(12),
    }]

    chips = []
    for p in ents(CFG.get("people")):
        chips.append({"type": "template", "entity": p["entity"], "content": p.get("name", ""),
                      "icon": p.get("icon", "mdi:account"),
                      "icon_color": f"{{{{ 'green' if is_state('{p['entity']}','home') else 'disabled' }}}}",
                      "tap_action": {"action": "more-info"}})
    power = CFG.get("power")
    if power:
        factor = 1000 if power.get("unit", "W") == "kW" else 1
        chips.append({"type": "template", "icon": "mdi:flash", "icon_color": "amber", "entity": power["entity"],
                      "content": f"{{{{ (states('{power['entity']}')|float(0)*{factor})|round(0)|int }}}} W",
                      "tap_action": {"action": "more-info"}})
    waste = CFG.get("waste")
    if waste:
        days, kind = waste["days_until"], waste["next_type"]
        chips.append({"type": "template", "icon": "mdi:recycle", "entity": waste.get("next_date", kind),
                      "icon_color": f"{{{{ 'red' if states('{days}')|int(9) <= 1 else 'green' }}}}",
                      "content": (f"{{% set d = states('{days}')|int(-1) %}}"
                                  f"{{{{ states('{kind}')|upper }}}} "
                                  "{{ 'today' if d == 0 else 'tomorrow' if d == 1 else 'in ' ~ d ~ 'd' }}"),
                      "tap_action": {"action": "more-info"}})
    alarm = CFG.get("alarm_mode")
    if alarm:
        on = f"is_state('{alarm}', 'on')"
        chips.append({"type": "template", "entity": alarm,
                      "icon": f"{{{{ 'mdi:shield-home' if {on} else 'mdi:shield-off-outline' }}}}",
                      "icon_color": f"{{{{ 'red' if {on} else 'disabled' }}}}",
                      "content": f"{{{{ 'Alarm on' if {on} else 'Alarm' }}}}",
                      "tap_action": {"action": "toggle", "confirmation": {"text": "Turn Alarm mode on or off?"}},
                      "hold_action": {"action": "more-info"}})
    if chips:
        welcome.append({"type": "custom:mushroom-chips-card", "chips": chips, **grid(12)})
    if CFG.get("weather"):
        welcome.append({"type": "weather-forecast", "entity": CFG["weather"], "forecast_type": "daily",
                        "show_current": True, "show_forecast": True, **grid(12)})
    sections = [section(welcome)]

    quick = []
    for q in ents(CFG.get("quick_settings")):
        kw = dict(cols=4, vertical=True, name=q.get("name"), icon=q.get("icon"), color=q.get("color"))
        action = q.get("action")
        if action == "scene":
            kw.update(tap=scene_tap(q["entity"]), hide_state=True)
        elif action == "script":
            kw.update(hide_state=True, tap={"action": "perform-action", "perform_action": "script.turn_on",
                                            "target": {"entity_id": q["entity"]},
                                            **({"confirmation": {"text": q["confirm"]}} if q.get("confirm") else {})})
        elif action == "todo":
            kw["tap"] = nav(f"/todo?entity_id={q['entity']}")
        elif isinstance(action, dict):
            kw["tap"] = action
        quick.append(tile(q["entity"], **kw))
    if quick:
        sections.append(section([heading("Quick settings", "mdi:tune-variant")] + quick))

    energy = CFG.get("energy")
    if energy:
        sections.append(section(energy_cards(energy)))

    media = CFG.get("media")
    if media:
        playing = section(now_playing(media), span=3)
        playing["visibility"] = media_on(media)
        sections.append(playing)

    if CFG.get("birdnet"):
        sections.append(section(birdnet_cards(CFG["birdnet"]), span=3))

    for f in FLOORS:
        rooms = [a for a in areas_by_floor[f["id"]] if a in ROOMS]
        cards = [heading(f["name"], f["icon"], tap=f"/{URL_PATH}/{f['path']}")]
        cards += [room_card(a, ROOMS[a], cols=c, rows=h) for a, (c, h) in zip(rooms, bento(len(rooms)))]
        sections.append(section(cards, span=3))

    return {"title": "Home", "path": "home", "icon": "mdi:home-heart", "type": "sections",
            "max_columns": 3, "dense_section_placement": True, "sections": sections}


def birds_path():
    return f"/{URL_PATH}/birds"


def bird_card(b, tap=None, **extra):
    """The last bird heard, as a mushroom card: photo, name, confidence, how long ago."""
    last, photo = b["last_bird"], b.get("photo")
    none = f"states('{last}') in ('unknown', 'unavailable', 'none')"
    card = {
        "type": "custom:mushroom-template-card", "entity": last, "layout": "horizontal",
        "icon": "mdi:bird", "icon_color": "teal",
        "primary": f"{{{{ 'No birds heard yet' if {none} else states('{last}') }}}}",
        "secondary": (f"{{% set c = state_attr('{last}', 'confidence') %}}"
                      f"{{% set t = state_attr('{last}', 'timestamp') %}}"
                      "{% if c and t %}{{ (c * 100) | round(0) | int }}% · {{ relative_time(as_datetime(t)) }} ago"
                      "{% else %}Listening…{% endif %}"),
        "tap_action": tap or {"action": "none"},
        **extra,
    }
    if photo:
        card["picture"] = f"{{{{ state_attr('{photo}', 'entity_picture') }}}}"
    return card


# A 1x1 transparent GIF: an `image` element of this size is an invisible, tappable layer over a picture.
CLEAR_PIXEL = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEAAAAALAAAAAABAAEAAAIBRAA7"


CAMERA_STYLE = """
ha-card {
  position: relative; border-radius: 28px !important; overflow: hidden; background: #15171c !important;
  border: 1px solid color-mix(in srgb, var(--primary-text-color) 9%, transparent) !important;
  box-shadow: 0 18px 40px -18px rgba(0, 0, 0, .55) !important;
  transition: translate .4s EASE, box-shadow .4s EASE, border-color .4s EASE, scale .2s EASE;
}
hui-image { transition: scale .9s EASE; }
@media (hover: hover) {
  ha-card:hover { translate: 0 -3px; box-shadow: 0 26px 48px -20px rgba(0, 0, 0, .6) !important; }
  ha-card:hover hui-image { scale: 1.03; }
  ha-card:hover::before { opacity: 1; translate: 0 0; }
}
ha-card:active { scale: .99; }
/* The camera name over a soft top and bottom shade. */
ha-card::after {
  content: @NAME@; position: absolute; inset: 0; z-index: 1; pointer-events: none;
  padding: 18px 22px; box-sizing: border-box;
  color: #fff; font-size: 20px; font-weight: 650; letter-spacing: -.01em; line-height: 1.15;
  text-shadow: 0 1px 10px rgba(0, 0, 0, .6);
  background: linear-gradient(to bottom, rgba(0, 0, 0, .55), transparent 34%),
              linear-gradient(to top, rgba(0, 0, 0, .35), transparent 28%);
}
/* State pill, top right: a pulsing red dot while live, a quiet one on a snapshot. */
hui-image::after {
  content: @PILL@; position: absolute; z-index: 2; top: 18px; right: 18px;
  padding: 5px 11px 5px 24px; border-radius: 999px; pointer-events: none;
  font-size: 12px; font-weight: 650; letter-spacing: .04em; text-transform: uppercase; color: #fff;
  background: radial-gradient(circle at 13px 50%, @DOT@ 3.5px, transparent 4.5px), rgba(0, 0, 0, .38);
  backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); @PULSE@
}
/* On hover, a small "open" hint appears bottom right. */
ha-card::before {
  content: "Open  ⤢"; position: absolute; z-index: 2; right: 18px; bottom: @HINT_BOTTOM@; pointer-events: none;
  padding: 6px 12px; border-radius: 999px; font-size: 12px; font-weight: 600; color: #fff;
  background: rgba(0, 0, 0, .38); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  opacity: 0; translate: 0 6px; transition: opacity .3s EASE, translate .3s EASE;
}
@keyframes lumen-live { 50% { opacity: .72; } }
@media (prefers-reduced-motion: reduce) {
  hui-image::after { animation: none; } ha-card, hui-image, ha-card::before { transition: none; }
  ha-card:hover, ha-card:hover hui-image, ha-card:active { translate: none; scale: none; }
}
""".replace("EASE", EASE)
DETECTED_STYLE = ("ha-card { border-color: #34d399 !important; "
                  "box-shadow: 0 0 0 2px #34d399, 0 18px 44px -16px rgba(52, 211, 153, .45) !important; }")


def camera_card(camera, name, live=True, bird=None, visibility=None, cols=24, tap_entity=None, since=None,
                detect=None):
    """A camera like the Cameras page draws it: its name over a soft shade, a Live pill (or, for a snapshot,
    when it was taken and "Tap for live"), a lift and slow zoom on hover, and a green ring while `detect`
    sensors see someone. Tapping anywhere opens the live view (`tap_entity` for a snapshot). A bird card
    can be pinned to the bottom."""
    more = {"action": "more-info", "entity": tap_entity or camera}
    if live:
        pill, dot, pulse = '"Live"', "#ff5a5f", "animation: lumen-live 2s ease-in-out infinite;"
    else:
        # card-mod renders the template, so the pill can say how old the snapshot is.
        pill = ((f"\"{{% set t = state_attr('{since}', 'last_triggered') %}}"
                 "{{ relative_time(t) ~ ' ago · ' if t else '' }}Tap for live\"") if since else '"Tap for live"')
        dot, pulse = "rgba(255,255,255,.6)", ""
    style = (CAMERA_STYLE.replace("@NAME@", json.dumps(name)).replace("@PILL@", pill).replace("@DOT@", dot)
             .replace("@PULSE@", pulse).replace("@HINT_BOTTOM@", "88px" if bird else "18px"))
    if detect:
        cond = " or ".join(f"is_state('{e}', 'on')" for e in detect)
        style += f"{{% if {cond} %}}\n{DETECTED_STYLE}\n{{% endif %}}\n"
    if not bird:
        # Without an overlay a picture-entity does the job, and it can crop the stream to fill the frame.
        card = {"type": "picture-entity", "entity": camera, "camera_image": camera,
                "camera_view": "live" if live else "auto", "show_name": False, "show_state": False,
                "fit_mode": "cover", "aspect_ratio": "16:9", "tap_action": more, "_style": "plain",
                "card_mod": {"style": style}, **grid(cols)}
        if visibility:
            card["visibility"] = visibility
        return card
    elements = [{"type": "image", "image": CLEAR_PIXEL, "entity": tap_entity or camera, "tap_action": more,
                 "style": {"top": "0", "left": "0", "width": "100%", "height": "100%", "transform": "none"}}]
    if bird:
        # picture-elements positions an element from its centre; pin it to the edges instead.
        elements.append({**bird, "style": {"top": "auto", "left": "14px", "right": "14px", "bottom": "14px",
                                           "transform": "none"}})
    card = {"type": "picture-elements", "camera_image": camera, "camera_view": "live" if live else "auto",
            "aspect_ratio": "16:9", "elements": elements, "_style": "plain", "card_mod": {"style": style},
            **grid(cols)}
    if visibility:
        card["visibility"] = visibility
    return card


def door_cameras(d, cols):
    """The front door: live while the doorbell is wired or active; otherwise the last visitor's snapshot,
    which opens the live view when tapped (the doorbell is a battery camera that only streams on demand)."""
    # Live while any of these hold: wired (always streams), recent activity, or the camera is awake anyway
    # (`asleep` is the battery camera's sleep sensor; off means it's streaming).
    live = [(d.get("wired"), "on"), (d.get("timer"), "active"), (d.get("asleep"), "off")]
    live = [(e, s) for e, s in live if e]
    name, detect = d.get("name", "Front door"), d.get("detect")
    if not (d.get("idle") and live):
        return [camera_card(d["live"], name, cols=cols, detect=detect)]
    live_if = [{"condition": "or", "conditions": [{"condition": "state", "entity": e, "state": s} for e, s in live]}]
    idle_if = [{"condition": "state", "entity": e, "state_not": s} for e, s in live]
    return [camera_card(d["live"], name, visibility=live_if, cols=cols, detect=detect),
            camera_card(d["idle"], name, live=False, visibility=idle_if, cols=cols,
                        tap_entity=d["live"], since=d.get("activity"))]


def bird_tiles(b, cols, tap):
    tiles = []
    if b.get("species_today"):
        tiles.append(tile(b["species_today"], cols, name="Species today", icon="mdi:feather", color="teal", tap=tap))
    if b.get("detections_today"):
        tiles.append(tile(b["detections_today"], cols, name="Detections", icon="mdi:microphone", color="cyan", tap=tap))
    if b.get("top_bird"):
        tiles.append(tile(b["top_bird"], cols, name="Most heard today", icon="mdi:trophy-outline", color="amber", tap=tap))
    return tiles


def bird_summary_tile(b, cols, tap):
    """One quiet tile for the home page: today's species count, linking to the Birds page."""
    if not b.get("species_today"):
        return []
    return [tile(b["species_today"], cols, name="Birds today", icon="mdi:bird", color="teal", tap=tap)]


def top_species_card(b, cols=12):
    """Species heard today with a bar each, from the `top_species` sensor ('Name~count;Name~count')."""
    s = b["top_species"]
    return {"type": "markdown", "title": "Heard today", "content": (
        f"{{% set raw = states('{s}') %}}"
        "{% if raw in ('unknown', 'unavailable', 'none', '') %}Nothing heard yet today."
        "{% else %}{% set rows = raw.split(';') %}{% set top = rows[0].split('~')[1] | int(1) %}"
        "{% for r in rows %}{% set p = r.split('~') %}"
        "**{{ p[0] }}** · {{ p[1] }}  \n{{ '▬' * ([(p[1] | int(0) / top * 12) | round(0, 'ceil') | int, 1] | max) }}\n\n"
        "{% endfor %}{% endif %}"), **grid(cols)}


def recent_birds_card(b, cols=12):
    """The latest detections, from the `recent` sensor's `data` attribute."""
    s = b["recent"]
    return {"type": "markdown", "title": "Recently heard", "content": (
        f"{{% set d = state_attr('{s}', 'data') %}}"
        "{% if not d %}No detections yet."
        "{% else %}| When | Species | |\n|:--|:--|--:|\n"
        "{% for x in d %}| {{ x.time[:5] if x.date == now().strftime('%Y-%m-%d') else x.date[5:] ~ ' ' ~ x.time[:5] }} "
        "| {{ x.commonName }} | {{ (x.confidence * 100) | round(0) | int }}% |\n"
        "{% endfor %}{% endif %}"), **grid(cols)}


def birdnet_cards(b):
    """Home-page hero (a full-width section): the garden camera with the last bird laid over it, the
    front door camera beside it, and today's bird tallies and device tiles underneath."""
    go = nav(birds_path())
    camera, door = b.get("camera"), b.get("door_camera")
    cards = [heading(b.get("title", "Outside"), "mdi:cctv" if camera else "mdi:bird",
                     tap=b.get("camera_page") or birds_path())]
    extra = [tile_from(t, 12) for t in b.get("extra_tiles", [])]
    for t in extra:
        t.pop("grid_options", None)
    if camera and door:
        cards.append(camera_card(camera, b.get("camera_name", "Garden"), bird=bird_card(b, tap=go), cols=18,
                                 detect=b.get("camera_detect")))
        cards += door_cameras(door, 18)
        cards += bird_summary_tile(b, 12, go)
        if extra:
            for t in extra:
                t["grid_options"] = {"columns": 12}
            cards += extra
        return cards
    left = (camera_card(camera, b.get("camera_name", "Garden"), bird=bird_card(b, tap=go), cols=24) if camera
            else bird_card(b, tap=go, **grid(24)))
    side = bird_summary_tile(b, 12, go) + extra
    for t in side:
        t.pop("grid_options", None)
    cards.append(left)
    if side:
        cards.append({"type": "vertical-stack", "cards": side, **grid(12)})
    return cards


BIRD_HERO_STYLE = """
ha-card {
  position: relative; border: none !important; border-radius: 0 !important; box-shadow: none !important;
  background: none !important; height: 100%; box-sizing: border-box; display: flex; align-items: flex-end;
  backdrop-filter: none !important; -webkit-backdrop-filter: none !important; overflow: visible !important;
  --hero-pad: clamp(14px, 2.5vw, 36px); --hero-photo: clamp(104px, 26%, 320px);
}
/* Full bleed like the room pages: the latest bird's photo pinned behind the whole page, softened (the source
   is only 320x240) and fading into the page colour, so the cards below scroll over it. A view background
   can't change with the bird, so the hero card draws it. */
ha-card::before {
  content: ""; position: fixed; z-index: -1; top: 0; left: 0; right: 0; height: clamp(62vh, 78vh, 88vh);
  pointer-events: none; transform: scale(1.25); transform-origin: 50% 0;
  background: radial-gradient(120% 70% at 0% 0%, color-mix(in srgb, var(--primary-color) 24%, transparent), transparent 70%),
              url("{{ state_attr('@PHOTO@', 'entity_picture') }}") center / cover;
  filter: blur(36px) saturate(1.4) brightness(.68);
  -webkit-mask-image: linear-gradient(to bottom, #000 28%, transparent 100%);
  mask-image: linear-gradient(to bottom, #000 28%, transparent 100%);
}
/* The sharp photo, framed. */
ha-card::after {
  content: ""; position: absolute; left: var(--hero-pad); bottom: var(--hero-pad); width: var(--hero-photo);
  aspect-ratio: 4 / 3; border-radius: 22px; pointer-events: none;
  background: url("{{ state_attr('@PHOTO@', 'entity_picture') }}") center / cover;
  box-shadow: 0 16px 40px -10px rgba(0, 0, 0, .65);
}
.container { position: relative; z-index: 1; width: 100%; height: 100%; box-sizing: border-box;
  display: flex; flex-direction: column; justify-content: flex-end; align-items: flex-start;
  padding: 0 16px var(--hero-pad) calc(var(--hero-photo) + var(--hero-pad) * 2); }
"""
# Mushroom's template card is built on Home Assistant's tile components: the text is slotted spans.
BIRD_HERO_TEXT = """
ha-tile-icon { display: none !important; }
.row { height: auto !important; align-items: flex-start !important; justify-content: flex-end !important; }
.content { height: auto !important; align-items: flex-end !important; justify-content: flex-start !important;
  text-align: left !important; }
ha-tile-info { flex: 0 1 auto !important; margin: 0 !important; align-items: flex-start !important; }
span[slot=primary] { color: #fff !important; font-weight: 700 !important; line-height: 1.1 !important;
  font-size: clamp(22px, 3.8vw, 48px) !important; text-shadow: 0 2px 18px rgba(0, 0, 0, .5); }
span[slot=secondary] { color: rgba(255, 255, 255, .85) !important; font-size: clamp(13px, 1.5vw, 17px) !important;
  margin-top: 4px; text-shadow: 0 1px 10px rgba(0, 0, 0, .5); }
"""


def bird_hero(b):
    """Birds page hero: the last bird's photo blurred across the card and framed sharp in front, with its
    name large beside it. All CSS, templated by card-mod from the photo entity, so it follows the latest bird."""
    last, photo = b["last_bird"], b["photo"]
    none = f"states('{last}') in ('unknown', 'unavailable', 'none')"
    return {
        "type": "custom:mushroom-template-card", "entity": last, "icon": "mdi:bird", "layout": "horizontal",
        "primary": f"{{{{ 'No birds heard yet' if {none} else states('{last}') }}}}",
        "secondary": (f"{{% set c = state_attr('{last}', 'confidence') %}}"
                      f"{{% set t = state_attr('{last}', 'timestamp') %}}"
                      f"{{% set sci = state_attr('{last}', 'scientificName') %}}"
                      "{% if c and t %}{{ sci }} · {{ (c * 100) | round(0) | int }}% · "
                      "{{ relative_time(as_datetime(t)) }} ago{% else %}Listening…{% endif %}"),
        "tap_action": {"action": "none"}, "_style": "plain",
        "card_mod": {"style": {".": BIRD_HERO_STYLE.replace("@PHOTO@", photo) + BIRD_HERO_TEXT}},
        **grid(36, rows=6),
    }


def birds_view(b):
    """Birds page: the Saezuri collage (iframe via ingress), the last bird and the day's tallies."""
    src = b.get("saezuri_src")
    hero = bird_hero(b) if b.get("photo") else None
    tiles = ([] if hero else [bird_card(b)]) + bird_tiles(b, 12, None)
    for t in tiles:
        t.pop("grid_options", None)
    for key, label, icon in (("saezuri_app", "Open Saezuri", "mdi:bird"), ("birdnet_app", "Open BirdNET-Go", "mdi:waveform")):
        if b.get(key):
            tiles.append({"type": "tile", "entity": b["last_bird"], "name": label, "icon": icon, "color": "teal",
                          "hide_state": True, "tap_action": nav("/" + b[key])})
    if src:
        first = [heading("Recently heard", "mdi:bird"),
                 {"type": "iframe", "url": src, "aspect_ratio": "75%", **grid(24, rows=10)},
                 {"type": "vertical-stack", "cards": tiles, **grid(12)}]
    else:
        first = [heading("Birds", "mdi:bird")] + [{**t, **grid(12)} for t in tiles]
    sections = ([section([hero], span=3)] if hero else []) + [section(first, span=3)]
    lower = []
    if b.get("top_species"):
        lower.append(top_species_card(b, 18))
    if b.get("recent"):
        lower.append(recent_birds_card(b, 18))
    if lower:
        sections.append(section(lower, span=3))
    return {"title": "Birds", "path": "birds", "subview": True, "back_path": f"/{URL_PATH}/home",
            "type": "sections", "max_columns": 3, "dense_section_placement": True, "sections": sections}


def bird_room_section(b):
    """Extra bird detail for a room page (the garden): last bird, today's species, latest detections."""
    cards = [heading("Birds", "mdi:bird", tap=birds_path()), bird_card(b, tap=nav(birds_path()), **grid(36))]
    if b.get("top_species"):
        cards.append(top_species_card(b, 18))
    if b.get("recent"):
        cards.append(recent_birds_card(b, 18))
    return section(cards, span=3)


def resolve_saezuri(call, b):
    """Look up Saezuri's ingress URL (it changes whenever the app is reinstalled) for the Birds page iframe."""
    app = b.get("saezuri_app")
    if not app:
        return
    try:
        info = call("supervisor/api", endpoint=f"/addons/{app}/info", method="get")
        b["saezuri_src"] = info["ingress_entry"].rstrip("/") + "/"
    except (RuntimeError, KeyError) as e:
        print(f"WARNING: could not look up the Saezuri app {app!r} ({e}); the Birds page has no collage")


# --------------------------------------------------------------------------------------
# Energy: when it was used and what drove it
# --------------------------------------------------------------------------------------
ELECTRIC_COLOR, GAS_COLOR = "#8b7cff", "#ff8a4c"


def energy_path():
    return f"/{URL_PATH}/energy"


def apex(series, title=None, span="24h", start="day", height=170, offset=None, cols=12, header=None, **extra):
    """apexcharts-card drawn like Lumen's mini graphs: a soft gradient area for electricity over rounded gas
    columns, no toolbar, axes or legend clutter, and (optionally) the totals large in the header."""
    hourly = span.endswith("h")
    in_chart = [x for x in series if x.get("show", {}).get("in_chart", True)]
    c = {
        "type": "custom:apexcharts-card",
        "graph_span": span,
        "header": header or {"show": bool(title), "title": title or "", "show_states": False},
        "series": series,
        "yaxis": [{"id": "kwh", "show": False, "min": 0}, {"id": "gas", "show": False, "min": 0, "opposite": True}],
        "apex_config": {
            "chart": {"height": height, "toolbar": {"show": False}, "zoom": {"enabled": False},
                      "fontFamily": "inherit", "background": "transparent", "parentHeightOffset": 0,
                      "animations": {"enabled": False}},
            "grid": {"show": False, "padding": {"left": 6, "right": 6, "top": -14, "bottom": -6}},
            "plotOptions": {"bar": {"borderRadius": 3 if hourly else 5, "borderRadiusApplication": "end",
                                    "columnWidth": "55%" if hourly else "46%"}},
            "fill": {"type": ["gradient" if x.get("type") == "area" else "solid" for x in in_chart],
                     "gradient": {"type": "vertical", "shadeIntensity": 0, "opacityFrom": .45, "opacityTo": .02,
                                  "stops": [0, 95]}},
            "dataLabels": {"enabled": False},
            "legend": {"show": False},
            "markers": {"size": 0, "hover": {"size": 4}},
            "xaxis": {"labels": {"style": {"fontSize": "11px", "colors": "var(--secondary-text-color)"},
                                 "datetimeUTC": False, "format": "HH:mm" if hourly else "d MMM",
                                 "hideOverlappingLabels": True},
                      "tickAmount": 6 if hourly else 7,
                      "axisBorder": {"show": False}, "axisTicks": {"show": False},
                      "crosshairs": {"stroke": {"color": "rgba(127,127,127,.35)", "dashArray": 3}},
                      "tooltip": {"enabled": False}},
            "tooltip": {"shared": True, "x": {"format": "HH:mm" if hourly else "ddd d MMM"}},
        },
        "card_mod": {"style": GLASS + APEX_STYLE}, "_style": "plain",
        **grid(cols),
    }
    if start:
        c["span"] = {"start": start, **({"offset": offset} if offset else {})}
    c.update(extra)
    return c


# The header reads like a mini graph's: a quiet title, the totals large underneath.
APEX_STYLE = """
ha-card { padding: 4px 4px 2px; }
#header { padding: 12px 12px 0 !important; }
#header__title { font-size: 16px !important; font-weight: 400 !important; opacity: .7; padding-bottom: 2px; }
#header__states { gap: 18px; justify-content: flex-start !important; flex-wrap: nowrap !important; }
#states__state { padding: 0 !important; }
#state__value { font-size: 26px !important; font-weight: 400 !important; letter-spacing: -.01em; }
#state__value #uom { font-size: 13px !important; font-weight: 500 !important; opacity: .7; }
#state__name { font-size: 11px !important; opacity: .6 !important; text-transform: uppercase; letter-spacing: .06em; }
/* Phones: three totals still fit on one line. */
@media (max-width: 600px) {
  #header__states { gap: 12px; }
  #state__value { font-size: 19px !important; }
}
"""


def usage_series(e, period="hour", area=True):
    """Electricity per period from the power sensor's mean (a kW mean over an hour is that hour's kWh),
    gas per period from the meter's change."""
    kw = 1 if e.get("power_unit", "kW") == "kW" else 0.001
    factor = kw * (24 if period == "day" else 1)
    elec = {"entity": e["graph"], "name": "Electricity", "yaxis_id": "kwh", "color": ELECTRIC_COLOR,
            "unit": " kWh", "float_precision": 2, "transform": f"return x * {factor};",
            "statistics": {"type": "mean", "period": period, "align": "start"}, "show": {"in_header": False},
            "extend_to": False}
    elec.update({"type": "area", "curve": "smooth", "stroke_width": 2.5} if area else
                {"type": "column", "opacity": .95})
    return [elec,
            {"entity": e["gas"], "name": "Gas", "type": "column", "yaxis_id": "gas", "color": GAS_COLOR,
             "opacity": .9, "unit": " m³", "float_precision": 2, "show": {"in_header": False},
             "extend_to": False, "transform": "return x >= 0.01 ? x : null;",
             "statistics": {"type": "change", "period": period, "align": "start"}}]


def totals_header(e, title, now=False):
    """Today's electricity and gas, large in the chart header (needs daily utility meters in `today`), and
    with `now` the power right now."""
    t = dict(e.get("today") or {})
    if now:
        t["now"] = e["graph"]
    series = [{"entity": t[k], "name": name, "color": col, "unit": unit, "float_precision": prec,
               "show": {"in_chart": False, "in_header": True}}
              for k, name, col, unit, prec in (("electricity", "Electricity", ELECTRIC_COLOR, " kWh", 1),
                                               ("gas", "Gas", GAS_COLOR, " m³", 2),
                                               ("now", "Now", ELECTRIC_COLOR,
                                                " " + e.get("power_unit", "kW"), 2)) if t.get(k)]
    return series, {"show": True, "title": title, "show_states": bool(series), "colorize_states": False}


# The AI's story, quietly: a plain card, the icon says how the day went.
INSIGHT_STYLE = """
.row, .content { height: auto !important; align-items: flex-start !important; }
.content { padding: 14px 18px 16px 14px !important; gap: 12px; }
ha-tile-icon { margin: 0 !important; }
ha-tile-info { padding: 7px 0 0; }
span[slot=primary] { white-space: normal !important; font-size: 15px !important; line-height: 1.35 !important; }
span[slot=secondary] { white-space: normal !important; line-height: 1.5 !important; margin-top: 4px; }
"""
RATING = {"good": ("mdi:leaf", "green", "Better than usual"),
          "usual": ("mdi:scale-balance", "blue-grey", "About usual"),
          "high": ("mdi:alert-circle-outline", "deep-orange", "Higher than usual")}


def rating_template(entity, field):
    """Pick the icon, colour or label for the AI's good/usual/high rating."""
    i = {"icon": 0, "color": 1, "label": 2}[field]
    m = ", ".join(f"'{k}': '{v[i]}'" for k, v in RATING.items())
    fallback = {"icon": "mdi:creation", "color": "amber", "label": "The day in energy"}[field]
    return "{{ {%s}.get(states('%s') | lower | trim, '%s') }}" % (m, entity, fallback)


def insight_card(text, title=None, icon="mdi:creation", color="amber", tap=None, cols=12):
    """A few lines of the AI's energy story on a plain card that wraps its text."""
    c = {"type": "custom:mushroom-template-card", "icon": icon, "icon_color": color,
         "primary": title or f"{{{{ states('{text}') }}}}",
         "secondary": f"{{{{ states('{text}') }}}}" if title else "",
         "tap_action": tap or {"action": "none"}, "_style": "plain",
         "card_mod": {"style": GLASS + INSIGHT_STYLE}, **grid(cols, rows="auto")}
    return c


def energy_cards(e):
    """Home page: today hour by hour (with power right now in the header; both open the Energy dashboard),
    and the AI's verdict on the day, which opens the Energy page. Without gas, the power curve instead."""
    dash = e.get("dashboard", "/energy")
    cards = [heading("Energy", "mdi:lightning-bolt", tap=dash,
                     badges=[badge(b["entity"], b.get("color"), icon=b.get("icon"),
                                   tap_action=nav(dash)) for b in ents(e.get("badges"))])]
    hourly = e.get("graph") and e.get("gas")
    if e.get("graph") and not hourly:
        cards.append(mini_graph(
            [(e["graph"], "Power", "#f7b731")], name="Power today", hours=24, height=120,
            show={"fill": "fade", "labels": False, "legend": False, "extrema": True, "icon": False},
            color_thresholds=[{"value": 0, "color": "#20bf6b"}, {"value": 0.8, "color": "#f7b731"},
                              {"value": 2, "color": "#eb3b5a"}],
            tap_action={"action": "navigate", "navigation_path": dash}))
    if hourly:
        totals, head = totals_header(e, "Today, hour by hour", now=True)
        cards.append(apex(usage_series(e) + totals, height=120, header=head))
    ins = e.get("insight")
    if ins:
        r = ins.get("rating")
        cards.append(insight_card(ins["headline"], title="{{ states('%s') }}" % ins["headline"],
                                  icon=rating_template(r, "icon") if r else "mdi:creation",
                                  color=rating_template(r, "color") if r else "amber",
                                  tap=nav(energy_path())))
        cards[-1]["secondary"] = ("{{ states('%s') }}" % ins["verdict"]) if ins.get("verdict") else \
            "Tap for the full story"
    return cards


def energy_view(e):
    """Energy page: the AI's story of the last day (what was used when, and the likely cause), the last
    48 hours and 14 days by the hour/day, and what's drawing power right now."""
    dash = e.get("dashboard", "/energy")
    ins = e.get("insight") or {}
    sections = []
    if ins:
        r = ins.get("rating")
        label = (rating_template(r, "label") + " · ") if r else ""
        story = [{"type": "custom:mushroom-title-card", "title": "{{ states('%s') }}" % ins["headline"],
                  "subtitle": label + ("written {{ relative_time(states.%s.last_changed) }} ago" % ins["headline"]),
                  **grid(36)}]
        cards = (("verdict", "Verdict", rating_template(r, "icon") if r else "mdi:scale-balance",
                  rating_template(r, "color") if r else "blue-grey", 12),
                 ("waste", "Wasted power", "mdi:power-plug-off-outline", "red", 12),
                 ("tip", "Tip", "mdi:lightbulb-on-outline", "amber", 12),
                 ("gas", "Gas", "mdi:fire", "deep-orange", 18),
                 ("power", "Electricity", "mdi:flash", "indigo", 18))
        for key, title, icon, color, cols in cards:
            if ins.get(key):
                story.append(insight_card(ins[key], title=title, icon=icon, color=color, cols=cols))
        sections.append(section(story, span=3))
    chart = [heading("Last 48 hours", "mdi:chart-bar", tap=dash),
             apex(usage_series(e), span="48h", start="hour", offset="-47h", height=240, cols=36,
                  header={"show": False})]
    sections.append(section(chart, span=2))
    now = [heading("Right now", "mdi:gauge")]
    if e.get("graph"):
        now.append(tile(e["graph"], 12, name="Power now", icon="mdi:flash", color="amber"))
    if e.get("gas_flow"):
        f = e["gas_flow"]
        now.append(tile(f, 12, name="Gas flow", icon="mdi:fire", color="deep-orange", state_content=[
            "state"]))
    if e.get("baseload"):
        now.append(tile(e["baseload"], 12, name="Always on (24 h low)", icon="mdi:power-plug-outline",
                        color="blue-grey"))
    now += [tile_from(d, 6, color="green") for d in ents(e.get("devices"))]
    actions = []
    if ins.get("refresh"):
        actions.append(tile(ins["refresh"], 6, name="Rewrite story", icon="mdi:creation", color="amber",
                            hide_state=True, tap={"action": "perform-action", "perform_action": ins["refresh"]}))
    actions.append(tile(e["graph"], 6, name="Energy dashboard", icon="mdi:open-in-new", color="primary",
                        hide_state=True, tap=nav(dash)))
    sections.append(section(now + actions))
    sections.append(section([heading("Last 14 days", "mdi:calendar-month-outline", tap=dash),
                             apex(usage_series(e, "day", area=False), span="14d", start="day",
                                  offset="-14d", height=200, cols=36, header={"show": False})], span=3))
    return {"title": "Energy", "path": "energy", "subview": True, "back_path": f"/{URL_PATH}/home",
            "type": "sections", "max_columns": 3, "dense_section_placement": True, "sections": sections}


# --------------------------------------------------------------------------------------
# Media: the TV as Lumen cards (artwork, play/pause, volume, a remote pad); the music through Music
# Assistant on the Media page (Yet Another Media Player: search, favourites, radio)
# --------------------------------------------------------------------------------------
def media_path():
    return f"/{URL_PATH}/media"


MEDIA_STYLE = """
ha-card { overflow: hidden; }
"""

YAMP_BARE = """
.media-artwork-placeholder, .card-lower-fade, .card-lower-content-bg { display: none !important; }
.entity-options-overlay, .entity-options-sheet { background: transparent !important; }
"""

# Shown when an app sends no artwork (YouTube, Netflix and live TV mostly don't): soft colour, like stage light.
MEDIA_FALLBACK_ART = "data:image/svg+xml;utf8," + urllib.parse.quote(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 800 500' preserveAspectRatio='xMidYMid slice'>"
    "<defs>"
    "<radialGradient id='a' cx='82%' cy='18%' r='60%'><stop offset='0' stop-color='#8b7cff' stop-opacity='.75'/>"
    "<stop offset='1' stop-color='#8b7cff' stop-opacity='0'/></radialGradient>"
    "<radialGradient id='b' cx='12%' cy='88%' r='65%'><stop offset='0' stop-color='#ff8a4c' stop-opacity='.6'/>"
    "<stop offset='1' stop-color='#ff8a4c' stop-opacity='0'/></radialGradient>"
    "<radialGradient id='c' cx='58%' cy='96%' r='45%'><stop offset='0' stop-color='#ff5fa2' stop-opacity='.45'/>"
    "<stop offset='1' stop-color='#ff5fa2' stop-opacity='0'/></radialGradient>"
    "</defs><rect width='800' height='500' fill='#15121f'/>"
    "<rect width='800' height='500' fill='url(#a)'/><rect width='800' height='500' fill='url(#b)'/>"
    "<rect width='800' height='500' fill='url(#c)'/></svg>")

# Android TV Remote key names (remote.send_command); override per key with `remote_keys` for other remotes.
REMOTE_KEYS = {"up": "DPAD_UP", "down": "DPAD_DOWN", "left": "DPAD_LEFT", "right": "DPAD_RIGHT",
               "select": "DPAD_CENTER", "back": "BACK", "home": "HOME", "menu": "MENU"}
# Apps that send no artwork still get recognised: their icon and colour stand in for the picture.
APP_ICONS = {"youtube": ("mdi:youtube", "red"), "netflix": ("mdi:netflix", "red"),
             "spotify": ("mdi:spotify", "green"), "plex": ("mdi:plex", "amber"), "kodi": ("mdi:kodi", "blue")}
PLAYBACK = ["media_previous_track", "media_play_pause", "media_next_track"]
# A media card: what's playing over its own picture, the TV's screen or the album art. On a wide card the
# picture fills the right (a 16:9 frame at the card's 248 px is ~440 px wide) and fades in, so the title and
# controls on the left stay readable; on a narrow card or a phone it fills the top instead. Two layers: the
# previous frame (already loaded) under the new one, so a refresh never flashes empty. @PIC@ and @PREV@ are
# template expressions.
MEDIA_CARD_WIDE = """
ha-card {
  background:
    linear-gradient(90deg, var(--card-background-color) calc(100% - 440px), color-mix(in srgb, var(--card-background-color) 55%, transparent) calc(100% - 340px), transparent calc(100% - 190px)),
    linear-gradient(0deg, color-mix(in srgb, var(--card-background-color) 55%, transparent), transparent 40%),
    {% if pic %}url('{{ pic }}') right center / auto 100% no-repeat,{% endif %}
    {% if prev %}url('{{ prev }}') right center / auto 100% no-repeat,{% endif %}
    var(--card-background-color) !important;
}
.content { max-width: 50%; }
hui-card-features { max-width: 50%; }
"""
MEDIA_CARD_NARROW = """
ha-card {
  background:
    linear-gradient(0deg, var(--card-background-color) 24%, color-mix(in srgb, var(--card-background-color) 55%, transparent) 46%, transparent 72%),
    linear-gradient(180deg, color-mix(in srgb, var(--card-background-color) 85%, transparent),
                    color-mix(in srgb, var(--card-background-color) 55%, transparent) 30%, transparent 55%),
    {% if pic %}url('{{ pic }}') center top / cover no-repeat,{% endif %}
    {% if prev %}url('{{ prev }}') center top / cover no-repeat,{% endif %}
    var(--card-background-color) !important;
}
.content, hui-card-features { max-width: none; }
"""
MEDIA_CARD_BASE = """
{% set pic = @PIC@ %}{% set prev = @PREV@ %}
.container { height: 100%; justify-content: space-between; }
.row, .content { align-items: flex-start !important; }
.content { padding: 18px 18px 0 16px !important; box-sizing: border-box; }
span[slot=primary] {
  font-size: 19px !important; font-weight: 650 !important; line-height: 1.3 !important; letter-spacing: -.01em;
  white-space: normal !important; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
}
span[slot=secondary] { font-size: 13px !important; margin-top: 4px; }
"""


def media_card_style(wide):
    """Wide cards fade the picture in from the right, and stack like narrow ones on phones."""
    if wide:
        return MEDIA_CARD_BASE + MEDIA_CARD_WIDE + "@media (max-width: 600px) {\n" + MEDIA_CARD_NARROW + "}\n"
    return MEDIA_CARD_BASE + MEDIA_CARD_NARROW

PAD_STYLE = """
ha-card { border-radius: 20px !important; height: @H@px; justify-content: center; padding: 0 !important; }
ha-card:active { scale: .94; transition: scale .1s; }
"""


def tvs(m):
    """The TVs: `tvs` (a list), or the single `tv`. Each belongs to a `room`; the first defaults to media's."""
    out = [dict(t) for t in (m.get("tvs") or ([m["tv"]] if m.get("tv") else []))]
    if out:
        out[0].setdefault("room", m.get("room"))
    return out


def media_on(m):
    """Visibility: a TV is on, a console is in a game, or one of the music players is playing."""
    conds = [tv_on(t) for t in tvs(m)] + [in_game(c) for c in consoles(m)]
    conds += [{"condition": "state", "entity": p["entity"], "state": "playing"} for p in ents(m.get("players"))]
    return [{"condition": "or", "conditions": conds}]


def tv_on(t):
    return {"condition": "state", "entity": t.get("power", t["entity"]), "state": "on"}


def player_card(entity, name, cols, rows=4, volume=False, tap=None, pic=None, prev=None):
    """What's playing on a Lumen media card: title and artist, previous/play-pause/next (and the volume),
    over the card's picture: `pic` (and the frame before, `prev`) as template expressions, or the player's
    own artwork. The app's icon says where it's playing. Tapping opens the player's full controls."""
    a = lambda k: f"state_attr('{entity}', '{k}')"
    app = f"({a('app_name')} or '') | lower"
    icons = "{%s}" % ", ".join(f"'{k}': '{v[0]}'" for k, v in APP_ICONS.items())
    colors = "{%s}" % ", ".join(f"'{k}': '{v[1]}'" for k, v in APP_ICONS.items())
    # Power lives on the remote pad; here just previous, play/pause and next.
    feats = [{"type": "media-player-playback", "controls": PLAYBACK}] + \
        ([{"type": "media-player-volume-slider"}] if volume else [])
    style = media_card_style(cols > 12).replace("@PIC@", pic or a("entity_picture")).replace("@PREV@", prev or "none")
    return {"type": "custom:mushroom-template-card", "entity": entity,
            "primary": "{{ %s or %s or '%s' }}" % (a("media_title"), a("app_name"), name),
            "secondary": "{{ [%s, %s] | select | join(' · ') }}" % (a("media_artist"), a("app_name")),
            "icon": "{{ %s.get(%s, 'mdi:play-circle-outline') }}" % (icons, app),
            "icon_color": "{{ %s.get(%s, 'pink') }}" % (colors, app),
            "features": feats, "features_position": "bottom",
            "tap_action": tap or {"action": "more-info"}, "_style": "plain",
            "card_mod": {"style": GLASS + style}, **grid(cols, rows)}


def pad_button(icon, tap, height=56):
    return {"type": "button", "icon": icon, "show_name": False, "show_state": False, "icon_height": "22px",
            "tap_action": tap, "hold_action": {"action": "none"}, "_style": "plain",
            "card_mod": {"style": GLASS + PAD_STYLE.replace("@H@", str(height))}}


def remote_pad(t, corner, cols, rows=3):
    """One card, three rows of three: back, up, home / left, OK, right / power, down and `corner`. Given more
    grid rows, the buttons grow to fill them."""
    h = (rows * 56 + (rows - 1) * 8 - 16) // 3
    keys = {**REMOTE_KEYS, **t.get("remote_keys", {})}
    key = lambda k, icon: pad_button(icon, {"action": "perform-action", "perform_action": "remote.send_command",
                                            "target": {"entity_id": t["remote"]}, "data": {"command": keys[k]}}, h)
    power = ({"action": "perform-action", "perform_action": "media_player.toggle",
              "target": {"entity_id": t["power"]}} if t.get("power") else
             {"action": "perform-action", "perform_action": "remote.toggle", "target": {"entity_id": t["remote"]}})
    buttons = [key("back", "mdi:arrow-left"), key("up", "mdi:chevron-up"), key("home", "mdi:home-outline"),
               key("left", "mdi:chevron-left"), key("select", "mdi:circle-outline"), key("right", "mdi:chevron-right"),
               pad_button("mdi:power", power, h), key("down", "mdi:chevron-down"), corner(h)]
    return {"type": "grid", "columns": 3, "square": False, "cards": buttons, **grid(cols, rows)}


def tv_cards(t, wide, full=False):
    """The TV: one media card (what's on, play/pause, over the TV's own screen when there's a `preview`
    camera and a `preview_frame` counter that ticks with every new screenshot), the remote pad beside it,
    and the volume of whatever plays the sound under the pad. Wide (a three-column section) side by side,
    narrow stacked. On the home page the pad's corner button opens the Media page; on the Media page itself
    it's the TV's menu key."""
    pic = prev = None
    if t.get("preview") and t.get("preview_frame"):
        url = f"state_attr('{t['preview']}', 'entity_picture')"
        frame = f"states('{t['preview_frame']}') | int(0)"
        pic = f"({url} ~ '&v=' ~ ({frame})) if {url} else none"
        prev = f"({url} ~ '&v=' ~ ({frame} - 1)) if {url} else none"
    player = player_card(t["entity"], t.get("name", "TV"), 24 if wide else 12, pic=pic, prev=prev)
    volume = tile(t.get("volume", t["entity"]), 12, name="Volume", icon="mdi:volume-high",
                  color="primary", hide_state=True, features=[{"type": "media-player-volume-slider"}])
    volume["features_position"] = "inline"
    if not t.get("remote"):
        return [player, volume]
    menu = {**REMOTE_KEYS, **t.get("remote_keys", {})}["menu"]
    corner = ((lambda h: pad_button("mdi:menu", {"action": "perform-action", "perform_action": "remote.send_command",
                                                 "target": {"entity_id": t["remote"]}, "data": {"command": menu}}, h))
              if full else (lambda h: pad_button("mdi:dots-horizontal", nav(media_path()), h)))
    # In this order the section's grid puts the pad beside the card and the volume under the pad.
    return [player, remote_pad(t, corner, 12), volume]


def music_cards(m, cols):
    """A card per music player, shown while it plays, except while its `tv` (a TV's name) is on: then
    it's playing the TV's sound, and the TV's card already shows it."""
    out = []
    by_name = {t.get("name"): t for t in tvs(m)}
    for p in ents(m.get("players")):
        c = player_card(p["entity"], p.get("name", friendly(p["entity"])), cols, volume=True)
        c["visibility"] = [{"condition": "state", "entity": p["entity"], "state": "playing"}]
        tv = by_name.get(p.get("tv"))
        if tv:
            c["visibility"].append({"condition": "state", "entity": tv.get("power", tv["entity"]), "state_not": "on"})
        out.append(c)
    return out


def now_playing(m, wide=True):
    """The home page's 'now playing': each TV with its remote while it's on, and any music that's playing."""
    cards = [heading("Now playing", "mdi:play-circle-outline", tap=media_path())]
    for t in tvs(m):
        gaming = [c for c in consoles(m) if c.get("tv") == t.get("name")]
        for c in tv_cards(t, wide):
            # while its console is in a game, the TV is showing the game: that card says it
            c["visibility"] = [tv_on(t)] + [{"condition": "state", "entity": g["entity"], "state_not": "playing"} for g in gaming]
            cards.append(c)
    for c in consoles(m):
        card = game_card(c, 36 if wide else 12)
        card["visibility"] = [in_game(c)]
        cards.append(card)
    return cards + music_cards(m, 36 if wide else 12)


def app_tiles(t):
    """App shortcuts on the TV's remote, and live TV."""
    out = [tile(t["remote"], 12, name=a["name"], icon=a.get("icon", "mdi:application"), color="red",
                hide_state=True, tap={"action": "perform-action", "perform_action": "remote.turn_on",
                                      "target": {"entity_id": t["remote"]}, "data": {"activity": a["activity"]}})
           for a in t.get("apps", []) if t.get("remote")]
    lt = t.get("live_tv")
    if lt:
        out.append(tile(lt["entity"], 12, name="Live TV", icon="mdi:television-classic", color="blue",
                        hide_state=True, tap={"action": "perform-action", "perform_action": "media_player.select_source",
                                              "target": {"entity_id": lt["entity"]},
                                              "data": {"source": lt.get("source", "Watch TV")}}))
    return out


def yamp_entities(m):
    """The music players as the card's chips."""
    return [{"entity_id": pl["entity"], "name": pl.get("name"),
             "music_assistant_entity": pl.get("music_assistant", pl["entity"])} for pl in ents(m.get("players"))]


def yamp(entities, cols=24, height=380, actions=None, **extra):
    c = {"type": "custom:yet-another-media-player", "entities": entities, "actions": actions or [],
         "match_theme": True, "control_layout": "modern", "adaptive_controls": True,
         "display_timestamps": True, "extend_artwork": True, "artwork_object_fit": "cover",
         "artwork_position": "center center", "show_chip_row": "always", "card_height": height,
         "idle_timeout_ms": 0, "dim_chips_on_idle": False, "default_search_filter": "all",
         "default_search_favorites": True, "search_view": "card", "search_card_columns": 4,
         "volume_mode": "slider", "_style": "plain", "card_mod": {"style": GLASS + MEDIA_STYLE},
         "media_artwork_overrides": [{"missing_art_url": MEDIA_FALLBACK_ART}], "idle_image": MEDIA_FALLBACK_ART,
         **grid(cols, rows="auto")}
    if extra.get("template") == "dedicated_search":
        # Just the list: no artwork placeholder or fade behind it.
        c.update(artwork_object_fit="no_artwork", show_chip_row="auto", disable_artwork_gradient=True)
        c.pop("media_artwork_overrides"); c.pop("idle_image")
        c["card_mod"] = {"style": GLASS + MEDIA_STYLE + YAMP_BARE}
    c.update(extra)
    return c


# --------------------------------------------------------------------------------------
# Game consoles: the game over its art, and (PlayStation Network) the account and its trophies
# --------------------------------------------------------------------------------------
TROPHIES = (("platinum", "Platinum", "light-blue"), ("gold", "Gold", "amber"),
            ("silver", "Silver", "blue-grey"), ("bronze", "Bronze", "deep-orange"))
PSN_STATUS = {"availabletoplay": "Online", "availabletocommunicate": "Online on the app", "busy": "Busy",
              "offline": "Offline"}


def consoles(m):
    """The consoles (`consoles`), each in a `room` (default: media's)."""
    out = ents(m.get("consoles"))
    for c in out:
        c.setdefault("room", m.get("room"))
    return out


def in_game(c):
    return {"condition": "state", "entity": c["entity"], "state": "playing"}


def psn(c, key):
    """An entity of the console's PSN account: the integration names them after the online id (`psn`)."""
    domain, name = key.split(".")
    return f"{domain}.{c['psn']}_{name}"


def game_card(c, cols, rows=4):
    """The game being played: its title over its art (the PlayStation Network integration's media player
    has both), the console, who's signed in and for how long."""
    e = c["entity"]
    who = f"states('{psn(c, 'sensor.online_id')}')" if c.get("psn") else "none"
    style = media_card_style(cols > 12).replace("@PIC@", f"state_attr('{e}', 'entity_picture')").replace("@PREV@", "none")
    return {"type": "custom:mushroom-template-card", "entity": e,
            "primary": "{{ state_attr('%s', 'media_title') or '%s' }}" % (e, c.get("name", "Game")),
            "secondary": "{{ ['%s', %s, 'for ' ~ relative_time(states.%s.last_changed)] | select | join(' · ') }}"
                         % (c.get("name", "Console"), who, e),
            "icon": c.get("icon", "mdi:sony-playstation"), "icon_color": "blue",
            "tap_action": {"action": "more-info"}, "_style": "plain",
            "card_mod": {"style": GLASS + style}, **grid(cols, rows)}


def psn_cards(c):
    """The account: avatar, online id, online or what's being played, the trophy level; the trophies."""
    if not c.get("psn"):
        return []
    status, level, nxt = psn(c, "sensor.online_status"), psn(c, "sensor.trophy_level"), psn(c, "sensor.next_level")
    names = "{%s}" % ", ".join(f"'{k}': '{v}'" for k, v in PSN_STATUS.items())
    profile = {"type": "custom:mushroom-template-card", "entity": psn(c, "sensor.online_id"),
               "picture": "{{ state_attr('%s', 'entity_picture') }}" % psn(c, "image.avatar"),
               "primary": "{{ states('%s') }}" % psn(c, "sensor.online_id"),
               "secondary": ("{%% set g = state_attr('%s', 'media_title') %%}"
                             "{{ ('Playing ' ~ g) if is_state('%s', 'playing') and g else %s.get(states('%s'), states('%s')) }}"
                             " · Level {{ states('%s') }} · {{ states('%s') }}%% to the next")
                            % (c["entity"], c["entity"], names, status, status, level, nxt),
               "badge_icon": "{{ 'mdi:circle' if states('%s') not in ['offline', 'unavailable', 'unknown'] else '' }}" % status,
               "badge_color": "green", "tap_action": {"action": "more-info"}, **grid(12)}
    trophies = [tile(psn(c, f"sensor.{k}_trophies"), 3, vertical=True, name=n, icon="mdi:trophy", color=col)
                for k, n, col in TROPHIES if usable(psn(c, f"sensor.{k}_trophies"))]
    return [profile] + trophies


def console_section(c):
    """A console on the Media page: the game while one is on, the account, the trophies."""
    plus = psn(c, "binary_sensor.subscribed_to_playstation_plus") if c.get("psn") else None
    badges = [{**badge(plus, "amber", name="PS Plus", icon="mdi:plus-circle"), "show_state": False,
               "visibility": [{"condition": "state", "entity": plus, "state": "on"}]}] if plus and usable(plus) else []
    game = game_card(c, 36)
    game["visibility"] = [in_game(c)]
    idle = tile(c["entity"], 12, name=c.get("name"), icon=c.get("icon", "mdi:sony-playstation"), color="blue")
    idle["visibility"] = [{"condition": "state", "entity": c["entity"], "state_not": "playing"}]
    return section([heading(c.get("name", "Console"), c.get("icon", "mdi:sony-playstation"), badges=badges),
                    game, idle] + psn_cards(c), span=3)


def media_view(m):
    """Media page: each TV (what's on, volume, the full remote, apps), the consoles (the game, the account,
    its trophies) and the music (Music Assistant favourites, search, and grouping the speakers into one)."""
    sections = [section([heading(t.get("name", "TV"), "mdi:television")] + tv_cards(t, True, full=True)
                        + app_tiles(t), span=3) for t in tvs(m)]
    sections += [console_section(c) for c in consoles(m)]
    if m.get("players"):
        music = [heading("Music", "mdi:music-circle-outline"),
                 yamp(yamp_entities(m), cols=36, height=420,
                      actions=[{"name": "Search", "icon": "mdi:magnify", "menu_item": "search"},
                               {"name": "Recently played", "icon": "mdi:history",
                                "menu_item": "search-recently-played"}])]
        sections.append(section(music, span=2))
        first = yamp_entities(m)[:1]
        sections.append(section([heading("Favourites", "mdi:heart-outline"),
                                 yamp(first, cols=12, height=420, template="dedicated_search", search_view="list")]))
    return {"title": m.get("title", "Media"), "path": "media", "subview": True, "back_path": f"/{URL_PATH}/home",
            "type": "sections", "max_columns": 3, "dense_section_placement": True, "sections": sections}


# --------------------------------------------------------------------------------------
# Robot vacuum: a page that really drives it — map, rooms, suction, mop, maintenance
# --------------------------------------------------------------------------------------
ROOM_ICONS = {"living": "mdi:sofa-outline", "kitchen": "mdi:silverware-fork-knife", "hall": "mdi:stairs",
              "entry": "mdi:door-open", "toilet": "mdi:toilet", "gray": "mdi:bed-outline",
              "grey": "mdi:bed-outline", "study": "mdi:desk", "bed": "mdi:bed-king-outline",
              "laundry": "mdi:washing-machine", "shower": "mdi:shower-head", "bath": "mdi:bathtub-outline"}
FAN_ICONS = {"quiet": "mdi:fan-speed-1", "gentle": "mdi:feather", "balanced": "mdi:fan-speed-2",
             "standard": "mdi:fan-speed-2", "turbo": "mdi:fan-speed-3", "max": "mdi:fan-plus"}


def vacuum_path():
    return f"/{URL_PATH}/vacuum"


def vacuum_maps(call, v):
    """Rooms per map from roborock.get_maps (segment ids are per map), and each map's image entity."""
    try:
        r = call("call_service", domain="roborock", service="get_maps",
                 target={"entity_id": v["entity"]}, return_response=True)
        maps = r["response"][v["entity"]]["maps"]
    except (RuntimeError, KeyError, TypeError) as err:
        print(f"WARNING: could not read the vacuum's maps ({err}); the vacuum page has no rooms")
        return
    base = friendly(v["entity"])
    images = {s["attributes"].get("friendly_name"): e for e, s in STATES.items() if e.startswith("image.")}
    v["maps"] = [{"name": m["name"], "rooms": [(int(k), n) for k, n in m.get("rooms", {}).items()],
                  "image": images.get(f"{base} {m['name']}")} for m in maps]


def vacuum_status_template(v):
    """A friendly status line with a bit of personality."""
    e, name = v["entity"], v.get("name", "The vacuum")
    room = f"states('{v['current_room']}')" if v.get("current_room") else "''"
    return (f"{{% set s = states('{e}') %}}{{% set r = {room} %}}"
            f"{{% set b = state_attr('{e}', 'battery_level') or states('{v.get('battery', '')}') %}}"
            "{% set here = (' in the ' ~ r | lower) if r not in ('', 'unknown', 'unavailable') else '' %}"
            "{% if s == 'cleaning' %}" + name + " is hunting crumbs" + "{{ here }} 🧹"
            "{% elif s == 'returning' %}" + name + " is heading home 🏠"
            "{% elif s == 'paused' %}" + name + " is taking a breather" + "{{ here }}"
            "{% elif s == 'error' %}" + name + " is stuck and needs a hand 🆘"
            "{% elif s == 'docked' and b | int(100) < 100 %}" + name + " is recharging · {{ b }}% ⚡"
            "{% elif s == 'docked' %}" + name + " is resting on the throne 👑"
            "{% else %}" + name + " is {{ s }}{% endif %}")


def vacuum_view(v):
    e, name = v["entity"], v.get("name", "Vacuum")
    svc = lambda action, **data: {"action": "perform-action", "perform_action": action,  # noqa: E731
                                  **({"data": data} if data else {}), "target": {"entity_id": e}}
    script = lambda s, **data: {"action": "perform-action", "perform_action": s,  # noqa: E731
                                **({"data": data} if data else {})}
    sel = v.get("selection")  # input_text holding the chosen segment ids
    maps = v.get("maps") or []
    map_sel = v.get("map_select")

    # ---- Hero: status line, map, and the big controls
    status = {"type": "custom:mushroom-title-card", "title": vacuum_status_template(v),
              "subtitle": (f"{{% set a = states('{v['area']}') if '{v.get('area', '')}' else '' %}}"
                           f"{{% set t = states('{v.get('clean_time', '')}') %}}"
                           f"Battery {{{{ state_attr('{e}', 'battery_level') or states('{v.get('battery', '')}') }}}}%"
                           f"{{{{ ' · last run ' ~ a ~ ' m² in ' ~ (t | float(0)) | round(0) | int ~ ' min' if a not in ('', 'unknown', 'unavailable') else '' }}}}"
                           f"{{{{ ' · fan ' ~ state_attr('{e}', 'fan_speed') if state_attr('{e}', 'fan_speed') else '' }}}}")
              if v.get("area") else f"Battery {{{{ state_attr('{e}', 'battery_level') }}}}%", **grid(36)}
    hero = [status]
    for m in maps:
        if not m.get("image"):
            continue
        card = {"type": "picture-entity", "entity": m["image"], "show_name": False, "show_state": False,
                "fit_mode": "contain", "aspect_ratio": "4:3", "tap_action": {"action": "more-info"},
                "_style": "plain", "card_mod": {"style": VACUUM_MAP_STYLE.replace("@NAME@", json.dumps(m["name"]))},
                **grid(20, rows=5)}
        if map_sel and len(maps) > 1:
            card["visibility"] = [{"condition": "state", "entity": map_sel, "state": m["name"]}]
        hero.append(card)
    big = []
    for label, icon, color, action, show in (
            ("Start", "mdi:play", "teal", svc("vacuum.start"), ["docked", "idle", "paused", "returning", "error"]),
            ("Pause", "mdi:pause", "amber", svc("vacuum.pause"), ["cleaning"]),
            ("Dock", "mdi:home-import-outline", "blue", svc("vacuum.return_to_base"), None),
            ("Spot", "mdi:target", "deep-purple", svc("vacuum.clean_spot"), None),
            ("Find me", "mdi:map-marker-radius-outline", "pink", svc("vacuum.locate"), None),
            ("Stop", "mdi:stop", "red", svc("vacuum.stop"), ["cleaning", "paused", "returning"])):
        c = {"type": "custom:mushroom-template-card", "icon": icon, "icon_color": color, "primary": label,
             "vertical": True, "tap_action": action, "entity": e, **grid(4, rows=2)}
        if show:
            c["visibility"] = [{"condition": "state", "entity": e, "state": show}]
        big.append(c)
    hero += big
    # suction: one chip per fan speed, the current one lit
    fans = STATES.get(e, {}).get("attributes", {}).get("fan_speed_list") or []
    if fans:
        hero.append({"type": "custom:mushroom-chips-card", "alignment": "justify", "chips": [
            {"type": "template", "icon": FAN_ICONS.get(f, "mdi:fan"), "content": f.capitalize(),
             "icon_color": f"{{{{ 'teal' if state_attr('{e}', 'fan_speed') == '{f}' else 'disabled' }}}}",
             "tap_action": svc("vacuum.set_fan_speed", fan_speed=f),
             "card_mod": {"style": f"{{% if state_attr('{e}', 'fan_speed') == '{f}' %}}ha-card {{ "
                                   "background: color-mix(in srgb, #2dd4bf 18%, transparent) !important; "
                                   "border-color: color-mix(in srgb, #2dd4bf 45%, transparent) !important; }"
                                   "{% endif %}"}}
            for f in fans], **grid(16)})
    # cleaning mode and mop water sit beside the map too
    for key, label, icon in (("cleaning_mode", "Cleaning", "mdi:robot-vacuum"),
                             ("mop_intensity", "Mop water", "mdi:water-outline")):
        if v.get(key) and usable(v[key]):
            hero.append(tile(v[key], 8, name=label, icon=icon, color="teal", features=[{"type": "select-options"}]))
    sections = [section(hero, span=3)]

    # ---- Rooms of the loaded map: tap to pick, hold to clean that room right away
    if maps and sel:
        rooms = [heading("Rooms", "mdi:floor-plan")]
        if map_sel:
            rooms.append(tile(map_sel, 12, name="Map", icon="mdi:map-outline", color="teal", rows=2,
                              features=[{"type": "select-options"}]))
        chosen = (f"states('{sel}').split(',') | map('trim') | select('match', '^[0-9]+$') | map('int') | list")
        for m in maps:
            for seg, rname in m["rooms"]:
                icon = next((i for k, i in ROOM_ICONS.items() if k in rname.lower()), "mdi:texture-box")
                on = f"{seg} in {chosen}"
                c = {"type": "custom:mushroom-template-card", "icon": icon, "primary": rname, "vertical": True,
                     "icon_color": f"{{{{ 'teal' if {on} else 'disabled' }}}}",
                     "secondary": f"{{{{ 'Picked' if {on} else 'Tap to pick' }}}}",
                     "tap_action": script(v["toggle_script"], segment=seg),
                     "hold_action": {**script(v["clean_room_script"], segment=seg),
                                     "confirmation": {"text": f"Clean the {rname.lower()} now?"}},
                     "_style": "plain",
                     "card_mod": {"style": GLASS + f"{{% if {on} %}}{ROOM_PICKED_STYLE}{{% endif %}}"},
                     **grid(6, rows=2)}
                if map_sel and len(maps) > 1:
                    c["visibility"] = [{"condition": "state", "entity": map_sel, "state": m["name"]}]
                rooms.append(c)
        n = f"({chosen}) | count"
        has = [{"condition": "state", "entity": sel, "state_not": ""}]
        rooms.append({"type": "custom:mushroom-template-card", "icon": "mdi:broom", "icon_color": "teal",
                      "primary": f"{{% set n = {n} %}}Clean {{{{ n }}}} room{{{{ 's' if n != 1 else '' }}}}",
                      "secondary": (f"{{% set p = states('{v['passes']}') | int(1) %}}"
                                    "{{ p }} pass{{ 'es' if p > 1 else '' }} · tap to start"
                                    if v.get("passes") else "Tap to start"),
                      "tap_action": script(v["clean_script"]), "visibility": has, **grid(18, rows=2)})
        if v.get("passes"):
            rooms.append({**tile(v["passes"], 12, name="Passes", icon="mdi:repeat", color="teal",
                                 features=[{"type": "numeric-input", "style": "buttons"}]), "visibility": has})
        rooms.append({**tile(sel, 6, name="Clear", icon="mdi:close", color="grey", hide_state=True,
                             tap=script(v["clear_script"])), "visibility": has})
        sections.append(section(rooms, span=3))

    # ---- Mode, maintenance, quiet hours
    mode = [heading("Settings", "mdi:tune-variant")]
    if v.get("quiet_hours"):
        mode.append(tile(v["quiet_hours"], 12, name="Quiet hours", icon="mdi:sleep", color="indigo",
                         state_content=["state"]))
    if v.get("volume") and usable(v["volume"]):
        mode.append(tile(v["volume"], 12, name="Voice volume", icon="mdi:volume-high", color="teal",
                         features=[{"type": "numeric-input", "style": "slider"}]))
    sections.append(section(mode))
    care = [heading("Maintenance", "mdi:wrench-outline")]
    for key, label, icon in (("brush", "Main brush", "mdi:brush"), ("side_brush", "Side brush", "mdi:brush-variant"),
                             ("filter", "Filter", "mdi:air-filter"), ("sensors", "Sensors", "mdi:eye-outline")):
        if v.get(key) and usable(v[key]):
            ent_ = v[key]
            care.append({**tile(ent_, 6, name=label, icon=icon, color="teal"), "_style": "plain",
                         "card_mod": {"style": GLASS + f"{{% if states('{ent_}') | float(99) < 5 %}}"
                                      "ha-card { border-color: color-mix(in srgb, #ff5a5f 55%, transparent) !important; }"
                                      " ha-tile-icon { --tile-icon-color: #ff5a5f; }{% endif %}"}})
    sections.append(section(care))
    stats = [heading("History", "mdi:history")]
    for key, label, icon in (("last_clean", "Last clean", "mdi:broom"), ("total_count", "Runs", "mdi:counter"),
                             ("total_area", "Area cleaned", "mdi:texture-box"), ("error", "Error", "mdi:alert-circle-outline")):
        if v.get(key) and usable(v[key]):
            stats.append(tile(v[key], 6, name=label, icon=icon, color="teal" if key != "error" else "red"))
    sections.append(section(stats))
    return {"title": name, "path": "vacuum", "subview": True, "back_path": f"/{URL_PATH}/home",
            "type": "sections", "max_columns": 3, "dense_section_placement": True, "sections": sections}


VACUUM_MAP_STYLE = """
ha-card {
  border-radius: 28px !important; overflow: hidden; height: 100%;
  background: radial-gradient(120% 90% at 50% 0%, color-mix(in srgb, #2dd4bf 10%, transparent), transparent 70%),
              color-mix(in srgb, var(--card-background-color) 72%, transparent) !important;
  border: 1px solid color-mix(in srgb, var(--primary-text-color) 9%, transparent) !important;
  box-shadow: 0 10px 30px -12px rgba(0, 0, 0, .35) !important;
}
hui-image { height: 100%; padding: 12px; box-sizing: border-box; }
ha-card::after {
  content: @NAME@; position: absolute; top: 16px; left: 20px; pointer-events: none;
  font-size: 13px; font-weight: 650; letter-spacing: .04em; text-transform: uppercase; opacity: .6;
}
"""
ROOM_PICKED_STYLE = """
ha-card { border-color: color-mix(in srgb, #2dd4bf 60%, transparent) !important;
  background: color-mix(in srgb, #2dd4bf 14%, var(--card-background-color)) !important;
  box-shadow: 0 0 0 1px color-mix(in srgb, #2dd4bf 50%, transparent), 0 14px 30px -14px rgba(45, 212, 191, .5) !important; }
ha-tile-icon { scale: 1.08; }
"""


# --------------------------------------------------------------------------------------
# Scenes: a row of big buttons per room; the scene last used glows while the room is lit
# --------------------------------------------------------------------------------------
def scenes_section(r):
    scenes = ents(r.get("scenes"))
    lights = room_lights(r)
    ids_ = [s["entity"] for s in scenes]
    lit = lights_on(lights)[3:-3] + " > 0" if lights else "true"
    latest = ("{% set ts = " + json.dumps(ids_) + " | map('states') | reject('in', ['unknown', 'unavailable']) "
              "| list %}{% set latest = ts | max if ts else '' %}")
    n_cards = len(scenes) + (1 if lights else 0)
    # One row across on wide screens; three to a row on phones, where a section is 12 columns wide.
    variants = [(max(4, min(12, 36 // n_cards)), "(min-width: 768px)"), (4, "(max-width: 767px)")]
    cards = [heading("Scenes", "mdi:palette-outline")]
    for cols, query in variants:
        screen = [{"condition": "screen", "media_query": query}]
        for s in scenes:
            style = (GLASS + latest + f"{{% if {lit} and states('{s['entity']}') == latest %}}" + SCENE_ON_STYLE
                     + "{% endif %}")
            cards.append({**tile(s["entity"], cols, vertical=True, name=s.get("name"),
                                 icon=s.get("icon", "mdi:palette"), color=s.get("color", "deep-purple"),
                                 hide_state=True, tap=scene_tap(s["entity"])),
                          "_style": "plain", "card_mod": {"style": style}, "visibility": screen})
        if lights:
            n = lights_on(lights)[3:-3]
            cards.append({"type": "custom:mushroom-template-card", "primary": "All off", "vertical": True,
                          "icon": "mdi:lightbulb-off-outline",
                          "secondary": f"{{% set n = {n} %}}{{{{ 'Everything off' if n == 0 else n ~ ' on' }}}}",
                          "icon_color": f"{{{{ 'disabled' if {n} == 0 else 'red' }}}}",
                          "tap_action": {"action": "perform-action", "perform_action": "light.turn_off",
                                         "target": {"entity_id": lights}, "data": {"transition": 3}},
                          "visibility": screen, **grid(cols)})
    return section(cards, span=3)


SCENE_ON_STYLE = """
ha-card { border-color: color-mix(in srgb, var(--tile-color) 55%, transparent) !important;
  background: color-mix(in srgb, var(--tile-color) 12%, var(--card-background-color)) !important; }
ha-tile-icon { scale: 1.12; }
"""


# --------------------------------------------------------------------------------------
# LED strips (WLED): the strip with its brightness, its favourites (WLED presets) as big
# swatches in their own colours, the one it's on glowing, and the switches that go with it
# --------------------------------------------------------------------------------------
# A favourite's colours, when house.yaml doesn't give them: from what it's called.
FAVOURITE_LOOKS = (
    ("fire|hearth|flame", "mdi:fireplace", ["#ffb14a", "#ff5a0a", "#7a1400"]),
    ("ember|lava", "mdi:campfire", ["#ff7a1a", "#c81e00", "#2a0000"]),
    ("candle", "mdi:candle", ["#ffd08a", "#ff9a3c", "#5a2600"]),
    ("sunset|dusk", "mdi:weather-sunset", ["#ffb03a", "#e64a6a", "#5a1e8c"]),
    ("aurora|polar", "mdi:aurora", ["#7cff6b", "#00c853", "#0a3d62"]),
    ("ocean|sea|water", "mdi:waves", ["#5ee7ff", "#00a3d4", "#0a2a7a"]),
    ("movie|film", "mdi:movie-open-outline", ["#ff8a3a", "#ff6a1a", "#3a1400"]),
    ("cinema|tv", "mdi:popcorn", ["#6f86ff", "#2848ff", "#06082a"]),
    ("pulse|beat", "mdi:heart-pulse", ["#ff9b5a", "#ff3d6e", "#6a1b9a"]),
    ("spectrum|rainbow", "mdi:equalizer", ["#ff2d55", "#ffd000", "#00e0ff"]),
    ("party|disco|club", "mdi:disco-ball", ["#ff00c8", "#7c4dff", "#00e5ff"]),
    ("bright|day|white", "mdi:white-balance-sunny", ["#ffffff", "#fff1dc", "#ffd6aa"]),
    ("night|sleep", "mdi:weather-night", ["#ff7a2a", "#ff4500", "#2a0a00"]),
    ("hallow|spook", "mdi:halloween", ["#ff7a00", "#ff3c00", "#6a1b9a"]),
    ("christmas|xmas", "mdi:pine-tree", ["#ff2a2a", "#ffffff", "#1fa83a"]),
)


def favourite_look(name):
    import re
    for pattern, icon, colors in FAVOURITE_LOOKS:
        if re.search(pattern, name, re.I):
            return icon, colors
    h = sum(ord(c) * 31 ** i for i, c in enumerate(name)) % 360
    return "mdi:star-four-points-outline", [f"hsl({h} 90% 62%)", f"hsl({(h + 50) % 360} 85% 40%)"]


FAVOURITE_STYLE = """
ha-card { overflow: hidden; isolation: isolate; }
ha-card::before { content: ""; position: absolute; inset: 0; z-index: -1; background: @GRAD@; opacity: .16;
  transition: opacity .45s EASE; pointer-events: none; }
ha-tile-icon { --tile-icon-color: @C0@; }
@media (hover: hover) { ha-card:hover::before { opacity: .3; } }
""".replace("EASE", EASE)
FAVOURITE_ON_STYLE = """
ha-card::before { opacity: .62; }
ha-card { border-color: color-mix(in srgb, @C0@ 70%, transparent) !important;
  box-shadow: 0 0 30px -8px @C0@, 0 10px 30px -12px rgba(0, 0, 0, .35) !important; }
ha-tile-icon { scale: 1.12; }
"""


def strip_section(s):
    light = s["entity"]
    obj = light.split(".", 1)[1]
    sel = s.get("preset", f"select.{obj}_preset")
    name = s.get("name") or friendly(light)
    badges = [badge(sel, "amber", icon="mdi:star-four-points-outline", name=None)] if usable(sel) else []
    cards = [heading(name, s.get("icon", "mdi:led-strip-variant"), badges=badges),
             tile(light, 12, name=name, icon=s.get("icon", "mdi:led-strip-variant"), color="amber",
                  state_content=["state", "brightness", "effect"],
                  features=[{"type": "light-brightness"}] if has_brightness(light) else None)]
    options = STATES.get(sel, {}).get("attributes", {}).get("options") or []
    favs = ents(s.get("favourites")) if s.get("favourites") else [{"entity": o} for o in options]
    for f in favs:
        option = f.get("option") or f.get("entity") or f.get("name")
        if options and option not in options:
            continue
        icon, colors = favourite_look(option)
        colors = f.get("colors") or colors
        grad = f"linear-gradient(135deg, {', '.join(colors)})"
        style = (GLASS + FAVOURITE_STYLE + f"{{% if is_state('{light}', 'on') and is_state('{sel}', {json.dumps(option)}) %}}"
                 + FAVOURITE_ON_STYLE + "{% endif %}").replace("@GRAD@", grad).replace("@C0@", colors[min(1, len(colors) - 1)])
        cards.append({**tile(sel, 4, vertical=True, name=f.get("name", option), icon=f.get("icon", icon), hide_state=True,
                             tap={"action": "perform-action", "perform_action": "select.select_option",
                                  "target": {"entity_id": sel}, "data": {"option": option}}),
                      "_style": "plain", "card_mod": {"style": style}})
    cards += [tile_from(t, 6, color="deep-purple") for t in ents(s.get("toggles")) if usable(t["entity"])]
    if s.get("length") and usable(s["length"]):
        cards.append(tile(s["length"], 12, name="Length", icon="mdi:ruler", color="amber",
                          features=[{"type": "numeric-input", "style": "slider"}]))
    return section(cards)



def floor_view(f, areas_by_floor):
    rooms = [a for a in areas_by_floor[f["id"]] if a in ROOMS]
    floor_lights = [l for a in rooms for l in room_lights(ROOMS[a])]
    # The header already shows the floor name; lead with the tagline instead.
    intro = [{
        "type": "custom:mushroom-title-card",
        "title": f.get("tagline") or f["name"],
        "subtitle": plural_lights(floor_lights) if floor_lights else "",
        **grid(36),
    }]
    sections = [section(intro, span=3)]
    for a in rooms:
        r = ROOMS[a]
        cards = [room_card(a, r, aspect="4/3")]
        lights = [l for l in ents(r.get("lights")) if usable(l["entity"])]
        cards += [tile_from(l, 6 if len(lights) > 1 else 12, color="amber") for l in lights]
        if r.get("vacuum"):
            v = r["vacuum"]
            cards.append(tile(v["entity"], 12, name=v.get("name"), icon="mdi:robot-vacuum", color="teal",
                              state_content=["state", "battery_level"]))
        if r.get("fridge"):
            fr = r["fridge"]
            cards.append(tile(fr["fridge_temp"], 6, name="Fridge", icon="mdi:fridge-outline", color="light-blue"))
            if fr.get("freezer_temp"):
                cards.append(tile(fr["freezer_temp"], 6, name="Freezer", icon="mdi:snowflake", color="blue"))
        sections.append(section(cards))
    return {"title": f["name"], "path": f["path"], "icon": f["icon"], "type": "sections",
            "max_columns": 3, "dense_section_placement": True, "sections": sections}


def hero_background(picture):
    """View background for a room page: the room photo full-bleed and pinned to the viewport,
    fading into the page colour, so cards scroll up over it like frosted glass. Every layer is
    fixed (HA pins the whole background when the value contains ` fixed`), so the fade always
    sits at the same spot on screen."""
    # HA serves area pictures as 512px thumbnails; a full-screen background wants the original.
    # The thumbnail (already cached by the room cards) sits underneath while that loads.
    thumb = picture
    picture = re.sub(r"^(/api/image/serve/[^/]+)/\d+x\d+$", r"\1/original", picture)
    bg = "var(--primary-background-color)"

    # Phones (up to 767px) get a tall, vivid photo; wider screens a shorter, softer one. A view
    # background can't hold a media query, so each value steps between the two with clamp().
    def step(phone, wide):
        return f"clamp({wide}, {phone} + (767px - 100vw) * 100, {phone})"

    # On wide screens ENTER_JS shrinks the background to the 60vh hero band, so the photo is
    # centred in what's visible; phones keep it anchored a little above centre.
    y = "clamp(30%, 30% + (100vw - 767px) * 100, 50%)"

    return ", ".join([
        # top scrim, so the floating header and the room name stay legible on bright photos
        "linear-gradient(to bottom, var(--lumen-hero-scrim, rgba(0,0,0,.38)), rgba(0,0,0,0) 26vh)",
        # a soft shade behind the room name, so it reads on bright photos
        "radial-gradient(70vw 34vh at 0% 22vh, rgba(0,0,0,.28), transparent 70%)",
        # fade into the page colour below the hero
        f"linear-gradient(to bottom, transparent {step('30vh', '16vh')}, "
        f"color-mix(in srgb, {bg} 55%, transparent) {step('50vh', '32vh')}, "
        f"color-mix(in srgb, {bg} 92%, transparent) {step('72vh', '46vh')}, "
        f"{bg} {step('88vh', '60vh')})",
        # wide screens only: a veil of page colour to soften the photo (zero height on phones)
        f"linear-gradient(color-mix(in srgb, {bg} var(--lumen-hero-veil, 30%), transparent) 0 0) 0 0 / 100% "
        f"clamp(0px, (100vw - 767px) * 100, 100vh) no-repeat",
        # a little theme colour washed over the photo, so it belongs to the page
        "radial-gradient(120% 70% at 0% 0%, color-mix(in srgb, var(--primary-color) 22%, transparent), transparent 70%)",
        f"center {y} / cover no-repeat url({json.dumps(picture)})",
        f"center {y} / cover no-repeat url({json.dumps(thumb)}) {bg}",
    ]) + " fixed"


def room_view(area_id, r, floor):
    offline = []

    def split(items):
        items = ents(items)
        offline.extend(e["entity"] for e in items if not usable(e["entity"]))
        return [e for e in items if usable(e["entity"])]

    lights = split(r.get("lights"))
    sections = [section([room_card(area_id, r, cols=36, navigate=False, banner=True)], span=3)]

    # Scenes, then the lights one by one
    if r.get("scenes"):
        sections.append(scenes_section(r))
    if lights:
        cards = [heading("Lighting", "mdi:lightbulb-group")]
        cards += [tile_from(l, 12, color="amber",
                            features=[{"type": "light-brightness"}] if has_brightness(l["entity"]) else None)
                  for l in lights]
        sections.append(section(cards))
    # LED strips: the favourites, one tap away
    sections += [strip_section(st) for st in ents(r.get("strips")) if usable(st["entity"])]

    # Media
    media = split(r.get("media"))
    m = CFG.get("media") or {}
    here = [t for t in tvs(m) if t.get("room") == area_id]
    games = [c for c in consoles(m) if c.get("room") == area_id]
    if here or games or (m and m.get("room") == area_id):
        cards = [heading("Media", "mdi:play-box-multiple", tap=media_path())]
        cards += sum((tv_cards(t, False) for t in here), [])
        for c in games:
            card = game_card(c, 12)
            card["visibility"] = [in_game(c)]
            cards.append(card)
        cards += music_cards(m, 12) if m.get("room") == area_id else []
        cards += [tile_from(sw, 6, color="pink") for sw in split(r.get("sound_switches"))]
        sections.append(section(cards))
    elif media:
        cards = [heading("Media", "mdi:play-box-multiple")]
        for m in media:
            is_tv = STATES[m["entity"]]["attributes"].get("device_class") == "tv"
            feats = [{"type": "media-player-volume-slider"}]
            if not is_tv:
                feats.insert(0, {"type": "media-player-playback", "hide_disabled_controls": True})
            cards.append(tile_from(m, 12, color="pink", features=feats))
        cards += [tile_from(s, 6, color="pink") for s in split(r.get("sound_switches"))]
        sections.append(section(cards))

    # Robot vacuum
    v = r.get("vacuum")
    if v:
        badges = [badge(v["battery"], "green")] if v.get("battery") else []
        if v.get("current_room"):
            badges.append(badge(v["current_room"], "teal", icon="mdi:map-marker"))
        cards = [heading(v.get("name", "Vacuum"), "mdi:robot-vacuum", badges=badges, tap=vacuum_path()),
                 tile(v["entity"], 12, name="Robot vacuum", color="teal", state_content=["state", "fan_speed"],
                      features=[{"type": "vacuum-commands", "commands": ["start_pause", "return_home", "locate"]}]),
                 tile(v["entity"], 12, name="Rooms, map & modes", icon="mdi:floor-plan", color="teal",
                      hide_state=True, tap=nav(vacuum_path()))]
        for key, name, icon, color in (("last_clean", "Last clean", "mdi:broom", "teal"),
                                       ("brush", "Brush", "mdi:brush", "teal"),
                                       ("filter", "Filter", "mdi:air-filter", "teal"),
                                       ("quiet_hours", "Quiet hours", "mdi:sleep", "indigo")):
            if v.get(key):
                cards.append(tile(v[key], 6, name=name, icon=icon, color=color))
        sections.append(section(cards))

    # Fridge
    fr = r.get("fridge")
    if fr:
        cards = [heading("Refrigerator", "mdi:fridge",
                         badges=[badge(d["entity"], name=d.get("name")) for d in ents(fr.get("doors"))]),
                 tile(fr["fridge_temp"], 6, name="Fridge", icon="mdi:fridge-outline", color="light-blue",
                      vertical=True)]
        if fr.get("freezer_temp"):
            cards.append(tile(fr["freezer_temp"], 6, name="Freezer", icon="mdi:snowflake", color="blue",
                              vertical=True))
        cards += [tile_from(s, 6, color="cyan") for s in fr.get("switches", [])]
        if fr.get("filter"):
            cards.append(tile(fr["filter"], 6, name="Water filter", icon="mdi:water-sync", color="cyan"))
        sections.append(section(cards))
        if fr.get("power"):
            sections.append(section(
                [heading("Energy", "mdi:lightning-bolt"),
                 mini_graph([(fr["power"], "Power", "#4bcffa")], name="Fridge power")] +
                ([tile(fr["energy"], 12, name="Total energy", icon="mdi:counter", color="light-blue")]
                 if fr.get("energy") else [])))

    # Camera
    if r.get("camera"):
        if usable(r["camera"]):
            sections.append(section([heading("Camera", "mdi:cctv"),
                                     {"type": "picture-entity", "entity": r["camera"], "camera_view": "live",
                                      "show_state": False, **grid(12)}]))
        else:
            offline.append(r["camera"])

    # Climate & presence
    graphs = split(r.get("graphs"))
    motion = split((r.get("motion") or []) + (r.get("contacts") or []))
    if graphs or motion or r.get("next_alarm"):
        cards = [heading("Climate & presence", "mdi:thermometer-lines")]
        cards += [mini_graph([(g["entity"], g.get("name", ""), g.get("color", "#ff9f43"))], name=g.get("name"))
                  for g in graphs]
        cards += [tile_from(m, 6 if len(motion) > 1 else 12, color="light-green") for m in motion]
        if r.get("next_alarm"):
            cards.append(tile(r["next_alarm"], 12, name="Next alarm", icon="mdi:alarm", color="indigo"))
        sections.append(section(cards))

    # Power (smart plugs)
    pw = split(r.get("power"))
    if pw:
        sections.append(section([heading("Power", "mdi:lightning-bolt"),
                                 mini_graph([(pw[0]["entity"], "Power", "#26de81")], name="Power draw", hours=48)] +
                                [tile_from(e, 12, name=e.get("name", "Total energy"), color="green")
                                 for e in pw[1:]]))

    # Details: automations, batteries, offline — kept quiet at the bottom
    autos = split(r.get("automations"))
    bats = split(r.get("batteries"))
    if autos or bats or offline:
        cards = [heading("Details", "mdi:dots-horizontal-circle-outline", style="subtitle")]
        cards += [tile_from(a, 12, color="purple", state_content=["state", "last_triggered"]) for a in autos]
        cards += [tile_from(b, 6, name=b.get("name") or friendly(b["entity"], " Battery")) for b in bats]
        if offline:
            cards.append(heading("Offline", "mdi:lan-disconnect", style="subtitle"))
            cards += [tile(e, 6, color="disabled") for e in offline]
        sections.append(section(cards))

    if len(sections) == 1:
        sections.append(section([{
            "type": "markdown",
            "content": (f"### Nothing here yet\n{r['name']} has no devices yet. Add some to this room "
                        "in your house config and re-run the generator."),
            **grid(36)}], span=3))

    view = {"title": r["name"], "path": r["path"], "subview": True,
            "back_path": f"/{URL_PATH}/{floor['path']}", "type": "sections", "max_columns": 3,
            "dense_section_placement": True, "sections": sections}
    if r.get("picture"):
        view["background"] = hero_background(r["picture"])
    return view


# --------------------------------------------------------------------------------------
def load_config(path):
    global CFG, FLOORS, ROOMS, URL_PATH, CFG_DIR
    CFG_DIR = os.path.dirname(os.path.abspath(path))
    with open(path) as fh:
        CFG = yaml.safe_load(fh)
    FLOORS = CFG["floors"]
    ROOMS = CFG["rooms"]
    URL_PATH = CFG.get("dashboard", {}).get("url_path", "home-dashboard")
    for area_id, room in ROOMS.items():
        room.setdefault("name", area_id.replace("_", " ").capitalize())
        room.setdefault("path", area_id.replace("_", "-"))


def apply_overlay(views):
    """Merge hand-made Lovelace into the generated views. `overlay:` names a JSON file with
    `views` (whole views, without the header section; they replace a generated view with the
    same path, or are added) and `sections` ({view, index, section}; inserted before the header
    is added, so index 0 is the first section under it)."""
    name = CFG.get("overlay")
    if not name:
        return
    with open(os.path.join(CFG_DIR, os.path.expanduser(name))) as fh:
        overlay = json.load(fh)
    for v in overlay.get("views", []):
        views[:] = [x for x in views if x["path"] != v["path"]] + [v]
    by_path = {v["path"]: v for v in views}
    for item in sorted(overlay.get("sections", []), key=lambda i: (i["view"], i["index"])):
        if item["view"] not in by_path:
            print(f"WARNING: overlay section for unknown view {item['view']!r}")
            continue
        by_path[item["view"]]["sections"].insert(item["index"], item["section"])


def connect():
    ha = CFG.get("home_assistant", {})
    url = os.environ.get("HA_URL", ha.get("url", "http://homeassistant.local:8123"))
    token = os.environ.get("HA_TOKEN")
    if not token:
        with open(os.path.expanduser(ha["token_file"])) as fh:
            token = fh.read().strip()
    HA.update(url=url.rstrip("/"), token=token)
    ws = websocket.create_connection(url.replace("http", "ws", 1).rstrip("/") + "/api/websocket", timeout=30)
    ws.recv()
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    reply = json.loads(ws.recv())
    if reply["type"] != "auth_ok":
        sys.exit(f"Authentication failed: {reply}")
    msg_id = [0]

    def call(type_, **kw):
        msg_id[0] += 1
        ws.send(json.dumps({"id": msg_id[0], "type": type_, **kw}))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == msg_id[0]:
                if not r.get("success"):
                    raise RuntimeError(r)
                return r.get("result")

    return call


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    load_config(args[0] if args else "house.yaml")
    call = connect()

    STATES.update({s["entity_id"]: s for s in call("get_states")})
    areas = call("config/area_registry/list")
    for a in areas:
        if a["area_id"] in ROOMS:
            ROOMS[a["area_id"]]["has_picture"] = bool(a.get("picture"))
            ROOMS[a["area_id"]]["picture"] = a.get("picture")
    # Room order follows the floor assignment in HA; rooms whose area has no floor use their `floor` key.
    areas_by_floor = {f["id"]: [a["area_id"] for a in areas
                                if (ROOMS.get(a["area_id"], {}).get("floor") or a.get("floor_id")) == f["id"]]
                      for f in FLOORS}
    floor_by_id = {f["id"]: f for f in FLOORS}
    haunt(call, areas, "--dry-run" in sys.argv)

    if CFG.get("birdnet"):
        resolve_saezuri(call, CFG["birdnet"])
    views = [home_view(areas_by_floor)]
    views += [floor_view(f, areas_by_floor) for f in FLOORS]
    views += [room_view(a, ROOMS[a], floor_by_id[ROOMS[a]["floor"]])
              for f in FLOORS for a in areas_by_floor[f["id"]] if a in ROOMS]

    if CFG.get("energy", {}).get("insight"):
        views.append(energy_view(CFG["energy"]))
    vac = next((r["vacuum"] for r in ROOMS.values() if r.get("vacuum", {}).get("selection")), None)
    if vac:
        vacuum_maps(call, vac)
        views.append(vacuum_view(vac))
    if CFG.get("media"):
        views.append(media_view(CFG["media"]))
    if CFG.get("birdnet"):
        views.append(birds_view(CFG["birdnet"]))
        room = next((v for v in views if v["path"] == ROOMS.get(CFG["birdnet"].get("room"), {}).get("path")), None)
        if room:
            room["sections"].insert(1, bird_room_section(CFG["birdnet"]))
    apply_overlay(views)

    unknown = sorted(set(ROOMS) - {a["area_id"] for a in areas})
    if unknown:
        print("WARNING: rooms with no matching HA area:", unknown)
    missing = sorted({e for r in ROOMS.values()
                      for k in ("lights", "strips", "media", "motion", "batteries", "automations", "power", "contacts")
                      for e in ids(r.get(k)) if e not in STATES}
                     | {e for k in ("last_bird", "photo", "species_today", "detections_today", "top_bird", "top_species", "recent")
                        for e in [CFG.get("birdnet", {}).get(k)] if e and e not in STATES})
    if missing:
        print("WARNING: entities not found in Home Assistant:", missing)

    for v in views:
        # Own full-width row, so the reserved header space spans every column on desktop.
        v["sections"].insert(0, section([header(v["title"], back=v.get("back_path")),
                                         {**navbar(), **grid(36)}], span=3))
    dash = CFG.get("dashboard", {})
    config = {"title": dash.get("title", "Home"), "views": views}
    if dash.get("kiosk", True):
        # Hide HA's header and sidebar; the floating navbar replaces them. ?disable_km restores them.
        config["kiosk_mode"] = {"kiosk": True}
    apply_style(config)

    out = f"{URL_PATH}.json"
    with open(out, "w") as fh:
        json.dump(config, fh, indent=1, ensure_ascii=False)
    print(f"{len(views)} views generated -> {out}")
    if "--dry-run" in sys.argv:
        return
    if not any(d["url_path"] == URL_PATH for d in call("lovelace/dashboards/list")):
        call("lovelace/dashboards/create", url_path=URL_PATH, title=dash.get("title", "Home"),
             icon=dash.get("icon", "mdi:home-heart"), show_in_sidebar=True, require_admin=False, mode="storage")
        print(f"created dashboard /{URL_PATH}")
    if not any(r["url"] == INTER_FONT for r in call("lovelace/resources")):
        call("lovelace/resources/create", res_type="css", url=INTER_FONT)
    call("lovelace/config/save", url_path=URL_PATH, config=config)
    print(f"saved to /{URL_PATH}")


if __name__ == "__main__":
    main()
