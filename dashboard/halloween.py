"""Halloween for Lumen: spooky scenes per room and for the whole house, music-and-light shows, a page to run
it all from, and a few easter eggs. Everything only shows around Halloween.

Driven by the `halloween:` section of house.yaml (see house.example.yaml). Two halves:

- Home Assistant: `package()` describes a package (halloween.yaml, written next to the dashboard JSON) with the
  season sensor, the scenes, the scripts that switch rooms to Halloween and back, the shows and the pranks.
  Copy it to /config/packages/ and reload. Deleting that one file removes all of it again.
- The dashboard: a Halloween page and tab, a Halloween row on each room page that has scenes, a banner on the
  home page, and the easter eggs. All of it is gated on binary_sensor.halloween_season, so it hides itself
  outside the season (and when the package is gone).

Going back is always one tap: the first Halloween scene in a room snapshots its lights, and "Back to normal"
puts the snapshot back. Lights that end up off get their Halloween colour washed out (a blink at the lowest
brightness in warm white), so they don't wake up blood red the next morning. Without a snapshot (Home
Assistant restarted in between) lit lights go back to warm white.
"""
import colorsys
import io
import json
import math
import random
import struct
import unicodedata
import urllib.parse
import wave

L = None          # the generator module (build_dashboard), for its card helpers and config
H = {}            # the halloween: section, with defaults filled in

SEASON = "binary_sensor.halloween_season"
ACTIVE = "binary_sensor.halloween_lights"
TOUCHED = "input_text.halloween_rooms"
PAUSED = "input_text.halloween_paused"
PENDING = "input_text.halloween_wake_up"
PRANKS = "input_boolean.halloween_pranks"
WARM = 2700

ORANGE, PURPLE, RED, BLOOD, GREEN, GHOST, BONE, CANDLE, BLUE = (
    [255, 90, 0], [130, 0, 255], [255, 0, 0], [120, 0, 8], [70, 255, 20], [150, 190, 255], [235, 235, 255],
    [255, 120, 30], [20, 40, 255])

# The looks a room can take. Colour lights take `color` in turn ([rgb, brightness %]), white-spectrum lights
# `ct` ([kelvin, %]), plain dimmable lights `dim` (%), on/off lights `onoff`, and WLED strips `strip`
# (effect, colour, palette, %). None = off.
LOOKS = {
    "pumpkin": {"name": "Pumpkin patch", "icon": "mdi:halloween", "color": "orange",
                "lights": {"color": [[ORANGE, 75], [PURPLE, 45], [[255, 140, 0], 60]], "ct": [2000, 35],
                           "dim": 30, "onoff": True, "strip": ["Fire 2012", ORANGE, "Fire", 100]}},
    "witches_brew": {"name": "Witch's brew", "icon": "mdi:pot-mix-outline", "color": "light-green",
                     "lights": {"color": [[GREEN, 55], [PURPLE, 50]], "ct": [6500, 12], "dim": 12, "onoff": False,
                                "strip": ["Drip", GREEN, "* Color 1", 100]}},
    "blood_moon": {"name": "Blood moon", "icon": "mdi:moon-waning-crescent", "color": "red",
                   "lights": {"color": [[RED, 50], [BLOOD, 30]], "ct": [2000, 8], "dim": 8, "onoff": False,
                              "strip": ["Breathe", RED, "* Color 1", 70]}},
    "ghost_light": {"name": "Ghost light", "icon": "mdi:ghost-outline", "color": "light-blue",
                    "lights": {"color": [[GHOST, 25], [[200, 225, 255], 12]], "ct": [6500, 10], "dim": 10,
                               "onoff": False, "strip": ["Ghost Rider", GHOST, "* Color 1", 80]}},
    "seance": {"name": "Séance", "icon": "mdi:crystal-ball", "color": "purple",
               "lights": {"color": [[CANDLE, 22], [[100, 0, 200], 15]], "ct": [1800, 12], "dim": 10, "onoff": False,
                          "strip": ["Candle Multi", CANDLE, "* Color 1", 70]}},
    "eyes": {"name": "Watching eyes", "icon": "mdi:eye-outline", "color": "deep-orange",
             "lights": {"color": None, "ct": None, "dim": None, "onoff": False,
                        "strip": ["Halloween Eyes", RED, "* Color 1", 100]}},
}
# The whole-house scene: every room with lights, colour where there is colour, low light elsewhere.
HOUSE_LOOK = {"name": "Haunted house", "icon": "mdi:home-alert-outline", "color": "deep-orange",
              "lights": {"color": [[ORANGE, 70], [PURPLE, 45]], "ct": [2000, 25], "dim": 20, "onoff": True,
                         "strip": ["Fire 2012", ORANGE, "Fire", 100]}}
# Which looks a room gets by default: everything in the room the shows run in, three for other colour rooms,
# two for rooms with white-spectrum lights only, none for plain lamps (they still join the whole-house scene).
LOOKS_FOCUS = ["pumpkin", "witches_brew", "blood_moon", "ghost_light", "seance", "eyes"]
LOOKS_COLOR = ["pumpkin", "blood_moon", "ghost_light"]
LOOKS_CT = ["ghost_light", "seance"]

# The shows: a song (Music Assistant: name + artist) or a sound file, with a light programme in the shows' room.
SHOWS = [
    {"key": "skeletons", "name": "Spooky Scary Skeletons", "icon": "mdi:skull-outline", "color": "white",
     "song": {"name": "Spooky, Scary Skeletons", "artist": "Andrew Gold"}, "program": "skeletons", "minutes": 4},
    {"key": "this_is_halloween", "name": "This Is Halloween", "icon": "mdi:halloween", "color": "orange",
     "song": {"name": "This Is Halloween", "artist": "Danny Elfman"}, "program": "pumpkins", "minutes": 5},
    {"key": "danse_macabre", "name": "Danse Macabre", "icon": "mdi:violin", "color": "purple",
     "song": {"name": "Danse macabre", "artist": "Camille Saint-Saëns"}, "program": "danse_macabre", "minutes": 9},
    {"key": "thriller", "name": "Thriller", "icon": "mdi:account-cowboy-hat-outline", "color": "light-green",
     "song": {"name": "Thriller", "artist": "Michael Jackson"}, "program": "thriller", "minutes": 7},
    {"key": "the_shape", "name": "Halloween (1978)", "icon": "mdi:knife", "color": "deep-orange",
     "song": {"name": "Halloween Theme - Main Title", "artist": "John Carpenter"}, "program": "the_shape",
     "minutes": 4},
    {"key": "thunderstorm", "name": "Thunderstorm", "icon": "mdi:weather-lightning-rainy", "color": "blue",
     "sound": "thunder", "program": "storm", "minutes": 2},
]

# The thunderstorm: (seconds in, strength 0-1, seconds until the thunder). The sound file is made from the same
# list (thunder_wav), so the flashes and the rumbles line up.
STORM = [(4.0, 0.6, 2.6), (11.5, 1.0, 0.5), (19.0, 0.4, 3.4), (24.5, 0.8, 1.2), (31.0, 1.0, 0.3),
         (32.4, 0.7, 0.6), (41.0, 0.5, 2.2), (48.5, 0.9, 0.9), (58.0, 0.35, 3.8), (66.0, 1.0, 0.4)]
STORM_SECONDS = 80


def slugify(text):
    """Home Assistant's entity id slug of a name."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    out, last = [], "_"
    for ch in text:
        if ch.isalnum():
            out.append(ch)
            last = ch
        elif last != "_":
            out.append("_")
            last = "_"
    return "".join(out).strip("_")


def enabled():
    return bool(H)


def setup(lumen):
    """Fill in H from house.yaml's halloween: section (empty when there is none)."""
    global L
    L = lumen
    cfg = L.CFG.get("halloween")
    H.clear()
    if not cfg:
        return
    H.update(cfg if isinstance(cfg, dict) else {})
    H.setdefault("season", {"from": "10-17", "until": "11-02"})
    H.setdefault("room", next(iter(L.ROOMS)))
    H.setdefault("exclude", [])
    H.setdefault("shows", SHOWS)
    H.setdefault("sounds", {})


# --------------------------------------------------------------------------------------
# The house as Halloween sees it: per room its lights by what they can do, and its strips
# --------------------------------------------------------------------------------------
def kind(entity):
    modes = set(L.STATES.get(entity, {}).get("attributes", {}).get("supported_color_modes") or [])
    if modes & {"hs", "rgb", "rgbw", "rgbww"} or (modes == {"xy"}):
        return "color"
    if "color_temp" in modes:
        return "ct"  # white spectrum (IKEA's WS bulbs also report xy, for colour temperature only)
    if modes & {"brightness", "white"}:
        return "dim"
    return "onoff"


def strips(room):
    out = []
    for s in L.ents(room.get("strips")):
        light = s["entity"]
        if not L.usable(light):
            continue
        obj = light.split(".", 1)[1]
        preset = s.get("preset", f"select.{obj}_preset")
        extra = [e for e in (f"select.{obj}_color_palette", f"number.{obj}_speed", f"number.{obj}_intensity")
                 if e in L.STATES]
        options = L.STATES.get(preset, {}).get("attributes", {}).get("options") or []
        normal = H.get("rooms", {}).get(room.get("_area"), {}).get("strip_normal") or next(
            (o for o in [L.ent(f).get("option") or L.ent(f)["entity"] for f in s.get("favourites") or []] + options
             if o in options and not is_spooky(o) and not o.lower().startswith("music")), None)
        out.append({"light": light, "preset": preset, "extra": extra, "normal": normal,
                    "palette": next((e for e in extra if e.startswith("select.")), None)})
    return out


def lights(room):
    """The room's lights Halloween drives (not its strips, not the excluded, not the unavailable), by kind."""
    skip = set(H["exclude"]) | {s["entity"] for s in L.ents(room.get("strips"))}
    out = {"color": [], "ct": [], "dim": [], "onoff": []}
    for l in L.room_lights(room):
        if l not in skip and L.usable(l):
            out[kind(l)].append(l)
    return out


def rooms():
    """{area: {name, path, lights by kind, strips, looks}} for every room Halloween can light."""
    out = {}
    for area, r in L.ROOMS.items():
        r["_area"] = area
        lk, st = lights(r), strips(r)
        if not (sum(lk.values(), []) or st):
            continue
        conf = H.get("rooms", {}).get(area, {})
        if "looks" in conf:
            looks = conf["looks"]
        elif area == H["room"]:
            looks = LOOKS_FOCUS
        elif lk["color"] or st:
            looks = LOOKS_COLOR
        elif lk["ct"]:
            looks = LOOKS_CT
        else:
            looks = []
        out[area] = {"name": r["name"], "path": r["path"], "lights": lk, "strips": st, "looks": looks}
    return out


def is_spooky(name):
    return any(w in name.lower() for w in ("hallow", "spook", "ghost", "haunt"))


def scene_name(room, look):
    return f"Halloween · {room['name']} · {look['name']}"


def scene_entity(room, look):
    return "scene." + slugify(scene_name(room, look))


HOUSE_SCENE = "scene." + slugify("Halloween · " + HOUSE_LOOK["name"])


# --------------------------------------------------------------------------------------
# Home Assistant: the package
# --------------------------------------------------------------------------------------
def hs(rgb):
    h, s, _ = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    return [round(h * 360, 1), round(s * 100, 1)]


def bri(pct):
    return max(1, round(pct * 2.55))


def light_state(entity, k, look):
    spec = look["lights"]
    if k == "color":
        if not spec["color"]:
            return {"state": "off"}
        i = light_state.counter = getattr(light_state, "counter", -1) + 1
        rgb, pct = spec["color"][i % len(spec["color"])]
        modes = L.STATES[entity]["attributes"].get("supported_color_modes") or []
        if "hs" in modes:
            return {"state": "on", "color_mode": "hs", "hs_color": hs(rgb), "brightness": bri(pct)}
        return {"state": "on", "rgb_color": rgb, "brightness": bri(pct)}
    if k == "ct":
        return {"state": "off"} if not spec["ct"] else {
            "state": "on", "color_mode": "color_temp", "color_temp_kelvin": spec["ct"][0], "brightness": bri(spec["ct"][1])}
    if k == "dim":
        return {"state": "off"} if not spec["dim"] else {"state": "on", "brightness": bri(spec["dim"])}
    return {"state": "on" if spec["onoff"] else "off"}


def strip_states(s, look):
    effect, rgb, palette, pct = look["lights"]["strip"]
    out = {s["light"]: {"state": "on", "effect": effect, "rgb_color": rgb, "brightness": bri(pct)}}
    if s["palette"]:
        out[s["palette"]] = {"state": palette}
    return out


def scene(name, icon, look, room_list):
    light_state.counter = -1
    entities = {}
    for r in room_list:
        for k, ls in r["lights"].items():
            if k == "onoff" and look is not HOUSE_LOOK:
                continue  # plain on/off lamps only join the whole-house scene
            for l in ls:
                entities[l] = light_state(l, k, look)
        for s in r["strips"]:
            entities.update(strip_states(s, look))
    return {"id": "lumen_" + slugify(name), "name": name, "icon": icon, "entities": entities}


def room_map(rs):
    """What the scripts need to know per room: its lights, and per strip its favourite select and settings."""
    return {a: {"lights": sum(r["lights"].values(), []),
                "strips": [{"light": s["light"], "preset": s["preset"], "extra": s["extra"],
                            "normal": s["normal"]} for s in r["strips"]]}
            for a, r in rs.items()}


def all_lights(rmap):
    return sorted({l for r in rmap.values() for l in r["lights"]})


def act(action, target=None, **data):
    a = {"action": action}
    if target is not None:
        a["target"] = {"entity_id": target}
    if data:
        a["data"] = data
    a["continue_on_error"] = True
    return a


def wait(seconds):
    return {"delay": {"milliseconds": int(seconds * 1000)}}


def listvar(entity):
    return ("{% set c = states('" + entity + "') %}"
            "{{ [] if c in ['unknown', 'unavailable', ''] else c.split(',') }}")


SNAP = "scene.halloween_before_{{ slot }}"
HAS_SNAP = "{{ ('scene.halloween_before_' ~ %s) in states.scene | map(attribute='entity_id') | list }}"


def scripts(rs):
    rmap = room_map(rs)
    shows = [s for s in H["shows"] if s.get("song") or H["sounds"].get(s.get("sound"))]
    show_ids = [f"script.halloween_show_{s['key']}" for s in shows]
    focus = H["room"]
    speaker = H.get("speaker")
    pause = H.get("pause_automations") or []
    out = {}

    out["halloween_save"] = {
        "alias": "Halloween · remember the lights", "icon": "mdi:content-save-outline", "mode": "queued",
        "description": "Snapshot the rooms' lights as scene.halloween_before_<slot>, so Back to normal can put "
                       "them back. A strip is remembered by its favourite when it's on one.",
        "fields": {"slot": {"selector": {"text": None}}, "rooms": {"selector": {"object": None}}},
        "variables": {"rmap": rmap, "entities": (
            "{% set ns = namespace(e=[]) %}{% for r in rooms if r in rmap %}"
            "{% set ns.e = ns.e + rmap[r].lights %}"
            "{% for s in rmap[r].strips %}"
            "{% if is_state(s.light, 'off') %}{% set ns.e = ns.e + [s.light] %}"
            "{% elif states(s.preset) in (state_attr(s.preset, 'options') or []) %}{% set ns.e = ns.e + [s.preset] %}"
            "{% else %}{% set ns.e = ns.e + [s.light] + s.extra %}{% endif %}"
            "{% endfor %}{% endfor %}"
            "{{ ns.e | unique | reject('is_state', ['unavailable', 'unknown']) | list }}")},
        "sequence": [{"if": "{{ entities | count > 0 }}", "then": [
            {"action": "scene.create", "data": {"scene_id": "halloween_before_{{ slot }}",
                                                "snapshot_entities": "{{ entities }}"}}]}],
    }

    out["halloween_restore"] = {
        "alias": "Halloween · put the lights back", "icon": "mdi:restore", "mode": "queued",
        "description": "Put the rooms back as they were before Halloween (their snapshot), or warm white without "
                       "one. With `forget`, only wash out the lights that are off (for the night).",
        "fields": {"slot": {"selector": {"text": None}}, "rooms": {"selector": {"object": None}},
                   "forget": {"default": False, "selector": {"boolean": None}}},
        "variables": {
            "rmap": rmap, "snap": SNAP, "has": HAS_SNAP % "slot",
            "saved": "{{ state_attr(snap, 'entity_id') or [] }}",
            "lights": "{% set ns = namespace(l=[]) %}{% for r in rooms if r in rmap %}"
                      "{% set ns.l = ns.l + rmap[r].lights %}{% endfor %}{{ ns.l }}",
            "strips": "{% set ns = namespace(s=[]) %}{% for r in rooms if r in rmap %}"
                      "{% set ns.s = ns.s + rmap[r].strips %}{% endfor %}{{ ns.s }}",
        },
        "sequence": [
            {"if": "{{ not (forget | default(false)) }}", "then": [{"choose": [
                {"conditions": "{{ has }}", "sequence": [
                    # A strip that was off goes back to its everyday favourite before it goes off, so it
                    # doesn't come back on in Halloween colours.
                    {"repeat": {"for_each": "{{ strips }}", "sequence": [
                        {"if": "{{ repeat.item.light in saved and is_state(repeat.item.light, 'on') "
                               "and repeat.item.normal }}",
                         "then": [act("select.select_option", "{{ repeat.item.preset }}",
                                      option="{{ repeat.item.normal }}"), wait(1.2)]}]}},
                    act("scene.turn_on", "{{ snap }}", transition=2), wait(2.5),
                ]}],
                "default": [
                    {"repeat": {"for_each": "{{ lights | select('is_state', 'on') | list }}", "sequence": [
                        {"if": "{{ 'color_temp' in (state_attr(repeat.item, 'supported_color_modes') or []) }}",
                         "then": [act("light.turn_on", "{{ repeat.item }}", color_temp_kelvin=WARM,
                                      transition=2)]}]}},
                    {"repeat": {"for_each": "{{ strips }}", "sequence": [
                        {"if": "{{ is_state(repeat.item.light, 'on') and repeat.item.normal }}",
                         "then": [act("select.select_option", "{{ repeat.item.preset }}",
                                      option="{{ repeat.item.normal }}")]}]}},
                    wait(2.5)]}]},
            # Lights that are off now forget their Halloween colour: on at the lowest brightness in warm white
            # for a moment, and off again. They'd come back at that brightness, so they're marked (by number in
            # `all`) for halloween_wake_up to give the first plain switch-on a proper brightness.
            {"variables": {"dark": "{% set ns = namespace(l=[]) %}{% for l in lights if is_state(l, 'off') "
                                   "and 'color_temp' in (state_attr(l, 'supported_color_modes') or []) %}"
                                   "{% set ns.l = ns.l + [l] %}{% endfor %}{{ ns.l }}",
                           "all": all_lights(rmap), "pending": listvar(PENDING)}},
            {"if": "{{ dark | count > 0 }}", "then": [
                act("light.turn_on", "{{ dark }}", color_temp_kelvin=WARM, brightness=1, transition=0),
                wait(0.8), act("light.turn_off", "{{ dark }}", transition=0),
                act("input_text.set_value", PENDING, value=(
                    "{% set ns = namespace(p=pending) %}{% for l in all %}{% if l in dark %}"
                    "{% set ns.p = ns.p + [loop.index0 | string] %}{% endif %}{% endfor %}"
                    "{{ ns.p | unique | join(',') }}"))]},
            {"if": "{{ has and (not (forget | default(false)) or lights | select('is_state', 'on') | list "
                   "| count == 0) }}",
             "then": [act("scene.delete", "{{ snap }}")]},
        ],
    }

    out["halloween_scene"] = {
        "alias": "Halloween · scene", "icon": "mdi:halloween", "mode": "queued",
        "description": "Switch to a Halloween scene, remembering how the rooms were the first time.",
        "fields": {"scene": {"selector": {"entity": {"domain": "scene"}}}, "rooms": {"selector": {"object": None}}},
        "variables": {"touched": listvar(TOUCHED)},
        "sequence": [
            {"repeat": {"for_each": "{{ rooms }}", "sequence": [
                {"if": "{{ not (" + (HAS_SNAP % "repeat.item")[3:-3] + ") }}", "then": [
                    {"action": "script.halloween_save", "data": {"slot": "{{ repeat.item }}",
                                                                 "rooms": "{{ [repeat.item] }}"}}]}]}},
            act("input_text.set_value", TOUCHED, value="{{ (touched + rooms) | unique | join(',') }}"),
            act("scene.turn_on", "{{ scene }}", transition=2),
        ],
    }

    out["halloween_normal"] = {
        "alias": "Halloween · back to normal", "icon": "mdi:lightbulb-on-outline", "mode": "queued",
        "description": "Stop any show and put the rooms back as they were before Halloween (default: every room "
                       "that went Halloween; with nothing remembered, every Halloween room goes warm white).",
        "fields": {"rooms": {"selector": {"object": None}}, "forget": {"default": False, "selector": {"boolean": None}}},
        "variables": {"touched": listvar(TOUCHED), "all": list(rmap),
                      "todo": "{{ rooms if rooms is defined and rooms else (touched if touched else all) }}"},
        "sequence": [
            {"if": "{{ not (forget | default(false)) }}", "then": [{"action": "script.halloween_show_stop"}]},
            {"repeat": {"for_each": "{{ todo }}", "sequence": [
                {"action": "script.halloween_restore", "data": {
                    "slot": "{{ repeat.item }}", "rooms": "{{ [repeat.item] }}",
                    "forget": "{{ forget | default(false) }}"}}]}},
            act("input_text.set_value", TOUCHED, value=(
                "{% set ns = namespace(k=[]) %}{% for r in touched %}"
                "{% if r not in todo or (forget | default(false) and ('scene.halloween_before_' ~ r) in "
                "states.scene | map(attribute='entity_id') | list) %}{% set ns.k = ns.k + [r] %}{% endif %}"
                "{% endfor %}{{ ns.k | join(',') }}")),
        ],
    }

    # Shows
    out["halloween_show_begin"] = {
        "alias": "Halloween · show begins", "icon": "mdi:play-outline", "mode": "queued",
        "description": "Stop any other show, remember the lights, and pause the automations that would get in "
                       "the way (the strip following the music, say).",
        "fields": {"show": {"selector": {"entity": {"domain": "script"}}}},
        "variables": {"shows": show_ids, "pause": pause, "paused": listvar(PAUSED)},
        "sequence": [
            {"variables": {"others": "{{ shows | reject('eq', show) | select('is_state', 'on') | list }}"}},
            {"if": "{{ others | count > 0 }}", "then": [act("script.turn_off", "{{ others }}"), wait(0.5)]},
            {"if": "{{ not (" + (HAS_SNAP % "'show'")[3:-3] + ") }}", "then": [
                {"action": "script.halloween_save", "data": {"slot": "show", "rooms": [focus]}}]},
            {"variables": {"to_pause": "{{ pause | select('is_state', 'on') | list }}"}},
            {"if": "{{ to_pause | count > 0 }}", "then": [
                {"action": "automation.turn_off", "target": {"entity_id": "{{ to_pause }}"},
                 "data": {"stop_actions": True}},
                act("input_text.set_value", PAUSED, value="{{ (paused + to_pause) | unique | join(',') }}")]},
        ],
    }
    out["halloween_show_end"] = {
        "alias": "Halloween · show ends", "icon": "mdi:stop-circle-outline", "mode": "queued",
        "description": "Put the lights back as they were before the show and switch the paused automations back on.",
        "variables": {"paused": listvar(PAUSED)},
        "sequence": [
            {"action": "script.halloween_restore", "data": {"slot": "show", "rooms": [focus]}},
            {"if": "{{ paused | count > 0 }}", "then": [act("automation.turn_on", "{{ paused }}")]},
            act("input_text.set_value", PAUSED, value=""),
        ],
    }
    out["halloween_show_stop"] = {
        "alias": "Halloween · stop the show", "icon": "mdi:stop", "mode": "queued",
        "description": "Stop whatever show is running, the music too, and put the lights back.",
        "variables": {"shows": show_ids + ["script.halloween_boo"], "paused": listvar(PAUSED)},
        "sequence": [
            {"variables": {"running": "{{ shows | select('is_state', 'on') | list }}"}},
            {"if": "{{ running | count > 0 }}", "then": [
                act("script.turn_off", "{{ running }}"),
                *([act("media_player.media_stop", speaker)] if speaker else []), wait(0.6)]},
            {"if": "{{ running | count > 0 or paused | count > 0 or "
                   + (HAS_SNAP % "'show'")[3:-3] + " }}",
             "then": [{"action": "script.halloween_show_end"}]},
        ],
    }
    out["halloween_show"] = {
        "alias": "Halloween · start or stop a show", "icon": "mdi:play-pause", "mode": "parallel",
        "fields": {"show": {"selector": {"entity": {"domain": "script"}}}},
        "sequence": [{"if": "{{ is_state(show, 'on') }}",
                      "then": [{"action": "script.halloween_show_stop"}],
                      "else": [act("script.turn_on", "{{ show }}")]}],
    }
    for s in shows:
        out[f"halloween_show_{s['key']}"] = show_script(s, rs[focus], speaker)
    if speaker:
        out["halloween_boo"] = boo_script(rs[focus], speaker)
    return out


# --- light programmes ---------------------------------------------------------------
def on(ents, rgb=None, pct=None, kelvin=None, t=None):
    if not ents:
        return []
    d = {}
    if rgb is not None:
        d["rgb_color"] = rgb
    if kelvin is not None:
        d["color_temp_kelvin"] = kelvin
    if pct is not None:
        d["brightness_pct"] = pct
    if t is not None:
        d["transition"] = t
    return [act("light.turn_on", ents, **d)]


def off(ents, t=None):
    if not ents:
        return []
    return [act("light.turn_off", ents, **({"transition": t} if t is not None else {}))]


def strip(room, effect, rgb, palette=None, pct=None, speed=None, intensity=None):
    out = []
    for s in room["strips"]:
        out += on([s["light"]], rgb, pct)
        out[-1]["data"]["effect"] = effect
        if palette and s["palette"]:
            out.append(act("select.select_option", s["palette"], option=palette))
        for name, v in (("speed", speed), ("intensity", intensity)):
            e = next((x for x in s["extra"] if x.endswith("_" + name)), None)
            if v is not None and e:
                out.append(act("number.set_value", e, value=v))
    return out


def groups(room):
    c = room["lights"]["color"]
    return c[0::2], c[1::2], room["lights"]["ct"] + room["lights"]["dim"]


def program(name, room):
    """(intro, loop): the actions that set the stage, and one round of the light show (repeated while the song
    plays). The rounds stay at a light or two per half second, which Zigbee bulbs keep up with."""
    a, b, w = groups(room)
    every = a + b
    if name == "skeletons":
        intro = off(w, 1) + on(a, ORANGE, 80, t=1) + on(b, BONE, 40, t=1) + strip(
            room, "Scanner Dual", BONE, "* Color 1", 100, speed=200, intensity=120)
        beat = [*on(a, BONE, 100, t=0.1), *on(b, ORANGE, 55, t=0.1), wait(0.92),
                *on(a, ORANGE, 55, t=0.1), *on(b, BONE, 100, t=0.1), wait(0.92)]
        rattle = sum(([*off(every, 0), wait(0.15), *on(every, BONE, 100, t=0), wait(0.15)] for _ in range(3)), [])
        return intro, beat * 4 + rattle
    if name == "pumpkins":
        intro = on(w, kelvin=2000, pct=10, t=2) + on(a, ORANGE, 80, t=2) + on(b, PURPLE, 50, t=2) + strip(
            room, "Fire 2012", ORANGE, "Fire", 100, speed=128, intensity=160)
        swing = [*on(a, PURPLE, 50, t=1.2), *on(b, ORANGE, 80, t=1.2), wait(1.6),
                 *on(a, ORANGE, 80, t=1.2), *on(b, PURPLE, 50, t=1.2), wait(1.6)]
        flare = [*on(every, ORANGE, 100, t=0.3), wait(0.6), *on(b, PURPLE, 50, t=0.8), wait(0.8)]
        return intro, swing * 3 + flare
    if name == "danse_macabre":
        # Midnight strikes twelve before Death tunes his violin.
        intro = off(w + every, 2) + [act("light.turn_off", [s["light"] for s in room["strips"]])] + [wait(2.5)]
        for _ in range(12):
            intro += on(every, BONE, 100, t=0) + [wait(0.25)] + off(every, 0.8) + [wait(1.0)]
        intro += strip(room, "Ghost Rider", PURPLE, "* Color 1", 100, speed=110) + on(w, kelvin=6500, pct=5, t=2)
        waltz = [*on(a, RED, 90, t=0.2), *on(b, PURPLE, 20, t=0.2), wait(1.0),
                 *on(a, PURPLE, 20, t=0.2), *on(b, RED, 90, t=0.2), wait(1.0)]
        return intro, waltz * 4
    if name == "thriller":
        intro = off(w, 2) + on(every, PURPLE, 30, t=2) + strip(room, "Halloween Eyes", RED, "* Color 1", 100,
                                                               speed=128, intensity=128)
        creep = [*on(a, GREEN, 60, t=1), *on(b, PURPLE, 40, t=1), wait(2),
                 *on(a, PURPLE, 40, t=1), *on(b, GREEN, 60, t=1), wait(2)]
        outage = [*off(every, 0), wait(0.35), *on(a, GREEN, 60, t=0), wait(0.2), *off(a, 0), wait(0.4),
                  *on(a, GREEN, 60, t=0.3), *on(b, PURPLE, 40, t=0.3), wait(1)]
        return intro, creep * 2 + outage
    if name == "the_shape":
        intro = off(w + b, 2) + on(a, ORANGE, 8, t=2) + strip(room, "Candle", ORANGE, "* Color 1", 55)
        pulse = [*on(a, ORANGE, 60, t=1.1), wait(1.2), *on(a, ORANGE, 6, t=1.1), wait(1.2)]
        return intro, pulse * 4
    if name == "storm":
        base = [*on(every, BLUE, 6, t=0), *off(w, 0)]
        intro = off(w, 1) + on(every, BLUE, 6, t=1) + strip(room, "Rain", [90, 120, 255], "* Color 1", 60, speed=170)
        steps, now = [], 0.0
        for at, strength, _ in STORM:
            if at > now:
                steps.append(wait(at - now))
            flash = [*on(every, BONE, 100, t=0), *on(w, kelvin=6500, pct=100, t=0), wait(0.08), *base]
            if strength > 0.75:
                flash += [wait(0.12), *on(every, BONE, 100, t=0), wait(0.06), *base]
            steps += flash
            now = at + 0.3 + (0.18 if strength > 0.75 else 0)
        return intro, steps
    raise ValueError(f"unknown programme {name!r}")


def show_script(s, room, speaker):
    intro, loop = program(s["program"], room)
    title = s.get("song", {}).get("name", "")
    match = slugify(title).split("_")[0] if title else ""
    if s.get("song"):
        play = [{"action": "music_assistant.play_media", "target": {"entity_id": speaker},
                 "data": {"media_id": s["song"]["name"], "artist": s["song"].get("artist"), "media_type": "track",
                          "enqueue": "replace"}}]
    else:
        f = H["sounds"][s["sound"]]
        play = [{"action": "media_player.play_media", "target": {"entity_id": speaker},
                 "data": {"media_content_id": media(f), "media_content_type": "music"}}]
    playing = f"is_state('{speaker}', 'playing')"
    still = playing + (f" and ((state_attr('{speaker}', 'media_title') or '') | lower).startswith('{match}')"
                       if match else "")
    body = [{"repeat": {"sequence": loop, "until": f"{{{{ not ({still}) or now().timestamp() > until }}}}"}}]
    if s["program"] == "storm":
        # a timeline, once (the sound file follows the same list), then the last rumble dies away
        body = loop + [{"wait_template": f"{{{{ not ({playing}) }}}}", "timeout": {"seconds": 25},
                        "continue_on_timeout": True}]
    return {
        "alias": f"Halloween · {s['name']}", "icon": s.get("icon", "mdi:music"), "mode": "single",
        "description": f"A Halloween show in the {room['name'].lower()}: "
                       + (f"{s['song']['name']} by {s['song'].get('artist')}" if s.get("song") else s["name"])
                       + ", with the lights.",
        "sequence": [
            {"action": "script.halloween_show_begin", "data": {"show": "{{ this.entity_id }}"}},
            *intro, *play,
            {"wait_template": f"{{{{ {playing} }}}}", "timeout": {"seconds": 25}, "continue_on_timeout": True},
            {"if": f"{{{{ not ({playing}) }}}}", "then": [
                {"action": "script.halloween_show_end"}, {"stop": "The music didn't start."}]},
            {"variables": {"until": f"{{{{ now().timestamp() + {int(s.get('minutes', 6) * 60)} }}}}"}},
            *body,
            {"action": "script.halloween_show_end"},
        ],
    }


def boo_script(room, speaker):
    a, b, w = groups(room)
    every = a + b
    sound = H["sounds"].get("boo")
    scream = [{"action": "media_player.play_media", "target": {"entity_id": speaker}, "continue_on_error": True,
               "data": {"media_content_id": media(sound), "media_content_type": "music", "announce": True}}] \
        if sound else []
    return {
        "alias": "Halloween · Boo!", "icon": "mdi:ghost", "mode": "single",
        "description": "A jump scare: every screen showing the dashboard sees a ghost, and the lights go mad.",
        "sequence": [
            {"if": "{{ states.script | selectattr('entity_id', 'search', '^script.halloween_show_') "
                   "| selectattr('state', 'eq', 'on') | list | count > 0 }}", "then": [{"stop": "A show is on."}]},
            {"action": "script.halloween_save", "data": {"slot": "boo", "rooms": [H["room"]]}},
            *scream,
            *sum(([*on(every, RED, 100, t=0), *on(w, kelvin=6500, pct=100, t=0), wait(0.12),
                   *off(every + w, 0), wait(0.18)] for _ in range(4)), []),
            wait(1.5),
            {"action": "script.halloween_restore", "data": {"slot": "boo", "rooms": [H["room"]]}},
        ],
    }


def media(name):
    folder = H["sounds"].get("folder", "halloween").strip("/")
    return f"media-source://media_source/local/{folder}/{name}"


# --- pranks ----------------------------------------------------------------------------
def flicker(ents, seconds):
    """Flicker lights like a failing bulb for a few seconds (inline, so no helper script)."""
    return [{"repeat": {"count": int(seconds / 0.45), "sequence": [
        act("light.turn_on", ents, brightness_pct="{{ range(3, 70) | random }}", transition=0.1),
        {"delay": {"milliseconds": "{{ range(120, 520) | random }}"}},
        {"if": "{{ range(4) | random == 0 }}", "then": [act("light.turn_off", ents, transition=0),
                                                        {"delay": {"milliseconds": "{{ range(80, 300) | random }}"}}]},
    ]}}]


def say(speaker, text, voice=None):
    tts = H.get("tts")
    if not tts:
        return []
    a = {"action": "tts.speak", "target": {"entity_id": tts}, "continue_on_error": True,
         "data": {"media_player_entity_id": speaker, "message": text}}
    if voice:
        a["data"]["options"] = {"voice": voice}
    return [a]


def automations(rs):
    season = {"condition": "state", "entity_id": SEASON, "state": "on"}
    pranks = {"condition": "state", "entity_id": PRANKS, "state": "on"}
    dark = {"condition": "state", "entity_id": "sun.sun", "state": "below_horizon"}
    out = [{
        "id": "lumen_halloween_tidy_up", "alias": "Halloween · tidy up",
        "description": "Nights and the end of the season: lights that went Halloween and are now off forget their "
                       "colour, so nothing wakes up blood red. At the end of the season everything goes back.",
        "mode": "queued",
        "triggers": ([{"trigger": "state", "entity_id": L.CFG["night_mode"], "to": "on", "for": {"minutes": 3},
                       "id": "night"}] if L.CFG.get("night_mode") else []) + [
            {"trigger": "time", "at": "05:00:00", "id": "night"},
            {"trigger": "state", "entity_id": SEASON, "from": "on", "to": "off", "id": "over"}],
        "conditions": [{"condition": "template", "value_template": "{{ states('" + TOUCHED + "') not in "
                                                                   "['', 'unknown', 'unavailable'] }}"}],
        "actions": [{"action": "script.halloween_normal",
                     "data": {"forget": "{{ trigger.id == 'night' }}"}}],
    }]
    lights_ = all_lights(room_map(rs))
    out.append({
        "id": "lumen_halloween_wake_up", "alias": "Halloween · lights wake up warm",
        "description": "A light that forgot its Halloween colour (Back to normal while it was off) remembers the "
                       "lowest brightness too: the first time it's switched on plainly, it gets a proper one.",
        "mode": "parallel",
        "triggers": [{"trigger": "state", "entity_id": lights_, "from": "off", "to": "on"}],
        "variables": {"all": lights_, "pending": listvar(PENDING),
                      "i": "{{ all.index(trigger.entity_id) if trigger.entity_id in all else -1 }}"},
        "conditions": [{"condition": "template", "value_template": "{{ i | string in pending }}"},
                       {"condition": "state", "entity_id": "script.halloween_restore", "state": "off"}],
        "actions": [
            {"if": "{{ (state_attr(trigger.entity_id, 'brightness') or 255) <= 3 }}",
             "then": [act("light.turn_on", "{{ trigger.entity_id }}", brightness_pct=60, transition=0.4)]},
            act("input_text.set_value", PENDING, value="{{ pending | reject('eq', i | string) | join(',') }}"),
        ],
    })
    p = H.get("pranks") or {}
    speaker = H.get("speaker")
    if p.get("doorbell"):
        d = p["doorbell"]
        room = rs.get(d.get("room"))
        ls = sum(room["lights"].values(), []) if room else []
        out.append({
            "id": "lumen_halloween_trick_or_treat", "alias": "Halloween · trick or treat",
            "description": "Pranks on, after dark: the doorbell makes the hall flicker orange and purple, and the "
                           "house says who's there.",
            "mode": "single", "max_exceeded": "silent",
            "triggers": [{"trigger": "state", "entity_id": d["sensor"], "to": "on"}],
            "conditions": [season, pranks, dark],
            "actions": [
                {"action": "script.halloween_save", "data": {"slot": "door", "rooms": [d["room"]]}},
                *say(d.get("speaker", speaker), d.get("say", "Trick or treat! Somebody is at the door... "
                                                              "and they want candy."), d.get("voice")),
                *sum(([*on(ls, ORANGE, 100, t=0), wait(0.5), *on(ls, PURPLE, 60, t=0), wait(0.5)]
                      for _ in range(6)), []),
                {"action": "script.halloween_restore", "data": {"slot": "door", "rooms": [d["room"]]}},
            ],
        })
    for i, h in enumerate(p.get("doors") or []):
        out.append({
            "id": f"lumen_halloween_creaky_door_{i}", "alias": f"Halloween · {h.get('name', 'creaky door')}",
            "description": "Pranks on, in the evening: opening it makes a light flicker, with a sound.",
            "mode": "single", "max_exceeded": "silent",
            "triggers": [{"trigger": "state", "entity_id": h["sensor"], "from": "off", "to": "on"}],
            "conditions": [season, pranks, dark, {"condition": "time", "after": "16:00:00", "before": "23:00:00"}],
            "actions": [
                *([{"action": "media_player.play_media", "target": {"entity_id": h.get("speaker", speaker)},
                    "continue_on_error": True,
                    "data": {"media_content_id": media(H["sounds"][h["sound"]]), "media_content_type": "music",
                             "announce": True}}] if H["sounds"].get(h.get("sound")) else []),
                *([{"action": "scene.create", "data": {"scene_id": f"halloween_door_{i}",
                                                       "snapshot_entities": [h["light"]]}},
                   *flicker([h["light"]], 5),
                   act("scene.turn_on", f"scene.halloween_door_{i}"),
                   act("scene.delete", f"scene.halloween_door_{i}")] if h.get("light") else []),
            ],
        })
    for i, m in enumerate(p.get("flicker") or []):
        out.append({
            "id": f"lumen_halloween_flicker_{i}", "alias": f"Halloween · {m.get('name', 'flickering light')}",
            "description": "Pranks on, after dark: now and then, walking in makes the light flicker.",
            "mode": "single", "max_exceeded": "silent",
            "triggers": [{"trigger": "state", "entity_id": m["sensor"], "from": "off", "to": "on"}],
            "conditions": [season, pranks, dark,
                           {"condition": "template", "value_template": f"{{{{ range({m.get('one_in', 3)}) | random == 0 }}}}"}],
            "actions": [
                {"wait_template": f"{{{{ is_state('{m['light']}', 'on') }}}}", "timeout": {"seconds": 10},
                 "continue_on_timeout": False},
                wait(m.get("after", 4)),
                {"action": "scene.create", "data": {"scene_id": f"halloween_flicker_{i}",
                                                    "snapshot_entities": [m["light"]]}},
                *flicker([m["light"]], 4),
                act("scene.turn_on", f"scene.halloween_flicker_{i}"),
                act("scene.delete", f"scene.halloween_flicker_{i}"),
            ],
        })
    if p.get("witching_hour", True):
        lit = sorted({l for r in rs.values() for l in r["lights"]["color"] + r["lights"]["ct"] + r["lights"]["dim"]})
        out.append({
            "id": "lumen_halloween_witching_hour", "alias": "Halloween · the witching hour",
            "description": "Pranks on: at three in the night after Halloween, whatever light is still on flickers.",
            "mode": "single",
            "triggers": [{"trigger": "time", "at": "03:00:00"}],
            "conditions": [pranks, {"condition": "template", "value_template": "{{ now().month == 11 and now().day == 1 }}"},
                           {"condition": "template", "value_template": f"{{{{ {json.dumps(lit)} | select('is_state', 'on') | list | count > 0 }}}}"}],
            "actions": [
                {"variables": {"lit": f"{{{{ {json.dumps(lit)} | select('is_state', 'on') | list }}}}"}},
                {"action": "scene.create", "data": {"scene_id": "halloween_witching", "snapshot_entities": "{{ lit }}"}},
                *flicker("{{ lit }}", 6),
                act("scene.turn_on", "scene.halloween_witching", transition=1),
                act("scene.delete", "scene.halloween_witching"),
            ],
        })
    return out


def package():
    """The Home Assistant package: everything Halloween on the HA side, in one file."""
    rs = rooms()
    start, end = H["season"]["from"], H["season"]["until"]
    in_season = (f"'{start}' <= d <= '{end}'" if start <= end else f"d >= '{start}' or d <= '{end}'")
    shows = [f"script.halloween_show_{s['key']}" for s in H["shows"]]
    scenes = [scene(scene_name(r, LOOKS[k]), LOOKS[k]["icon"], LOOKS[k], [r])
              for r in rs.values() for k in r["looks"]]
    scenes.append(scene("Halloween · " + HOUSE_LOOK["name"], HOUSE_LOOK["icon"], HOUSE_LOOK, list(rs.values())))
    return {
        "template": [{"binary_sensor": [
            {"name": "Halloween season", "unique_id": "lumen_halloween_season", "icon": "mdi:halloween",
             "state": "{% set d = now().strftime('%m-%d') %}{{ " + in_season + " }}",
             "attributes": {
                 "nights_to_go": "{{ (now().replace(month=10, day=31).date() - now().date()).days }}",
                 "from": start, "until": end}},
            {"name": "Halloween lights", "unique_id": "lumen_halloween_lights", "icon": "mdi:lightbulb-alert-outline",
             "state": "{{ states('" + TOUCHED + "') not in ['', 'unknown', 'unavailable'] or "
                      + json.dumps(shows) + " | select('is_state', 'on') | list | count > 0 }}",
             "attributes": {"rooms": listvar(TOUCHED)}},
        ]}],
        "input_text": {
            "halloween_rooms": {"name": "Halloween rooms", "max": 255, "icon": "mdi:halloween"},
            "halloween_paused": {"name": "Halloween paused automations", "max": 255, "icon": "mdi:pause"},
            "halloween_wake_up": {"name": "Halloween lights to wake up warm", "max": 255, "icon": "mdi:lightbulb-auto"},
        },
        "input_boolean": {"halloween_pranks": {"name": "Halloween pranks", "icon": "mdi:emoticon-devil-outline"}},
        "scene": scenes,
        "script": scripts(rs),
        "automation": automations(rs),
    }


def write_package(path):
    import yaml

    class Dumper(yaml.SafeDumper):
        pass

    Dumper.ignore_aliases = lambda self, data: True
    Dumper.add_representer(str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s,
                                                                 style="|" if "\n" in s else None))
    head = ("# Lumen · Halloween — generated by build_dashboard.py from house.yaml's halloween: section.\n"
            "# Copy to /config/packages/halloween.yaml and reload (Developer tools → YAML → All YAML configuration).\n"
            "# To remove Halloween from Home Assistant, delete this file and reload.\n")
    with open(path, "w") as fh:
        fh.write(head + yaml.dump(package(), Dumper=Dumper, sort_keys=False, allow_unicode=True, width=120))


# --------------------------------------------------------------------------------------
# Sounds: the thunderstorm is synthesised, so its rumbles line up with STORM's flashes
# --------------------------------------------------------------------------------------
def thunder_wav(rate=22050):
    rnd = random.Random(1031)
    n = int(STORM_SECONDS * rate)
    out = [0.0] * n
    # rain: soft, slightly low-passed noise all through, swelling a little with the storm
    lp = 0.0
    for i in range(n):
        lp += 0.35 * (rnd.uniform(-1, 1) - lp)
        swell = 0.5 + 0.5 * math.sin(i / n * math.pi)
        out[i] = lp * 0.11 * (0.6 + 0.4 * swell)
    for at, strength, delay in STORM:
        start = int((at + delay) * rate)
        length = int((3.5 + 5 * strength) * rate)
        brown, slow, crack = 0.0, 0.0, delay < 1.0
        for j in range(min(length, n - start)):
            x = j / rate
            brown = (brown + rnd.uniform(-1, 1) * 0.06) * 0.996
            slow += 0.02 * (brown - slow)
            env = (min(1.0, x / 0.18) * math.exp(-x / (0.9 + 1.6 * strength))
                   * (0.75 + 0.25 * math.sin(x * 7.3 + at)))
            v = slow * 9 * env * strength
            if crack and x < 0.25:  # close strikes crack before they rumble
                v += rnd.uniform(-1, 1) * 0.55 * strength * math.exp(-x / 0.05)
            out[start + j] += v
    peak = max(abs(v) for v in out) or 1
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(v / peak * 0.92 * 32767)) for v in out))
    return buf.getvalue()


def upload_sounds(call):
    """Upload the synthesised thunder to the sounds folder once (HA's local media, e.g. /media/halloween)."""
    if not H["sounds"].get("thunder"):
        return
    folder = H["sounds"].get("folder", "halloween").strip("/")
    base = f"media-source://media_source/local/{folder}"
    try:
        have = {c["title"] for c in call("media_source/browse_media", media_content_id=base)["children"]}
    except RuntimeError:
        have = set()
    name = H["sounds"]["thunder"]
    if name in have:
        return
    print(f"halloween: uploading {name} to {folder}/")
    body, form = L.multipart({"media_content_id": base}, {"file": (name, "audio/wav", thunder_wav())})
    L.http("/api/media_source/local_source/upload", body,
           {"Authorization": f"Bearer {L.HA['token']}", "Content-Type": form})


# --------------------------------------------------------------------------------------
# The dashboard
# --------------------------------------------------------------------------------------
def path():
    return f"/{L.URL_PATH}/halloween"


IN_SEASON = [{"condition": "state", "entity": SEASON, "state": "on"}]

# A look's buttons glow in its colour while it's the room's latest scene and the room is lit.
SPOOKY_TILE = """
ha-card { overflow: hidden; isolation: isolate; }
ha-card::before { content: ""; position: absolute; inset: 0; z-index: -1; opacity: .14; pointer-events: none;
  background: radial-gradient(120% 90% at 50% 0%, var(--tile-color), transparent 70%); transition: opacity .45s EASE; }
@media (hover: hover) { ha-card:hover::before { opacity: .28; } }
"""
SPOOKY_ON = """
ha-card::before { opacity: .5; }
ha-card { border-color: color-mix(in srgb, var(--tile-color) 65%, transparent) !important;
  box-shadow: 0 0 28px -8px var(--tile-color), 0 10px 30px -12px rgba(0, 0, 0, .35) !important; }
ha-tile-icon { scale: 1.12; }
"""


def scene_tap(scene_id, areas):
    return {"action": "perform-action", "perform_action": "script.halloween_scene",
            "data": {"scene": scene_id, "rooms": areas}}


def normal_tap(areas=None):
    return {"action": "perform-action", "perform_action": "script.halloween_normal",
            "data": {"rooms": areas} if areas else {}}


def look_tiles(area, r, cols):
    ids_ = [scene_entity(r, LOOKS[k]) for k in r["looks"]]
    lit = L.lights_on(sum(r["lights"].values(), []) + [s["light"] for s in r["strips"]])[3:-3] + " > 0"
    latest = ("{% set ts = " + json.dumps(ids_) + " | map('states') | reject('in', ['unknown', 'unavailable']) "
              "| list %}{% set latest = ts | max if ts else '' %}")
    tiles = []
    for k, sid in zip(r["looks"], ids_):
        look = LOOKS[k]
        style = (L.GLASS + SPOOKY_TILE.replace("EASE", L.EASE) + latest
                 + f"{{% if {lit} and states('{sid}') == latest and is_state('{ACTIVE}', 'on') %}}" + SPOOKY_ON
                 + "{% endif %}")
        tiles.append({**L.tile(sid, cols, vertical=True, name=look["name"], icon=look["icon"], color=look["color"],
                               hide_state=True, tap=scene_tap(sid, [area])),
                      "_style": "plain", "card_mod": {"style": style}})
    tiles.append(normal_tile(cols, [area], "Normal"))
    return tiles


def normal_tile(cols, areas=None, name="Back to normal", vertical=None):
    """The way back: lit up while something's in Halloween colours."""
    secondary = ("{% set r = state_attr('" + ACTIVE + "', 'rooms') or [] %}"
                 "{{ 'Everything is normal' if is_state('" + ACTIVE + "', 'off') else "
                 "(r | count ~ (' room' if r | count == 1 else ' rooms') ~ ' in Halloween colours') if r else "
                 "'A show is on' }}")
    if areas:
        secondary = "{{ 'Lights as they were' }}"
    return {"type": "custom:mushroom-template-card", "primary": name, "secondary": secondary,
            "icon": "mdi:lightbulb-on-outline", "vertical": cols <= 6 if vertical is None else vertical,
            "icon_color": f"{{{{ 'amber' if is_state('{ACTIVE}', 'on') else 'disabled' }}}}",
            "tap_action": normal_tap(areas), **L.grid(cols)}


def room_section(area_id):
    """The Halloween row on a room page (in season, and only for rooms with looks)."""
    if not enabled():
        return None
    r = rooms().get(area_id)
    if not r or not r["looks"]:
        return None
    s = L.section([L.heading("Halloween", "mdi:halloween", tap=path())] + look_row(area_id, r), span=3)
    s["visibility"] = IN_SEASON
    return s


def look_row(area_id, r):
    """A room's looks and Normal in one row on wide screens; three to a row on phones (like the Scenes row)."""
    n = len(r["looks"]) + 1
    cards = []
    for cols, query in [(max(4, min(12, 36 // n)), "(min-width: 768px)"), (4, "(max-width: 767px)")]:
        screen = [{"condition": "screen", "media_query": query}]
        cards += [{**t, "visibility": screen} for t in look_tiles(area_id, r, cols)]
    return cards


def favourite_visibility(option):
    """WLED favourites called something Halloween only show in season."""
    return IN_SEASON if enabled() and is_spooky(option) else None


# The page's hero: the countdown over a night sky with a moon, bats and a jack-o'-lantern.
HERO_ART = "data:image/svg+xml;utf8," + urllib.parse.quote(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 800 300' preserveAspectRatio='xMidYMid slice'>"
    "<defs><radialGradient id='m' cx='78%' cy='39%' r='22%'><stop offset='0' stop-color='#ffe6a8'/>"
    "<stop offset='.55' stop-color='#ffb347' stop-opacity='.55'/><stop offset='1' stop-color='#ff7a00' stop-opacity='0'/>"
    "</radialGradient><linearGradient id='s' x1='0' y1='0' x2='0' y2='1'><stop offset='0' stop-color='#1a0b2e'/>"
    "<stop offset='1' stop-color='#3a1206'/></linearGradient></defs>"
    "<rect width='800' height='300' fill='url(#s)'/><circle cx='624' cy='118' r='120' fill='url(#m)'/>"
    "<circle cx='624' cy='118' r='46' fill='#ffe9b8'/>"
    "<g fill='#0b0612'><path d='M0 300 L0 236 Q90 210 170 240 T360 232 T560 246 T800 226 L800 300Z'/>"
    "<path d='M120 240 l6-38 l6 38z M300 236 l5-30 l5 30z M690 232 l7-44 l7 44z'/>"
    "<path d='M560 60 q12-10 22 0 q6-8 14 0 q-8 2-14 8 q-4-4-8 0 q-6-6-14-8z'/>"
    "<path d='M470 104 q9-8 17 0 q5-6 11 0 q-6 2-11 6 q-3-3-6 0 q-5-5-11-6z'/>"
    "<path d='M700 140 q8-7 15 0 q4-5 10 0 q-5 1-10 5 q-3-3-5 0 q-4-4-10-5z'/></g></svg>")

HERO_STYLE = """
ha-card {
  min-height: 220px; justify-content: flex-end; overflow: hidden; border: none !important;
  background: linear-gradient(0deg, rgba(10, 4, 18, .78), rgba(10, 4, 18, 0) 70%), url("ART") center / cover !important;
}
ha-card:active { scale: .99; transition: scale .1s; }
.container { padding: 22px 24px !important; }
span[slot=primary] { color: #fff !important; font-size: clamp(26px, 4vw, 38px) !important; font-weight: 750 !important;
  letter-spacing: -.02em; line-height: 1.1 !important; text-shadow: 0 2px 18px rgba(0, 0, 0, .6); white-space: normal !important; }
span[slot=secondary] { color: rgba(255, 236, 214, .85) !important; font-size: 15px !important; margin-top: 6px; }
ha-tile-icon { --tile-icon-color: #ff8a1a; scale: 1.35; transform-origin: left bottom; margin-bottom: 6px;
  filter: drop-shadow(0 0 14px rgba(255, 138, 26, .7)); animation: lumen-flicker 3.2s infinite; }
@keyframes lumen-flicker { 0%, 100% { opacity: 1; } 46% { opacity: .82; } 50% { opacity: .95; } 53% { opacity: .7; } 56% { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { ha-tile-icon { animation: none; } }
""".replace("ART", HERO_ART)

COUNTDOWN = ("{% set n = state_attr('" + SEASON + "', 'nights_to_go') | int(0) %}"
             "{{ 'Happy Halloween!' if n == 0 else 'One more night until Halloween' if n == 1 else "
             "(n ~ ' nights until Halloween') if n > 1 else 'Halloween is over… for now' }}")


def hero():
    user = L.CFG.get("user_name")
    return {"type": "custom:mushroom-template-card", "primary": COUNTDOWN,
            "secondary": "Scenes, shows and a way back to normal" + (f" · happy haunting, {user}" if user else ""),
            "icon": "mdi:halloween", "icon_color": "orange", "vertical": False, "multiline_secondary": True,
            # the pumpkin is an easter egg: tap it
            "tap_action": {"action": "perform-action", "perform_action": "script.halloween_boo"}
            if H.get("speaker") else {"action": "none"},
            "hold_action": {"action": "none"},
            "_style": "plain", "card_mod": {"style": HERO_STYLE}, **L.grid(36, rows=4)}


def show_card(s, cols):
    """A show: its name and who it's by, glowing while it runs; tap to start, tap again to stop."""
    sid = f"script.halloween_show_{s['key']}"
    who = s["song"].get("artist") if s.get("song") else "Lightning, then the thunder"
    style = (L.GLASS + SPOOKY_TILE.replace("EASE", L.EASE).replace("var(--tile-color)", "rgb(var(--rgb-COLOR))")
             + f"{{% if is_state('{sid}', 'on') %}}" + SPOOKY_ON.replace("var(--tile-color)", "rgb(var(--rgb-COLOR))")
             + "{% endif %}").replace("COLOR", s.get("color", "orange"))
    return {"type": "custom:mushroom-template-card", "entity": sid, "primary": s["name"],
            "secondary": f"{{{{ 'Playing · tap to stop' if is_state('{sid}', 'on') else {json.dumps(who)} }}}}",
            "icon": s.get("icon", "mdi:music"),
            "icon_color": s.get("color", "orange"),
            "vertical": True, "tap_action": {"action": "perform-action", "perform_action": "script.halloween_show",
                                             "data": {"show": sid}},
            "hold_action": {"action": "more-info"}, "_style": "plain", "card_mod": {"style": style}, **L.grid(cols)}


WIDTHS = [(True, "(min-width: 768px)"), (False, "(max-width: 767px)")]


def screen(cards, query):
    return [{**c, "visibility": [{"condition": "screen", "media_query": query}]} for c in cards]


def view():
    rs = rooms()
    speaker = H.get("speaker")
    sections = [L.section([hero()], span=3)]

    # Three across on wide screens; on phones the scene and Pranks side by side, Back to normal under them.
    house = [L.heading("Whole house", "mdi:home-outline")]
    for wide, query in WIDTHS:
        house += screen([
            {**L.tile(HOUSE_SCENE, 12 if wide else 6, vertical=True, name=HOUSE_LOOK["name"], icon=HOUSE_LOOK["icon"],
                      color=HOUSE_LOOK["color"], hide_state=True, tap=scene_tap(HOUSE_SCENE, list(rs))),
             "_style": "plain", "card_mod": {"style": L.GLASS + SPOOKY_TILE.replace("EASE", L.EASE)}},
            *([normal_tile(12, vertical=True)] if wide else []),
            L.tile(PRANKS, 12 if wide else 6, vertical=True, name="Pranks", icon="mdi:emoticon-devil-outline",
                   color="deep-purple", tap={"action": "toggle"}),
            *([] if wide else [normal_tile(12, vertical=False)])], query)
    sections.append(L.section(house, span=3))

    shows = [s for s in H["shows"] if s.get("song") or H["sounds"].get(s.get("sound"))]
    if speaker and shows:
        cards = [L.heading("Shows", "mdi:music-clef-treble")]
        for wide, query in WIDTHS:
            cards += screen([show_card(s, 12 if wide else 6) for s in shows], query)
        playing = L.player_card(speaker, "Speakers", 36, rows=3, volume=True)
        playing["visibility"] = [{"condition": "state", "entity": speaker, "state": "playing"}]
        cards.append(playing)
        sections.append(L.section(cards, span=3))

    # The shows' room across the page, the others side by side.
    for area, r in sorted(rs.items(), key=lambda i: i[0] != H["room"]):
        if not r["looks"]:
            continue
        cards = [L.heading(r["name"], "mdi:halloween" if area == H["room"] else "mdi:door-open",
                           tap=f"/{L.URL_PATH}/{r['path']}")]
        if area == H["room"]:
            cards += look_row(area, r)
        else:
            cards += look_tiles(area, r, 6)
        sections.append(L.section(cards, span=3 if area == H["room"] else 1))

    return {"title": "Halloween", "path": "halloween", "icon": "mdi:halloween", "type": "sections",
            "max_columns": 3, "dense_section_placement": True, "sections": sections,
            "background": "radial-gradient(120% 60% at 80% 0%, color-mix(in srgb, #ff7a00 16%, transparent), "
                          "transparent 70%) fixed, radial-gradient(90% 50% at 0% 100%, color-mix(in srgb, "
                          "#7c3aed 14%, transparent), transparent 70%) fixed, var(--primary-background-color)"}


def nav_route():
    """The Halloween tab: only there in season."""
    return {"url": path(), "icon": "mdi:halloween", "label": "Halloween",
            "hidden": f"[[[ return hass.states['{SEASON}']?.state !== 'on'; ]]]",
            "selected": f"[[[ return location.pathname === '{path()}'; ]]]"}


def home_section():
    """On the home page in season: a banner to the Halloween page, and Back to normal while it's needed."""
    banner = {"type": "custom:mushroom-template-card", "primary": COUNTDOWN,
              "secondary": "Spooky scenes, shows and more",
              "icon": "mdi:halloween", "icon_color": "orange", "tap_action": L.nav(path()),
              "_style": "plain", "card_mod": {"style": L.GLASS + BANNER_STYLE}, **L.grid(12)}
    back = {**normal_tile(12), "visibility": [{"condition": "state", "entity": ACTIVE, "state": "on"}]}
    s = L.section([banner, back])
    s["visibility"] = IN_SEASON
    return s


BANNER_STYLE = """
ha-card { overflow: hidden; isolation: isolate;
  background: linear-gradient(110deg, color-mix(in srgb, #ff7a00 26%, var(--card-background-color)),
                              color-mix(in srgb, #7c3aed 20%, var(--card-background-color))) !important; }
ha-tile-icon { filter: drop-shadow(0 0 10px rgba(255, 122, 0, .6)); }
"""


def greeting(base):
    """The home page greeting, in season: the witching hour after midnight, and on the night itself."""
    return ("{% if is_state('" + SEASON + "', 'on') and now().hour < 4 %}The witching hour"
            "{% elif is_state('" + SEASON + "', 'on') and now().month == 10 and now().day == 31 %}Happy Halloween"
            "{% else %}" + base + "{% endif %}")


# Cobwebs in the corner of the room photos while it's the season (--lumen-halloween is set by EASTER_JS).
COBWEB = "data:image/svg+xml;utf8," + urllib.parse.quote(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 120 120' fill='none' stroke='white' stroke-width='1.1' "
    "stroke-linecap='round' opacity='.75'><path d='M0 0 L120 40 M0 0 L95 95 M0 0 L40 120 M0 0 L120 8 M0 0 L8 120'/>"
    "<path d='M22 7 Q20 16 17 17 Q10 20 7 22 M46 15 Q42 32 35 35 Q20 41 15 46 M72 24 Q64 50 53 53 Q30 63 24 72 "
    "M98 33 Q86 68 71 71 Q40 84 33 98'/></svg>")
COBWEB_STYLE = """
room-background-image::before {
  content: ""; position: absolute; left: 0; bottom: 0; width: 34%; max-width: 130px; aspect-ratio: 1; z-index: 1;
  background: url("WEB") left bottom / contain no-repeat; transform: scaleY(-1);
  opacity: calc(var(--lumen-halloween, 0) * .55); pointer-events: none;
}
""".replace("WEB", COBWEB)

BAT = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 32'><path fill='#140a1e' d='M32 10c-2-4-4-5-6-4 "
       "1 3-1 6-4 5-4-1-7-6-14-7 4 4 5 9 3 13 4-2 8-1 10 2 2-3 6-3 8 0 1-2 2-3 3-3s2 1 3 3c2-3 6-3 8 0 2-3 6-4 10-2-2-4"
       "-1-9 3-13-7 1-10 6-14 7-3 1-5-2-4-5-2-1-4 0-6 4z'/></svg>")
GHOST = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 120'><path fill='#f4f1ff' d='M50 4C26 4 12 22 12 "
         "46v66l12-10 13 12 13-12 13 12 13-12 12 10V46C88 22 74 4 50 4z'/><ellipse cx='36' cy='48' rx='7' ry='10' "
         "fill='#120a1a'/><ellipse cx='64' cy='48' rx='7' ry='10' fill='#120a1a'/><ellipse cx='50' cy='76' rx='9' "
         "ry='12' fill='#120a1a'/></svg>")

# Easter eggs, run from the navbar (it's on every view), once per page load. In season:
# - --lumen-halloween on <html>, which shows the cobwebs on the room photos;
# - now and then a bat flits across the screen (more often on the night itself, after dark);
# - when script.halloween_boo runs (tap the pumpkin on the Halloween page), a ghost rushes at every screen;
# - the Konami code (↑ ↑ ↓ ↓ ← → ← → B A) starts Spooky Scary Skeletons, and skulls rain down.
EASTER_JS = """
if (!window.__lumenHalloween) {
  window.__lumenHalloween = true;
  const SEASON = '@SEASON@', BOO = 'script.halloween_boo', SKELETONS = 'script.halloween_show_skeletons';
  const hassNow = () => document.querySelector('home-assistant')?.hass;
  const inSeason = () => hassNow()?.states[SEASON]?.state === 'on';
  const calm = matchMedia('(prefers-reduced-motion: reduce)');
  const sprite = (svg, css) => {
    const el = document.createElement('div');
    el.innerHTML = svg;
    el.style.cssText = 'position:fixed;z-index:99999;pointer-events:none;' + css;
    document.body.appendChild(el);
    return el;
  };
  const bat = () => {
    if (document.hidden || calm.matches) return;
    const size = 30 + Math.random() * 34, y = 8 + Math.random() * 55, ltr = Math.random() < .5;
    const el = sprite(@BAT@, `width:${size}px;top:${y}vh;left:0;filter:drop-shadow(0 0 6px rgba(255,140,40,.35))`);
    const x0 = ltr ? -80 : innerWidth + 80, x1 = ltr ? innerWidth + 80 : -80;
    const frames = [0, .25, .5, .75, 1].map(f => ({
      transform: `translate(${x0 + (x1 - x0) * f}px, ${Math.sin(f * 9) * 40}px) scaleX(${ltr ? 1 : -1}) scaleY(${f * 20 % 2 < 1 ? 1 : .7})`}));
    el.animate(frames, {duration: 3800 + Math.random() * 2500, easing: 'ease-in-out'}).onfinish = () => el.remove();
  };
  const batLoop = () => {
    const d = new Date(), night = d.getMonth() === 9 && d.getDate() === 31 && (d.getHours() >= 17 || d.getHours() < 4);
    if (inSeason()) { bat(); if (Math.random() < .3) setTimeout(bat, 500 + Math.random() * 900); }
    setTimeout(batLoop, (night ? 40 : 110) * 1000 * (0.6 + Math.random() * 0.9));
  };
  setTimeout(batLoop, 12000 + Math.random() * 20000);
  const ghost = () => {
    const el = sprite(@GHOST@, 'left:50%;top:50%;width:40vmin;');
    el.animate([{transform: 'translate(-50%,-30%) scale(.2)', opacity: 0},
                {transform: 'translate(-50%,-50%) scale(1)', opacity: .95, offset: .35},
                {transform: 'translate(-50%,-50%) scale(2.6)', opacity: 0}],
               {duration: 1300, easing: 'cubic-bezier(.3,0,.2,1)'}).onfinish = () => el.remove();
    document.body.animate([{transform: 'translate(0,0)'}, {transform: 'translate(-6px,3px)'}, {transform: 'translate(5px,-4px)'},
                           {transform: 'translate(-3px,2px)'}, {transform: 'translate(0,0)'}], {duration: 380, iterations: 2});
  };
  let booWas = null;
  setInterval(() => {
    const h = hassNow();
    if (!h) return;
    document.documentElement.style.setProperty('--lumen-halloween', inSeason() ? '1' : '0');
    const boo = h.states[BOO]?.state;
    if (boo === 'on' && booWas === 'off' && inSeason()) ghost();
    booWas = boo;
  }, 1000);
  const code = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'ArrowLeft', 'ArrowRight', 'b', 'a'];
  let at = 0;
  window.addEventListener('keydown', (e) => {
    at = e.key === code[at] ? at + 1 : (e.key === code[0] ? 1 : 0);
    if (at < code.length) return;
    at = 0;
    if (!inSeason()) return;
    hassNow()?.callService('script', 'turn_on', {entity_id: SKELETONS});
    for (let i = 0; i < 24; i++) setTimeout(() => {
      const el = sprite('<div style="font-size:' + (24 + Math.random() * 30) + 'px">💀</div>', `left:${Math.random() * 96}vw;top:-60px`);
      el.animate([{transform: 'translateY(0) rotate(0)'}, {transform: `translateY(${innerHeight + 120}px) rotate(${Math.random() * 720 - 360}deg)`}],
                 {duration: 2200 + Math.random() * 1600, easing: 'cubic-bezier(.4,0,.8,.6)'}).onfinish = () => el.remove();
    }, i * 120);
  });
}
"""


def easter_js():
    if not enabled():
        return ""
    return (EASTER_JS.replace("@SEASON@", SEASON).replace("@BAT@", json.dumps(BAT))
            .replace("@GHOST@", json.dumps(GHOST)))


def room_card_style():
    return COBWEB_STYLE if enabled() else ""
