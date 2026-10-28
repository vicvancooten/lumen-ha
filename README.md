# Lumen for Home Assistant

A calm, modern look for Home Assistant: a **theme** with frosted-glass cards, plus a
**dashboard generator** that builds a floor → room dashboard from a short YAML file.
It includes photo room cards, a floating navigation bar with a sliding indicator, and a
mini header with search and Assist.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![Validate](https://github.com/vicvancooten/lumen-ha/actions/workflows/validate.yml/badge.svg)](https://github.com/vicvancooten/lumen-ha/actions/workflows/validate.yml)

<p align="center">
  <img src="docs/desktop-home-dark.webp" alt="Home page, dark" width="100%">
</p>

<p align="center">
  <img src="docs/mobile-home-dark.webp" alt="Mobile home" width="24%">
  <img src="docs/mobile-room-dark.webp" alt="Mobile room page" width="24%">
  <img src="docs/mobile-room-light.webp" alt="Mobile room page, light" width="24%">
</p>

<details>
<summary>More screenshots</summary>

![Home page, light](docs/desktop-home-light.webp)
![Floor page](docs/desktop-floor-dark.webp)
![Navbar motion](docs/navbar-motion.webp)

</details>

## What's in the box

| Part | What it does |
| --- | --- |
| `themes/lumen.yaml` | The **Lumen** theme. Light and dark modes, Inter typography, soft 28px frosted cards with pill-shaped chips, buttons, sliders and icons, violet accent, warm amber for lights that are on. With card-mod, cards rise in when a page opens and lift on hover. Status bar colours match the page for an edge-to-edge app look. |
| `dashboard/build_dashboard.py` | Generates the dashboard: a home page (greeting, weather, quick settings, energy, every room as a photo card), one page per floor, and one page per room with lighting, media, vacuum, fridge, climate, power and details sections. |
| `dashboard/house.example.yaml` | Describes your home: floors, rooms and which entities go where. |

## 1. Install the theme

### Through HACS (recommended)

1. Open **HACS**, click the ⋮ menu (top right), then **Custom repositories**.
2. Add `https://github.com/vicvancooten/lumen-ha` with the type **Theme**.
3. Search for **Lumen** in HACS and click **Download**.
4. Go to **Developer tools → YAML** and click **Reload themes**, or restart Home Assistant.
5. Open your **profile**, set **Theme** to **Lumen** and the mode to **Auto**.

HACS themes need themes enabled in `configuration.yaml`. Most setups already have this:

```yaml
frontend:
  themes: !include_dir_merge_named themes
```

### Manually

Copy `themes/lumen.yaml` to `/config/themes/lumen.yaml`, then follow steps 4–5 above.

> **Frosted blur:** install [card-mod](https://github.com/thomasloven/lovelace-card-mod) (HACS → Frontend)
> to get the blur behind cards. Without card-mod the theme still works; cards are just solid.

## 2. Generate the dashboard (optional)

The theme stands on its own. The generator adds the full dashboard.

### Requirements

Install these frontend cards from **HACS → Frontend**:

- [Mushroom](https://github.com/piitaya/lovelace-mushroom)
- [mini-graph-card](https://github.com/kalkih/mini-graph-card)
- [Room Summary Card](https://github.com/homeassistant-extras/room-summary-card)
- [card-mod](https://github.com/thomasloven/lovelace-card-mod)
- [Navbar Card](https://github.com/joseluis9595/lovelace-navbar-card)
- [Kiosk Mode](https://github.com/NemesisRE/kiosk-mode) (hides the default header and sidebar)

You also need Python 3.10+ on any machine that can reach Home Assistant.

### Steps

1. **Create a long-lived access token.** Go to your profile → **Security**, scroll to
   **Long-lived access tokens**, create one and save it to a file, e.g. `~/.config/lumen/ha-token`.
   You can also export it as `HA_TOKEN`.
2. **Set up floors and areas** in **Settings → Areas, labels & zones**. Add a picture to each area:
   it becomes the room photo.
3. **Optional: create an "All lights" group** for the quick-settings tile:
   **Settings → Devices & services → Helpers → Create helper → Group → Light group**.
4. **Describe your home:**

   ```bash
   git clone https://github.com/vicvancooten/lumen-ha.git
   cd lumen-ha/dashboard
   pip install -r requirements.txt
   cp house.example.yaml house.yaml   # house.yaml is git-ignored
   ```

   Edit `house.yaml`. Room keys are your HA **area IDs**, floor `id`s are your HA **floor IDs**,
   and every entity list takes plain IDs or `{entity, name, icon, color}` objects.
   Everything except `floors` and `rooms` is optional. The example file documents each section.

5. **Generate:**

   ```bash
   python3 build_dashboard.py house.yaml --dry-run   # writes home-dashboard.json, touches nothing
   python3 build_dashboard.py house.yaml             # creates/updates the dashboard in HA
   ```

   The generator creates the dashboard if it doesn't exist yet and adds the Inter font as a dashboard
   resource. Re-run it whenever you add devices or rooms.

### BirdNET-Go (optional)

If you run [BirdNET-Go](https://github.com/tphakala/birdnet-go) (e.g. as a Home Assistant app), the home
page can show the last bird heard, with its photo and confidence, plus today's species and detection counts
and the most-heard bird.

1. Copy `ha/birdnet_go.yaml` to `/config/packages/birdnet_go.yaml` (see the comments in the file for the
   `packages:` line and the host to edit), and restart Home Assistant. It uses plain REST sensors against
   BirdNET-Go's API, so no MQTT broker is needed.
2. Add the `birdnet:` section from `house.example.yaml` to your `house.yaml` and regenerate.

The block is a full-width hero. With `camera:` set it shows the live camera with the last bird (photo,
confidence, time ago) laid over it; add `door_camera:` for a second camera beside it (live while the
doorbell is active, the last visitor otherwise). Tapping a bird opens a **Birds** page with the Saezuri
collage embedded, today's species and the latest detections; `room:` adds the same detail to a room page.

The collage is embedded through Home Assistant's ingress. The browser needs an `ingress_session` cookie for
that, which the frontend normally sets only when you open the app's own panel, so the generator's navbar
refreshes one in the background (at most every 20 minutes). The app's ingress URL changes when it is
reinstalled, so regenerate the dashboard after that.

### Game consoles (optional)

With the [PlayStation Network](https://www.home-assistant.io/integrations/playstation_network/) integration, a
console in a game shows up wherever the TVs do: on the home page's **Now playing** (in place of the TV it's
plugged into), on its room's page and on the **Media** page, as the game's title over its art with how long it's
been on. Name the account's `psn` prefix and the Media page also gets the profile (avatar, online status, trophy
level) and the trophy counts. Add a `consoles:` list under `media:` (see `house.example.yaml`) and regenerate.

### LED strips (optional)

A [WLED](https://www.home-assistant.io/integrations/wled/) strip listed under a room's `strips:` gets its own
section on the room page: the strip with its brightness and what it's playing, its favourites (WLED presets) as
big swatches in their own colours, the one it's on glowing, and any switches that go with it (follow the music,
a length slider). Favourites are coloured and iconed by what they're called (Fireplace, Aurora, Movie...), or by
`colors:` and `icon:` per favourite. See `house.example.yaml`.

### Keeping hand-made pages (optional)

Regenerating replaces the whole dashboard, so anything added in the HA editor is lost. Export what you
want to keep to a JSON file and name it under `overlay:` in `house.yaml`: whole views (e.g. a Cameras
page) and sections inserted into generated views. See `house.example.yaml` for the format.

### Using the dashboard

- **Bottom bar:** Home, one tab per floor, and **More** (Edit dashboard, Energy, History, Map,
  Settings, Profile & theme, Show sidebar). A floor's tab stays highlighted inside its rooms.
- **Top bar:** page name, back (on room pages), search, Assist and notifications.
- **Room pages:** the room's photo is the page background, fading into the page colour as you scroll, with the room name large on top.
- **Room cards:** tap to open the room, and tap a device icon to toggle it. The card lights up when a light
  is on and gets a green border while there's motion.
- **Get the normal HA interface back:** use **More → Show sidebar**, or add `?disable_km` to any dashboard URL.
  To keep HA's own header and sidebar permanently, set `dashboard.kiosk: false`.

### Halloween (optional)

Add a `halloween:` section to `house.yaml` (see `house.example.yaml`) and, from mid-October to just after
Halloween, the dashboard gets a **Halloween** tab and page, a Halloween row on each room page that suits it, a
countdown on the home page and a few easter eggs. Outside that window all of it hides itself.

- **Scenes:** every room with colour or white-spectrum lights gets spooky looks (Pumpkin patch, Blood moon,
  Ghost light, Witch's brew, Séance, Watching eyes, depending on what its lights can do; the room in `room:` gets
  them all), and the whole house gets **Haunted house**. WLED strips join in with matching effects.
- **Back to normal:** the first Halloween scene in a room remembers how its lights were, and **Normal** (per
  room) or **Back to normal** (everything) puts them back. Lights that end up off forget their Halloween colour,
  so they don't come back on blood red, and the first time they're switched on again they get a proper
  brightness. At night (Night mode, and 05:00) and when the season ends, the house tidies up on its own.
- **Shows:** a song through Music Assistant (Spooky Scary Skeletons, This Is Halloween, Danse Macabre, Thriller,
  John Carpenter's Halloween theme) with a light show to match in the `room:`, or a thunderstorm whose flashes
  line up with the thunder. Tap to start, tap again to stop; the lights go back as they were afterwards.
- **Pranks** (a switch on the page, off by default): the doorbell says trick or treat and the hall flickers, a
  door creaks with a light flickering, a light flickers now and then when you walk in, and whatever is still on
  at three in the night after Halloween flickers.
- **Easter eggs:** find them.

The generator writes `halloween.yaml` next to the dashboard JSON: a Home Assistant
[package](https://www.home-assistant.io/docs/configuration/packages/) with the season sensor, scenes, scripts and
automations. Copy it to `/config/packages/halloween.yaml` and reload all YAML. It uploads the thunderstorm
(synthesised, so nothing to license) to the `sounds.folder` in HA's local media.

**Cleaning up:** delete `/config/packages/halloween.yaml` and reload, delete the sounds folder under Media, and
remove `halloween:` from `house.yaml` and regenerate. Until you do, it just sleeps until next October.

## Troubleshooting

- **Cards show "Custom element doesn't exist":** one of the required HACS cards is missing. Install it, then hard-refresh the browser.
- **Rooms missing:** the room key must match an HA area ID, and its `floor` must match a floor `id` in `floors`.
- **`WARNING: entities not found`:** the generator lists entity IDs from `house.yaml` that don't exist in HA.
- **Theme not in the list:** reload themes (Developer tools → YAML) and check the `frontend: themes:` include above.
- **Navbar overlaps the phone's gesture bar:** update navbar-card, and make sure the companion app runs edge-to-edge.

## License

MIT, see [LICENSE](LICENSE).
