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
    return {"action": "perform-action", "perform_action": "scene.turn_on", "target": {"entity_id": entity}}


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
  background: color-mix(in srgb, var(--card-background-color) 72%, transparent) !important;
  backdrop-filter: blur(18px) saturate(140%);
  -webkit-backdrop-filter: blur(18px) saturate(140%);
  border: 1px solid color-mix(in srgb, var(--primary-text-color) 9%, transparent) !important;
  border-radius: 28px !important;
  box-shadow: 0 10px 30px -12px rgba(0, 0, 0, .35) !important;
  transition: translate .35s EASE, box-shadow .35s EASE, border-color .35s EASE;
}
@media (hover: hover) {
  ha-card:hover {
    translate: 0 -2px;
    border-color: color-mix(in srgb, var(--primary-text-color) 16%, transparent) !important;
    box-shadow: 0 22px 44px -18px rgba(0, 0, 0, .5) !important;
  }
}
@media (prefers-reduced-motion: reduce) { ha-card, ha-card:hover { transition: none; translate: none; } }
""".replace("EASE", EASE)

# Room photo cards: lift on hover while the photo slowly zooms, press in on tap.
PHOTO_BASE = "#15171c"
ROOM_HOVER_STYLE = """
ha-card { transition: translate .4s EASE, scale .2s EASE, box-shadow .4s EASE !important; }
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
ha-card {
  backdrop-filter: none !important; -webkit-backdrop-filter: none !important;
  animation: lumen-hero .7s EASE backwards;
}
ha-card:hover { translate: none !important; }
room-background-image { display: none; }
.sensors, .stats { color: rgba(255, 255, 255, .88) !important; text-shadow: 0 1px 10px rgba(0, 0, 0, .5); }
@keyframes lumen-hero { from { opacity: 0; translate: 0 14px; } }
@media (max-width: 767px) { ha-card { --user-grid-aspect-ratio: 5/4 !important; aspect-ratio: 5/4 !important; } }
@media (prefers-reduced-motion: reduce) { ha-card { animation: none; } }
""".replace("EASE", EASE)
GLASS_TYPES = {"tile", "weather-forecast", "custom:mini-graph-card", "markdown", "custom:mushroom-template-card"}
TITLE_STYLE = """
.title { font-size: 30px !important; font-weight: 700 !important; letter-spacing: -.02em; }
.subtitle { font-size: 15px !important; opacity: .7; }
"""
HEADING_STYLE = """
ha-card { --ha-heading-card-title-font-size: 17px; --ha-heading-card-title-font-weight: 650; }
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
  .button:not(.active):hover { background: color-mix(in srgb, var(--primary-text-color) 7%, transparent); }
  .button:not(.active):hover .icon, .button:not(.active):hover .image { translate: 0 -2px; }
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
  border: 1px solid color-mix(in srgb, var(--primary-text-color) 9%, transparent) !important;
  box-shadow: 0 18px 40px -16px rgba(0, 0, 0, .45) !important;
}
"""

HEADER_STYLES = """
:host { display: block; height: 64px; }            /* reserve room at the top of the page */
.navbar, .navbar.desktop.top {
  top: calc(env(safe-area-inset-top, 0px) + 10px) !important;
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
  border: 1px solid color-mix(in srgb, var(--primary-text-color) 9%, transparent) !important;
  box-shadow: 0 12px 30px -16px rgba(0, 0, 0, .45) !important;
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


def navbar():
    def selected(paths):
        return "[[[ return " + json.dumps(paths) + ".includes(window.location.pathname) ]]]"

    # Tab index per page (rooms map to their floor's tab), used by the sliding indicator.
    tabs = {f"/{URL_PATH}/home": 0}
    for i, f in enumerate(FLOORS, start=1):
        tabs[f"/{URL_PATH}/{f['path']}"] = i
        tabs.update({f"/{URL_PATH}/{r['path']}": i for r in ROOMS.values() if r["floor"] == f["id"]})
    haunted = [a for a, r in ROOMS.items() if r.get("haunt")]
    haunt_js = HAUNT_JS % (json.dumps(haunted), float(CFG["haunted"].get("every", 100)),
                           float(CFG["haunted"].get("duration", 1))) if haunted else ""
    home_selected = ("[[[ " + NAV_MOTION_JS % json.dumps(tabs) + haunt_js +
                     f"return window.location.pathname === {json.dumps(f'/{URL_PATH}/home')}; ]]]")
    routes = [{"url": f"/{URL_PATH}/home", "icon": "mdi:home-outline", "icon_selected": "mdi:home",
               "label": "Home", "selected": home_selected}]
    for f in FLOORS:
        rooms = [r["path"] for r in ROOMS.values() if r["floor"] == f["id"]]
        routes.append({
            "url": f"/{URL_PATH}/{f['path']}", "icon": f["icon"], "label": f.get("short", f["name"]),
            "selected": selected([f"/{URL_PATH}/{p}" for p in [f["path"]] + rooms]),
        })
    routes.append({
        "icon": "mdi:dots-horizontal", "label": "More", "tap_action": {"action": "open-popup"},
        "popup": [
            full_load("mdi:pencil-outline", "Edit dashboard", f"/{URL_PATH}/home?disable_km&edit=1"),
            {"icon": "mdi:lightning-bolt-outline", "label": "Energy", "url": "/energy"},
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
        t = node.get("type")
        if t in GLASS_TYPES:
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
def home_view(areas_by_floor):
    user = CFG.get("user_name")
    greeting = ("{% set h = now().hour %}"
                "{{ 'Good night' if h < 6 else 'Good morning' if h < 12 else "
                "'Good afternoon' if h < 18 else 'Good evening' }}" + (f", {user}" if user else ""))
    welcome = [{
        "type": "custom:mushroom-title-card",
        "title": greeting,
        "subtitle": "{{ now().strftime('%A %-d %B') }} · " + plural_lights(all_lights()),
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
        elif action == "todo":
            kw["tap"] = nav(f"/todo?entity_id={q['entity']}")
        elif isinstance(action, dict):
            kw["tap"] = action
        quick.append(tile(q["entity"], **kw))
    if quick:
        sections.append(section([heading("Quick settings", "mdi:tune-variant")] + quick))

    energy = CFG.get("energy")
    if energy:
        cards = [heading("Energy", "mdi:lightning-bolt",
                         badges=[badge(b["entity"], b.get("color"), icon=b.get("icon"))
                                 for b in ents(energy.get("badges"))])]
        if energy.get("graph"):
            cards.append(mini_graph(
                [(energy["graph"], "Power", "#f7b731")], name="Power today", hours=24, height=140,
                show={"fill": "fade", "labels": False, "legend": False, "extrema": True, "icon": False},
                color_thresholds=[{"value": 0, "color": "#20bf6b"}, {"value": 0.8, "color": "#f7b731"},
                                  {"value": 2, "color": "#eb3b5a"}]))
        cards += [tile_from(t, 6) for t in energy.get("tiles", [])]
        sections.append(section(cards))

    for f in FLOORS:
        rooms = [a for a in areas_by_floor[f["id"]] if a in ROOMS]
        cards = [heading(f["name"], f["icon"], tap=f"/{URL_PATH}/{f['path']}")]
        cards += [room_card(a, ROOMS[a], cols=6) for a in rooms]
        sections.append(section(cards, span=3))

    return {"title": "Home", "path": "home", "icon": "mdi:home-heart", "type": "sections",
            "max_columns": 3, "dense_section_placement": True, "sections": sections}


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
    return ", ".join([
        # top scrim, so the floating header and the room name stay legible on bright photos
        "linear-gradient(to bottom, rgba(0,0,0,.38), rgba(0,0,0,0) 26vh)",
        # fade into the page colour below the hero
        f"linear-gradient(to bottom, transparent 30vh, color-mix(in srgb, {bg} 55%, transparent) 50vh, "
        f"color-mix(in srgb, {bg} 92%, transparent) 72vh, {bg} 88vh)",
        # a little theme colour washed over the photo, so it belongs to the page
        "radial-gradient(120% 70% at 0% 0%, color-mix(in srgb, var(--primary-color) 22%, transparent), transparent 70%)",
        f"center 30% / cover no-repeat url({json.dumps(picture)})",
        f"center 30% / cover no-repeat url({json.dumps(thumb)}) {bg}",
    ]) + " fixed"


def room_view(area_id, r, floor):
    offline = []

    def split(items):
        items = ents(items)
        offline.extend(e["entity"] for e in items if not usable(e["entity"]))
        return [e for e in items if usable(e["entity"])]

    lights = split(r.get("lights"))
    sections = [section([room_card(area_id, r, cols=36, navigate=False, banner=True)], span=3)]

    # Lighting
    scenes = ents(r.get("scenes"))
    if lights or scenes:
        cards = [heading("Lighting", "mdi:lightbulb-group")]
        cards += [tile(s["entity"], 12, name=s.get("name"), icon=s.get("icon", "mdi:weather-night"),
                       color="deep-purple", hide_state=True, tap=scene_tap(s["entity"])) for s in scenes]
        cards += [tile_from(l, 12, color="amber",
                            features=[{"type": "light-brightness"}] if has_brightness(l["entity"]) else None)
                  for l in lights]
        sections.append(section(cards))

    # Media
    media = split(r.get("media"))
    if media:
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
        cards = [heading(v.get("name", "Vacuum"), "mdi:robot-vacuum", badges=badges),
                 tile(v["entity"], 12, name="Robot vacuum", color="teal", state_content=["state", "fan_speed"],
                      features=[{"type": "vacuum-commands", "commands": ["start_pause", "return_home", "locate"]}])]
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
    global CFG, FLOORS, ROOMS, URL_PATH
    with open(path) as fh:
        CFG = yaml.safe_load(fh)
    FLOORS = CFG["floors"]
    ROOMS = CFG["rooms"]
    URL_PATH = CFG.get("dashboard", {}).get("url_path", "home-dashboard")
    for area_id, room in ROOMS.items():
        room.setdefault("name", area_id.replace("_", " ").capitalize())
        room.setdefault("path", area_id.replace("_", "-"))


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

    views = [home_view(areas_by_floor)]
    views += [floor_view(f, areas_by_floor) for f in FLOORS]
    views += [room_view(a, ROOMS[a], floor_by_id[ROOMS[a]["floor"]])
              for f in FLOORS for a in areas_by_floor[f["id"]] if a in ROOMS]

    unknown = sorted(set(ROOMS) - {a["area_id"] for a in areas})
    if unknown:
        print("WARNING: rooms with no matching HA area:", unknown)
    missing = sorted({e for r in ROOMS.values()
                      for k in ("lights", "media", "motion", "batteries", "automations", "power", "contacts")
                      for e in ids(r.get(k)) if e not in STATES})
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
