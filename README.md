# Pepper Teleoperation & Behaviors

A small Python toolkit for driving a **SoftBank Pepper** robot from a laptop:
keyboard teleop with a live head-camera feed, a "follow me" behavior, voice
commands, tablet control, and a few emotes. Talks to the robot over NAOqi
(`qi`) on port **9559** — no SSH login required for the core apps.

Confirmed against **NAOqi 2.5.10.7** (classic Pepper).

---

## What's in here

| File | Purpose |
|------|---------|
| `pepper_diag.py` | **Run this first.** Connects, prints the NAOqi version, checks required services, and makes Pepper say hello. |
| `pepper_teleop.py` | Main app: keyboard drive + live camera in a pygame window, arms/head/hands, voice commands, tablet messages, in-app menu. |
| `pepper_features.py` | Robot-side actions (follow-me, face characteristics, emotes, LEDs, tablet hosting) imported by the teleop app. Not run directly. |
| `pepper_navigation.py` | Mapping & remembered-spots via ALNavigation (explore an area, remember spots, drive back to them). Imported by the teleop app. Not run directly. |
| `pepper_follow_me.py` | Standalone "Pepper follows a person and talks" behavior. |
| `pepper_settings.py` | Opens Pepper's on-tablet Settings menu (Wi-Fi, volume, language) — no login needed. |
| `pepper_autostart.py` | Enable/disable an installed Choregraphe behavior to run at boot. |
| `deploy_67.sh` | Uploads the `assets/67.gif` emote to Pepper's own web server (needs SSH). |
| `assets/` | `67.gif` + `67.html` served on the tablet by the "67" emote. |

---

## 1. Prerequisites

- A **Pepper robot** powered on, with its head on the **same network** as your laptop.
- **Python 3** on the laptop.
- Wi-Fi **without client isolation.** School/corporate/guest networks often block
  device-to-device traffic on port 9559. If you can't connect, put Pepper and the
  laptop on your **own router** (or a phone hotspot).

### Find Pepper's IP

Press the **chest button** once — Pepper says its IP address aloud. You'll pass
this as `--ip <PEPPER_IP>` to every script.

---

## 2. Setup

Create a virtual environment and install the two dependencies. The NAOqi Python 3
bindings ship as the `qi` package on PyPI — no separate pynaoqi SDK download needed.

```bash
python3 -m venv ~/pepper-venv
source ~/pepper-venv/bin/activate

pip install qi pygame
```

Activate the venv in every new shell before running a script:

```bash
source ~/pepper-venv/bin/activate
```

---

## 3. Verify the connection

Always start with the diagnostic. It confirms your setup, network path, and the
robot's firmware, then greets you out loud:

```bash
python pepper_diag.py --ip <PEPPER_IP>
```

Expected output ends with a spoken "Connection successful. I am ready." and a list
of available services. If it can't connect:

- `ping <PEPPER_IP>` — is the robot reachable at all?
- Double-check the IP (press the chest button again).
- Port **9559** may be blocked by the network — switch to your own router.

---

## 4. Run the teleop app

```bash
python pepper_teleop.py --ip <PEPPER_IP>
```

A resizable pygame window opens showing the head camera. It captures the keyboard.

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--ip` | *(required)* | Pepper's IP address |
| `--port` | `9559` | NAOqi port |
| `--camera` | `0` | `0` = top camera, `1` = bottom |
| `--vga` | off | 640×480 feed (default is 320×240, smoother) |
| `--fps` | `15` | Requested camera frame rate |

### Controls (summary)

```
W/S/A/D ...... drive (forward/back, strafe)       Q/E ..... rotate
SHIFT ........ boost to full speed
CTRL+WASD/QE . move the HEAD instead of the base   G ....... recenter head
H/J .......... left arm up/down    L/K ... right arm up/down
O/P .......... open/close both hands
ENTER ........ type a message -> shows on tablet + Pepper speaks it
CTRL+M ....... open menu (follow-me, voice commands, dialogue, emotes, navigation, LEDs)
[ / ] ........ speed scale down/up   1/2/... select camera   C cycle camera
F ............ fullscreen   SPACE emergency stop   ESC quit
```

The full, authoritative control list lives in the docstring at the top of
`pepper_teleop.py`.

> **Note:** On startup the teleop disables Pepper's autonomous life (person
> tracking, idle head movement) so it doesn't fight your manual control, and it
> lowers the base's safety-margin so teleop actually moves. Both are **restored on
> exit** (press `ESC` to quit cleanly rather than killing the process).

### Mapping & remembered spots (Navigation menu)

Open the menu (`CTRL+M`) → **Navigation (map & spots)…** to map a space with
ALNavigation and return to spots later:

1. **Explore & map area** — Pepper drives itself around and builds a metrical map
   (a few minutes; keep the area clear). The **radius** (cycle it with *Map radius*)
   caps how far from the start it maps — bump it to `8–12 m` and start from the
   *centre* of the space for a bigger map. When it finishes, the map is auto-saved
   on the robot and localization stays running. *Voice: "explore the area".*
2. **Remember this spot** — drive Pepper somewhere (or let it be), then save its
   current position in the map under a name (`spot 1`, `spot 2`, …). *Voice: "remember
   this spot".*
3. **Go to spot…** — pick a remembered spot and Pepper navigates back to it,
   avoiding obstacles.
4. **Load current map & localize** — in a later session, reload the current map so
   you can return to spots again. Place Pepper where it started mapping first — it
   relocalizes at the map origin.

**Multiple maps (one per room).** ALNavigation builds one map per explore run and
can't stitch runs together, so cover a big/multi-room space as *separate* maps:

- **Map: … (N maps)** opens the maps list. Pick a map to **switch** to it (drive
  Pepper to that room's start point first — switching reloads and relocalizes it),
  or **+ New map** to start a fresh one that the next *Explore* fills. *Voice: "new
  map".* The active map is marked `*`.
- Explore / Remember / Go-to / Forget all act on the **current** map only, so each
  room keeps its own map and its own spots.

Everything is saved to `pepper_map.json` next to the app (git-ignored — it's
robot-specific runtime state; an older single-map file is migrated automatically
into `map 1`). While exploring or navigating, ALNavigation owns the base, so
keyboard/voice driving is paused (HUD shows `NAV`); pick **Stop navigation** (or
say "stop navigation") to abort and take back control.

> **Note:** Mapping and navigation need `ALNavigation`, which is present on
> Pepper's NAOqi. Dead-reckoning drift makes a single map less reliable the larger
> it gets (~5–8 m is the sweet spot) — for anything bigger, prefer several smaller
> per-room maps over one large one, and re-explore if a map goes stale.

---

## 5. Follow-me behavior (standalone)

Pepper tracks a person and walks to keep a set distance, greeting people as they
arrive:

```bash
python pepper_follow_me.py --ip <PEPPER_IP>
python pepper_follow_me.py --ip <PEPPER_IP> --distance 0.5
python pepper_follow_me.py --ip <PEPPER_IP> --target Face --language English
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--distance` | `0.6` | Metres Pepper tries to keep from the person |
| `--target` | `People` | `People` or `Face` detection |
| `--language` | robot default | TTS language |

Stop with **Ctrl-C** — Pepper stops moving, drops tracking, sits, and rests.

---

## 6. Tablet & configuration helpers

**Open the on-tablet Settings menu** (change Wi-Fi / volume / language by hand, no
login needed):

```bash
python pepper_settings.py --ip <PEPPER_IP>
```

**Deploy the "67" GIF emote** to Pepper's web server so the tablet serves it
locally/offline. This is the one script that needs SSH — login is `nao@<ip>`,
password `pepper`:

```bash
./deploy_67.sh <PEPPER_IP>
```

The teleop app auto-deploys this asset on startup; run the script manually only if
that fails.

---

## 7. Auto-run a behavior at boot (optional)

To make an **installed** Choregraphe behavior run on the tablet at startup (no
laptop, no router — the tablet talks to NAOqi internally):

```bash
# List installed behaviors (* = already runs at boot)
python pepper_autostart.py --ip <PEPPER_IP> --list

# Enable / disable
python pepper_autostart.py --ip <PEPPER_IP> --enable  followme/behavior_1
python pepper_autostart.py --ip <PEPPER_IP> --disable followme/behavior_1
```

The behavior must already be **Packaged & Installed** from Choregraphe first —
Live-Play behaviors are not installed and won't appear in `--list`.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `couldn't import qi` | Activate the venv: `source ~/pepper-venv/bin/activate` |
| `couldn't import pygame` | `pip install pygame` inside the venv |
| `can't reach NAOqi` | Same Wi-Fi? Correct IP? Port 9559 blocked by network isolation → use your own router |
| Head snaps back / fights you | Expected if autonomous life re-enables; the teleop app disables it — quit with `ESC` to restore cleanly |
| Robot barely moves | The safety bubble caps base speed (~0.35 m/s); teleop already lowers the margin |
| `deploy_67.sh` auth fails | SSH login is `nao@<ip>`, password `pepper` |

---

## Configuration reference

- **NAOqi port:** `9559` (default everywhere; override with `--port`)
- **SSH:** `nao@<PEPPER_IP>`, password `pepper` (only `deploy_67.sh` uses it)
- **Tuning constants** (speeds, joint limits, security margins, voice-command
  timings) live at the top of `pepper_teleop.py` and `pepper_follow_me.py` — edit
  there if you need different behavior.
