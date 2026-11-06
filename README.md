# Pepper MCP

**Gemini Live drives a SoftBank Pepper** (NAOqi 2.5) in real time: it hears and sees through
Pepper's own head microphone and camera, talks through its speakers, and moves the body through
an MCP tool server that runs inside the same process.

* **Every robot feature as a tool** – speech with gestures, camera snapshots,
  people/face/touch/sound perception, base and joint motion, animations and custom gestures
  (plus a 100-animation Choregraphe library and 11 whole-song dances with music), LEDs, tablet, tic-tac-toe, following, navigation and maps, behaviours. 107 tools and
  10 resources (no prompts), listed in [`docs/MCP_ENDPOINTS.md`](docs/MCP_ENDPOINTS.md);
  Gemini is offered 94 of the tools.
* **Real-time by design** – the robot pushes audio and ALMemory events to the laptop. The
  microphone goes straight to Gemini Live and the voice comes back to the speakers; robot
  events reach it as short notes. Design notes in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
* **Reflexes** – a high five when somebody raises a hand, eyes and head that follow whoever is
  talking, a head tilt when touched: sub-second reactions that run on the server without the
  LLM. All toggleable (a greeting wave exists too, off by default because Gemini greets).
* **Works without a robot** – `--fake` runs the whole stack against an in-memory NAOqi so
  you can check the install first.

This branch contains only the server and its voice agent. The keyboard teleop lives on `main`.

**Contents:** [Quick start](#quick-start-everything-already-set-up) ·
[Part 1 – Install on the laptop](#part-1--install-on-the-laptop) ·
[Part 2 – Network](#part-2--network-laptop--router-by-ethernet) ·
[Part 3 – Bring up Pepper](#part-3--bring-up-pepper) ·
[Configuration](#configuration) ·
[Persona, language and vocabulary](#persona-language-and-vocabulary) ·
[Project layout](#project-layout) · [Development](#development) ·
[Troubleshooting](#troubleshooting)

---

## Quick start (everything already set up)

```bash
# 1. power the router, plug the laptop's Ethernet cable into it
# 2. close Pepper's charging hatch, power Pepper on, wait ~3 min,
#    press the chest button once → it says its IP
cd pepper-controller && source .venv/bin/activate     # the cloned repo + its venv (step 3)
export PEPPER_IP=192.168.178.25                       # the IP Pepper just said
ping -c 2 "$PEPPER_IP" && nc -zv "$PEPPER_IP" 9559
pepper-mcp-verify --ip "$PEPPER_IP" --no-move         # optional sanity check
pepper-mcp-gemini --ip "$PEPPER_IP" --preview-port 8765 --liveliness off
```

New laptop? Do **Part 1** and **Part 2** once. New router? Redo **Part 2** and step 10.

**The setup this guide assumes:**

```
 internet ── your normal Wi-Fi ─────── Wi-Fi ┐
                                             laptop / Mac
 Pepper ~~ Wi-Fi ~~ Pepper's router ── Ethernet cable ┘
```

Pepper has its own router (no internet needed). The laptop is plugged into that router with an
**Ethernet cable**, and keeps using its normal Wi-Fi for the internet (Gemini runs in the cloud).
Pepper itself joins the router's Wi-Fi. Every step below has a **Linux** and a **macOS** part
where the two differ; do only the one for your machine.

**All steps at a glance:**

| Part | Step | What | How often |
|---|---|---|---|
| 1 Install | [1](#1-what-you-need) | Check you have everything | once |
| | [2](#2-system-packages) | Install `git`, `uv`, `nmap` | once per laptop |
| | [3](#3-clone-the-repo-and-create-the-virtual-environment) | Clone the repo, create `.venv`, install | once per laptop |
| | [4](#4-add-the-gemini-api-key) | Put the Gemini key in `.env` | once per laptop |
| | [5](#5-test-without-the-robot) | Test with the fake robot | once per laptop |
| 2 Network | [6](#6-connect-the-laptop-to-the-router-by-ethernet) | Cable to the router, internet stays on Wi-Fi | once per laptop |
| | [7](#7-set-up-the-router) | Router Wi-Fi for Pepper, isolation off | once per router |
| | [8](#8-let-the-robot-through-the-laptops-firewall) | Firewall / macOS Local Network | once per laptop |
| 3 Pepper | [9](#9-power-on) | Power Pepper on | every session |
| | [10](#10-first-time-only-put-pepper-on-the-routers-wi-fi) | Put Pepper on the router's Wi-Fi | once per router |
| | [11](#11-get-peppers-ip-and-check-the-connection) | Get Pepper's IP, `ping` it | every session |
| | [12](#12-hardware-checklist) | `pepper-mcp-verify` | first time, or when something is off |
| | [13](#13-start-gemini-live) | `pepper-mcp-gemini` | every session |
| | [14](#14-watch-and-test-it) | Dashboard and first tests | – |
| | [15](#15-shut-down) | Stop and shut down | every session |

---

## Part 1 – Install on the laptop

No robot needed for this part.

### 1. What you need

| Item | Notes |
|---|---|
| **Linux:** x86_64 laptop (Arch or Ubuntu 22.04 / 24.04 tested) | ARM Linux (e.g. Raspberry Pi) can only run `--fake`: there is no `qi` wheel for it |
| **macOS:** Apple-silicon Mac (M1–M4), macOS 13+ | Intel Macs have no `qi` wheel. A MacBook needs a USB-C Ethernet adapter; a Mac mini has a port built in |
| Internet on the laptop (Wi-Fi) | Gemini Live runs in the cloud |
| A Gemini API key | free from [Google AI Studio](https://aistudio.google.com/apikey) |
| A router for Pepper + an Ethernet cable | any home router; it does not need internet |
| Pepper with NAOqi 2.5.x, charged | plug the charger into the back of the base; unplug it before driving |
| ~2 m × 2 m of clear floor | Pepper moves its base during the checks and when Gemini decides to walk |

You do **not** need to install Python: `qi` (the NAOqi client) needs Python 3.12, and
[`uv`](https://docs.astral.sh/uv/) downloads a private copy just for this project.

### 2. System packages

**Arch Linux**

```bash
sudo pacman -Syu --needed git openssh uv openbsd-netcat nmap avahi nss-mdns
```

**Ubuntu / Debian**

```bash
sudo apt update
sudo apt install -y git curl openssh-client netcat-openbsd nmap avahi-utils
curl -LsSf https://astral.sh/uv/install.sh | sh     # uv, into ~/.local/bin
source ~/.local/bin/env                             # or open a new terminal
```

**macOS** (`ssh`, `nc` and `ping` ship with macOS)

```bash
xcode-select --install          # git and compilers; skip if `git --version` already works
# Homebrew, if `brew --version` fails (https://brew.sh):
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile && eval "$(/opt/homebrew/bin/brew shellenv)"
brew install uv nmap
```

Check: `git --version` and `uv --version` both print a version.

### 3. Clone the repo and create the virtual environment

The repo does **not** contain a `.venv` folder: you create it here, once, after cloning.

```bash
git clone -b mcp-server https://github.com/TechInnovationZurichAcn/pepper-controller.git
cd pepper-controller
uv venv --python 3.12 .venv          # creates .venv/ (downloads Python 3.12 if needed)
source .venv/bin/activate            # your prompt now starts with (pepper-controller) or (.venv)
uv pip install -e ".[all,dev]"       # installs qi, google-genai and the pepper-mcp-* commands
```

Check: `which pepper-mcp-gemini pepper-mcp-verify` prints two paths inside `.venv/bin/`.

> **Every new terminal:** `cd pepper-controller && source .venv/bin/activate` first. Without
> it you get `command not found` for `pepper-mcp-gemini` / `pepper-mcp-verify`. Always run the
> commands from the repo root: the `.env` key file is looked up in the current folder.

It must be Python **3.12** (`qi` has no wheels for 3.13+, and on macOS only for 3.12).

### 4. Add the Gemini API key

```bash
echo 'GEMINI_LIVE_KEY=<your key>' > .env && chmod 600 .env    # .env is git-ignored
```

Only the key is read from `.env`. Other settings are flags or exported `PEPPER_*` variables
(see [Configuration](#configuration)).

### 5. Test without the robot

```bash
pytest -q                                                 # whole test suite, fake robot (~1–2 min)
pepper-mcp-gemini --fake --preview-port 8765              # fake robot, real Gemini session
```

Open <http://localhost:8765/> (dashboard), `Ctrl-C` to stop. If this fails, the problem is the
install or the key, not the robot.

---

## Part 2 – Network: laptop ↔ router by Ethernet

Do this once per laptop and router. The robot opens connections **back to** the laptop
(microphone audio, events), so the router must not isolate its clients, and the laptop's
firewall must let the robot in.

### 6. Connect the laptop to the router by Ethernet

The router has no internet, so the laptop must use the cable **only** to reach the router and
Pepper, and keep sending everything else over Wi-Fi. Set this up **before** plugging the cable
in. Do **not** join the laptop's Wi-Fi to Pepper's router: its Wi-Fi stays on your normal
network.

**Linux** (NetworkManager, the default on Ubuntu Desktop and most Arch desktops). Create a
profile that takes an address from the router but never its default route or DNS:

```bash
nmcli con add type ethernet con-name pepper-router \
  connection.autoconnect yes connection.autoconnect-priority 100 \
  ipv4.method auto ipv4.never-default yes ipv4.ignore-auto-dns yes ipv6.method disabled
```

It has no interface name, so it works with any Ethernet port or USB adapter, and its priority
beats the automatic "Wired connection N" profiles. Now power the router, plug the cable into one
of its **LAN** ports (not WAN / Internet) and check:

```bash
ip -br link                                  # the Ethernet device that is UP, e.g. enp3s0 / enx…
nmcli con up pepper-router ifname <ETH_DEV>  # usually already up on its own
ip -br addr show <ETH_DEV>                   # an address from the router, e.g. 192.168.178.20/24
ip route get 1.1.1.1                         # → dev wl…   (internet over Wi-Fi)
ip route get 192.168.178.1                   # the router's address → dev <ETH_DEV>
```

If `ip route get 1.1.1.1` says `dev <ETH_DEV>`, another wired profile won: run
`nmcli con down "Wired connection N"` and `nmcli con up pepper-router` again. Side effect: on
other wired networks this profile also keeps internet on Wi-Fi; there, use
`nmcli con up "Wired connection N"` if you want a wired uplink.

<details>
<summary>Linux edge cases: no link light, OWC dock, systemd-networkd</summary>

* **No link light / `ethtool <ETH_DEV>` says `Link detected: no`:** swap the cable or router
  port. With an **OWC dock**, the laptop must be connected to the dock's USB-C port marked with
  the **laptop icon**; through any other port the dock's Ethernet never gets a link.
* **A `default via … dev <ETH_DEV>` route with `metric 1024`**, or `resolvectl status <ETH_DEV>`
  showing the router as DNS with `Default Route: yes`: `systemd-networkd` also manages the cable
  (`systemctl is-active systemd-networkd`). Give the adapter its own file, matched by MAC
  (`ip -br link`), that keeps gateway and DNS off it:

  ```ini
  # /etc/systemd/network/10-pepper.network  (sorts before any catch-all "enp*" file)
  [Match]
  MACAddress=<ADAPTER_MAC>

  [Network]
  DHCP=ipv4
  DNSDefaultRoute=no
  IPv6AcceptRA=no

  [DHCPv4]
  UseGateway=no
  UseDNS=no
  UseDomains=no
  ```

  then `sudo networkctl reload && sudo networkctl reconfigure <ETH_DEV>`.
* **No NetworkManager** (`nmcli: command not found`, e.g. `iwd` or netplan): configure the
  equivalent there: DHCP on the cable, but no default route and no DNS from it.

</details>

**macOS.** Power the router and plug the cable into one of its **LAN** ports (not WAN /
Internet); macOS takes an address from the router by itself. To keep the internet on Wi-Fi:
*System Settings → Network → ⋯ (bottom of the list) → Set Service Order…* and drag
**Wi-Fi above** the Ethernet / USB LAN service. Then check:

```bash
networksetup -listallhardwareports   # which enX is Ethernet and which is Wi-Fi
ipconfig getifaddr <ETH_DEV>         # e.g. en0 on a Mac mini → an address from the router
route -n get 1.1.1.1                 # interface: must be the Wi-Fi device
route -n get 192.168.178.1           # the router's address → interface: the Ethernet device
```

(Mac mini: `en0` is Ethernet, `en1` Wi-Fi. MacBook: `en0` is Wi-Fi, the adapter is a higher
`enX`.)

The router's address is on its label (FRITZ!Box: `192.168.178.1`; others often `192.168.0.1` or
`192.168.1.1`); use yours in the `route get` lines.

### 7. Set up the router

Open the router's admin page in a browser (its address and login are on the label), then:

1. Set a Wi-Fi name and a WPA2 password for Pepper (2.4 GHz is the safe choice).
2. Turn **off** "AP isolation" / "client isolation", and do not use the guest network.
3. Its LAN subnet must not overlap your normal Wi-Fi's (a `192.168.x.0/24` is usually fine).
4. Optional: once Pepper shows up in the DHCP client list (step 10), add a reservation so its
   IP stays the same.

### 8. Let the robot through the laptop's firewall

The robot calls back into the laptop on a random TCP port (audio stream, events). A firewall
that blocks it makes hearing **fail silently**. Allow the router's whole subnet once, so it
keeps working whatever IP Pepper gets. The subnet is your laptop's cable address with the last
number replaced by `0/24` (e.g. `192.168.178.20` → `192.168.178.0/24`).

**Linux.** Find out which firewall you have:

```bash
sudo ufw status                 # Ubuntu ships ufw; "inactive" = nothing to do
sudo firewall-cmd --state       # "not running" / command not found = nothing to do
```

| Firewall | Allow the router's subnet | Undo |
|---|---|---|
| `ufw` (active) | `sudo ufw allow from 192.168.178.0/24` | `sudo ufw status numbered`, then `sudo ufw delete <n>` |
| `firewalld` | `sudo firewall-cmd --permanent --zone=trusted --add-source=192.168.178.0/24 && sudo firewall-cmd --reload` | the same with `--remove-source=` |
| none (default on a fresh Arch install) | nothing to do | – |

Raw `nftables`/`iptables`: allow inbound TCP from the subnet, or pin the callback port with
`PEPPER_LISTEN_URL=tcp://0.0.0.0:9560` and open only that.

**macOS.** Two separate permissions:

1. **Local Network (macOS 15+).** The first time your terminal app reaches the robot (step 11),
   macOS asks whether it may "find and connect to devices on your local network": click
   **Allow**. Missed it? *System Settings → Privacy & Security → Local Network* → enable your
   terminal app, then restart the terminal. Without it, connections fail with `No route to host`.
2. **Firewall** (*System Settings → Network → Firewall*, off by default). If it is on, allow
   the venv's Python, or the robot's audio is dropped silently:

   ```bash
   PY="$(realpath .venv/bin/python)"
   sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add "$PY"
   sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp "$PY"
   ```

   Clicking **Allow** on the "accept incoming network connections?" dialog does the same.
   Re-run after recreating the venv.

To open the dashboard from another device (phone) also allow TCP 8765
(`sudo ufw allow 8765/tcp`, or `sudo firewall-cmd --add-port=8765/tcp`).

---

## Part 3 – Bring up Pepper

Everything here is the same on Linux and macOS.

### Robot parts you will touch

* **Chest button** – just below the tablet. Short press: power on, or (when running) say
  the IP. Hold ~3 s: shut down.
* **Emergency stop** – the button at the back of the neck. Press it if Pepper does anything
  unsafe: the motors stop and the robot goes to a safe rest. Release it, then the server's
  `wake_up` tool (or a server restart) brings the motors back.
* **Charging hatch** – the flap over the charger socket at the back of the base. **It must be
  closed for Pepper to move:** while it is open NAOqi locks the wheels, so driving, turning and
  following do nothing (arms, head and gestures still work). NAOqi reports it as a "Safety" stop,
  which looks like an obstacle; the server reads the hatch sensor instead and answers base
  commands with "charging hatch is open", shows `charging_hatch_open` in `robot_status` and
  warns at start. `pepper-mcp-verify` checks it too.
* **Ethernet port** – under the cover on the back of the head (only needed in step 10).

### 9. Power on

1. Close the charging hatch and unplug the charger.
2. Press the chest button once (short). The eyes and shoulders light up and the tablet
   boots.
3. Wait ~2–3 minutes. Boot is finished when Pepper lifts its head, stands up straight and
   its eyes light up.

If Pepper stays slumped forward after boot it is in rest mode – that is fine, the server
wakes it on start.

### 10. First time only: put Pepper on the router's Wi-Fi

Skip this if Pepper already knows the router (pressing the chest button tells you an IP in the
router's subnet). Pepper remembers the network and rejoins it on every boot.

1. Plug Pepper's head Ethernet port into another LAN port of the router.
2. Find its wired IP (`<WIRED_IP>` below): press the chest button, or look in the router's client list, or
   `nmap -p 9559 --open <subnet>` (e.g. `192.168.178.0/24`).
3. Join the Wi-Fi with connman over SSH (password `pepper` unless it was changed; type `yes`
   the first time SSH asks about the host key).
   **WPS push-button** avoids typing the key (FRITZ!Box: press *Connect/WPS* ~1 s until the
   WLAN LED blinks; you then have ~2 minutes):

   ```bash
   ssh nao@<WIRED_IP> connmanctl services      # find the router's id: wifi_…_managed_psk
   ssh -t nao@<WIRED_IP> connmanctl            # interactive shell on the robot, then:
   connmanctl> agent on
   connmanctl> connect wifi_…_managed_psk
   Passphrase?                                 # WPS: press the router button, then just Enter
                                               # here and at the WPS PIN prompt (empty = push-button);
                                               # or type the Wi-Fi key here
   connmanctl> services                        # "*AR <SSID>" = favourite + connected
   connmanctl> quit
   ```

   `invalid-key` means the router rejected the key: use the *Wi-Fi network key* from the label
   (FRITZ!Box: "WLAN-Netzwerkschlüssel"), not the admin password, or use WPS. "In progress"
   means an earlier attempt is still pending: `disconnect <id>` first. The robot's web page
   (`http://<WIRED_IP>/` → *Network settings*, user `nao`) or the tablet's settings work too.
4. Unplug Pepper's cable. Its Wi-Fi IP is **different** from the wired one: press the chest
   button again (or `ssh nao@<WIRED_IP> ip -4 -o addr show wlan0` before unplugging).

### 11. Get Pepper's IP and check the connection

Press the chest button once: Pepper says "… my internet address is 192.168.178.25". The router
hands it out by DHCP, so it can change between sessions: ask each time. (Pepper says it is
not connected → step 10.) Other ways: the router's client list, `nmap -p 9559 --open <subnet>`,
or Zeroconf (`dns-sd -B _naoqi._tcp` on macOS; on Linux `avahi-browse -rt _naoqi._tcp`, which
needs `sudo systemctl enable --now avahi-daemon` once).

Put the IP into a variable, so the commands below can be copied as they are (do this in every
new terminal, after `source .venv/bin/activate`):

```bash
export PEPPER_IP=192.168.178.25     # the IP Pepper said
ping -c 3 "$PEPPER_IP"
nc -zv "$PEPPER_IP" 9559            # NAOqi port: must say succeeded / open
```

* `ping` fails → wrong IP (ask Pepper again), or the cable / step 6 is not right.
* `ping` works but port 9559 does not → client isolation is still on in the router (step 7).

### 12. Hardware checklist

Make sure no `pepper-mcp-gemini` is running (only one program can use Pepper at a time). Clear
~1 m in front of Pepper (one check drives the base forward for 0.3 s), then:

```bash
pepper-mcp-verify --ip "$PEPPER_IP"       # --no-move skips the drive, --quiet skips speech
```

You want `[OK ]` on at least `connect`, `tts`, `audio_callback` (≈ 6 callbacks/s), `camera`
and `move`. A `FAIL` on `audio_callback` almost always means the firewall (step 8) or client
isolation (step 7); if it says `"PepperMcpAudio" … is already registered`, a
`pepper-mcp-gemini` is still running somewhere: stop it first. What every check proves and its fallback:
[`docs/ROBOT_VERIFICATION.md`](docs/ROBOT_VERIFICATION.md).

Playing sound files and showing pages on the tablet copies files onto the robot over SSH
as user `nao`, password `pepper` (the factory default). With `--preview-port` the server
also starts a small relay on the head over SSH, because the tablet cannot reach the laptop
directly. If your robot's password was changed, export `PEPPER_SSH_PASSWORD=<password>` (and
`PEPPER_SSH_USER` if needed) before starting the server.

Tested from Linux laptops and a Mac mini (macOS, with `--no-move --quiet`).

### 13. Start Gemini Live

```bash
pepper-mcp-gemini --ip "$PEPPER_IP" --preview-port 8765 --liveliness off   # key from step 4
```

Pepper wakes up, stands, and after a few seconds the terminal says `Pepper is live …`, followed
by the dashboard address (`Dashboard: http://localhost:8765/ …`). Then just talk to it. Started
right after Pepper booted, it first waits (up to 90 s for NAOqi to answer, then up to 120 s for
NAOqi to register speech, audio, camera and tablet) and says so in the log. The terminal shows `[heard]` (what Gemini understood), `[said]` (its answer)
and `[tool]` (body actions). `Ctrl-C` stops it (step 15).

Good to know:

* **Talk after Pepper has finished.** The mic sits next to the speakers, so it is muted while
  Pepper makes any sound; interrupting mid-sentence does not work. Stand within about 1 m.
* **Persona:** what Pepper says and how it behaves comes from [`persona.txt`](persona.txt)
  (plus [`knowledge.txt`](knowledge.txt)). The shipped one is German and written for one event
  (children visiting an office): **adapt it before using Pepper elsewhere**
  ([Persona, language and vocabulary](#persona-language-and-vocabulary)).
* **One program at a time:** stop any other `pepper-mcp-gemini` (and don't run
  `pepper-mcp-verify`) while it runs.
* Model, voice and every other option: [Configuration](#configuration).

<details>
<summary>What it does in detail</summary>

On start it wakes Pepper, turns autonomous life off, applies the liveliness preset and lowers
the base safety distances to 0.15 m / 0.05 m (restored on shutdown).

A speech-to-speech model hears Pepper's own head microphone (front, or the next working one),
watches its head camera (1 fps), answers through Pepper's loudspeakers and drives the body
through the MCP tools in the same process: 94 of the 107 (`say`, the speech and hearing tools,
`recognize_keywords`, `wait_for_event` and the other blocking ones are left out, because Gemini
is the voice and the ears). Actions run in the background while it keeps talking and report back
silently, and turns that would only say "no speech" or a gesture name are kept off the speakers.
Robot events (touch, people appearing, leaving or waving, `motion.blocked`, failed tasks,
high-five results, losing the person it follows, a fall, low battery, tic-tac-toe moves) are
passed to it as short notes; it reacts with its body and speaks only when a person would, without
narrating what it sensed or which tool it used. Other reflexes (attention, touch) already
moved the body and are not reported. Nothing but Gemini speaks: reflexes and gestures stay silent
in NAOqi's voice, the `greet` reflex is off (Gemini greets), BasicAwareness is off (liveliness
`calm`) and NAOqi's push recovery, which says "ouch" when Pepper is pushed, is turned off while
the server runs. The free API tier has no Google Search quota, so search is not enabled.

</details>

### 14. Watch and test it

1. Open <http://localhost:8765/>: the control dashboard. Header: connection, battery,
   posture, people, speaking/hearing state, running tasks and a **STOP** button (also `Esc`).
   Below it: live camera (top/bottom/depth), head and base nudge pads, a say box and volume,
   switches for every reflex (greet, high five, attention, touch, battery, idle), the Hearing card
   (microphone and voice agent, with a switch to mute the mic), liveliness and autonomous abilities, tracking/follow and
   perception extractors, one button per gesture (the animation library shows up as its groups
   such as `greeting`, plus the dances; the `+` in a button's top-right corner puts that gesture
   on Pepper's tablet as a big button visitors can press, `−` takes it off again), mood lights, the conversation panel (what
   Gemini heard and said), sensors and a filterable event log.
   Controls work from the laptop; another device (phone) needs `--preview-token <secret>` and
   `http://<laptop-ip>:8765/?token=<secret>`, otherwise it gets a view-only page (STOP still works).
2. Stand ~1 m in front, let Pepper finish any sound, and say a sentence. Your words show up as
   `[heard]` in the terminal and in the conversation panel, and Gemini answers through the speakers.
3. Raise a hand facing the robot: `person.waving`, then the high-five reflex. Touch the
   back of its raised hand to finish it.
4. Touch its head: a `touch.start` event, the eyes flash green and the head tilts briefly.
5. Ask it to play tic-tac-toe: the board appears on the tablet; tap a square and Pepper answers
   with its own move. 5 s after the game ends the tablet goes back to the gesture buttons, if any are pinned.

### 15. Shut down

1. `Ctrl-C` `pepper-mcp-gemini` (`kill`/SIGTERM does the same). It stops
   the base, restores the NAOqi safety distances and autonomous life, and releases camera and
   microphone. Only `kill -9` skips this.
2. Hold the chest button ~3 s until Pepper says goodbye and goes to rest; wait for the
   lights to go off before opening the charging hatch and plugging in the charger.
3. Unplug the laptop's cable from the router whenever you like; the internet stays on Wi-Fi.

If `pepper-mcp-gemini` was killed instead of stopped, the robot keeps the lowered safety distances
until the next start or a reboot.

---

## Configuration

Every flag has a `PEPPER_*` environment variable; the flag wins. Full list:
`pepper-mcp-gemini --help` and [`src/pepper_mcp/config.py`](src/pepper_mcp/config.py).

| Flag | Env | Default | Purpose |
|---|---|---|---|
| `--ip` / `--port` | `PEPPER_IP` / `PEPPER_PORT` | – / 9559 | robot address |
| `--fake` | `PEPPER_FAKE` | off | in-memory robot, no hardware |
| `--preview-port` | `PEPPER_PREVIEW_PORT` | 0 (off) | control dashboard, MJPEG, WAV, SSE |
| `--preview-token` | `PEPPER_PREVIEW_TOKEN` | empty | lets other devices use the dashboard controls (`/?token=…`); empty = laptop only |
| `--language` | `PEPPER_LANGUAGE` | `German` | default conversation and robot TTS language; also the first language hint (plus English) for Gemini's input transcript. Pepper still answers in whatever language it is spoken to |
| `--liveliness` | `PEPPER_LIVELINESS` | `calm` | `off`, `calm`, `lively` autonomous movement (`lively` adds BasicAwareness, which steers the head on its own) |
| `--keep-autonomous-life` | `PEPPER_AUTONOMOUS_LIFE=keep` | disable | leave NAOqi autonomous life on |
| `--safety-front` / `--safety-side` | `PEPPER_SAFETY_FRONT_M` / `PEPPER_SAFETY_SIDE_M` | 0.15 / 0.05 | base safety distances in m (NAOqi: 0.4 / 0.1, which blocks most indoor moves) |
| `--reflexes-off a,b` / `--no-reflexes` | `PEPPER_REFLEXES_OFF` / `PEPPER_REFLEXES` | all on | disable reflexes |
| `--no-audio` / `--no-camera` / `--no-perception` | `PEPPER_AUDIO` / `PEPPER_CAMERA` / `PEPPER_PERCEPTION` | on | switch subsystems off |
| `--camera-fps` / `--camera-res` | `PEPPER_CAMERA_FPS` / `PEPPER_CAMERA_RES` | 10 / `qvga` | camera stream |
| – | `PEPPER_AUDIO_RATE` | 16000 | ignored by `pepper-mcp-gemini`, which always uses 16000 (Gemini Live's input rate) |
| `--mic` | `PEPPER_MIC` | `auto` | head mic to listen with: `auto` (front, or the first that isn't dead; measured for 0.6 s at start, so loud talking right next to one side can mislead it), `front`, `left`, `right`, `rear` |
| – | `PEPPER_LISTEN_URL` | `tcp://0.0.0.0:0` | callback endpoint for the robot |
| – | `PEPPER_SSH_USER` / `PEPPER_SSH_PASSWORD` | `nao` / `pepper` | robot login for copying sounds and tablet assets (`scp`); never logged |
| – | `PEPPER_MAP_STORE` | `pepper_maps.json` | where saved maps and spots are kept (relative to the working directory) |
| `--log-level` | `PEPPER_LOG_LEVEL` | `INFO` | logs go to stderr |
| – | `PEPPER_PREVIEW_HOST` | `0.0.0.0` | address the dashboard listens on (`127.0.0.1` = laptop only; the tablet relay then can't reach it) |
| – | `PEPPER_WAKE_UP` | on | wake Pepper and go to `StandInit` at start |
| – | `PEPPER_JPEG_QUALITY` | 80 | JPEG quality of camera frames (Gemini video, dashboard) |
| – | `PEPPER_MEMORY_POLL_S` | 0.1 | poll period for ALMemory keys when push subscriptions fail (always on this robot) |

Options of `pepper-mcp-gemini` only (no environment variable):

| Flag | Default | Purpose |
|---|---|---|
| `--model` | `gemini-3.8-live` | Gemini Live model; `gemini-3.1-flash-live*` models get blocking tool calls (no async function calling) |
| `--voice` | `Kore` | prebuilt Gemini voice (`Puck`, `Charon`, …) |
| `--persona` | `Pepper` | replaces `{name}` in `persona.txt` |
| `--no-video` | off | don't stream the head camera (1 frame/s) to Gemini |
| `--no-transcripts` | off | don't print `[heard]`/`[said]` lines and turn off input transcription (`[tool]` lines stay). The attention reflex then gets no `speech.*` events, so the eyes and head no longer react to people talking |

`GEMINI_LIVE_KEY` is the only value read from `.env`; a key in `.env` wins over an exported one.
Booleans in `PEPPER_*` variables accept `1`/`true`/`yes`/`on`, anything else is off.
The variables of removed features (`PEPPER_STT*`, `PEPPER_VAD_*`, `PEPPER_BARGE_IN`,
`PEPPER_LISTENING`, `PEPPER_ECHO_TAIL_MS`, `PEPPER_MCP_*`) are ignored, and
`pepper-mcp-gemini` logs a warning if one is set, so delete them from old `.env` files.

## How the interaction loop works

See also [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design.

```
   person talks ──▶ head mic (16 kHz) ──▶ Gemini Live (cloud) ──▶ voice ──▶ Pepper's speakers
   hand raised  ──▶ ALWavingDetection ──▶ person.waving ──▶ high_five reflex (≈0.2 s) ──▶ reflex.completed
   Gemini ──▶ function call (play_gesture, look_at_person, set_leds, camera_snapshot, move_to …)
          ──▶ MCP tool (in-process) ──▶ robot;  robot events ──▶ short notes to Gemini
```

## Persona, language and vocabulary

Gemini's instructions (who Pepper is, how it talks, how it uses its body, safety rules) are in
[`persona.txt`](persona.txt) in the repository root. It is read at every start, so edit it and
restart `pepper-mcp-gemini`. The shipped persona is German and made for one occasion (Swiss
*Nationaler Zukunftstag*: children aged 10–12 visiting an office, questions about becoming a
software developer). Keep its later paragraphs about the body, robot events and safety; they are
what stops Gemini from narrating tool calls or pretending it moved when it didn't. The file is
plain text; `{name}` (the `--persona` value) and `{language}` (`--language`) are replaced if you
use them, any other text, braces included, is sent as written. The file must stay in the repo
root: `pepper-mcp-gemini` looks for it next to `src/`, not in the current directory.

`--language` (default `German`) sets NAOqi's TTS language and the first language hint for the
input transcript (English is always the second). Gemini itself answers in whatever language it is
spoken to and the persona says. Known names: German, English, French, Italian, Spanish, Dutch,
Portuguese, Japanese, Chinese; any other value turns the transcription hint off.

What Pepper knows about the day itself (the programme, Accenture, the computer science and AI
slides, the CLEAR prompt framework) is in [`knowledge.txt`](knowledge.txt), condensed from the
Zukunftstag 2026 presentation. It is appended to the persona at every start and is optional:
delete or replace it for another event and restart.

Names and words Pepper keeps mishearing go in [`vocabulary.txt`](vocabulary.txt) (one per line,
`#` comments), which biases the input transcriber; restart to apply. That transcriber only writes
the `[heard]` lines and the dashboard's Conversation card: Gemini answers from the audio itself.
So a misheard `[heard]` line with a sensible reply is only a log problem; a wrong reply means
Pepper really misheard, and the fix is usually standing closer (within about 1 m).

## Project layout

```
src/pepper_mcp/
  cli.py, config.py, server.py, runtime.py     robot options, settings, MCP server, wiring
  robot/        NAOqi wrappers (motion, speech, camera, audio_in, perception, …) + fake.py
  reflexes/     attention, high_five, greet, touch_reaction, low_battery, idle
  tools/        MCP tools, one module per domain (auto-registered)
  games/        tic-tac-toe on the tablet
  assets/       files served to the tablet (GIF, audio); animations/ + dances/ (Choregraphe
                timelines) and choreography.json, written by choregraphe_import.py
  preview.py    MJPEG / live audio / SSE side channel; verify.py
  gemini_live.py  `pepper-mcp-gemini`: Gemini Live voice/video agent on Pepper's own hardware
  dashboard.py  dashboard state + whitelisted actions; dashboard.html is the page
  tablet_home.py  gesture buttons pinned to the tablet (tablet_gestures.html), back after a game
docs/           MCP_ENDPOINTS.md (every tool/resource/event), ARCHITECTURE.md (design),
                ROBOT_VERIFICATION.md (hardware checklist, firmware quirks)
tests/          pytest suite against the fake robot
persona.txt     Gemini's system instruction
knowledge.txt   facts about the event, appended to the persona
vocabulary.txt  transcription hints
.env            GEMINI_LIVE_KEY (git-ignored, you create it)
pepper_maps.json  saved maps and spots (git-ignored, written at runtime)
```

Extending: new tool domain → `tools/<name>.py` with `register(mcp, rt)`; new gesture →
`robot/gestures.py` or the `define_gesture` tool at runtime; Choregraphe animations and dances →
`python -m pepper_mcp.choregraphe_import <folder>` (see below); new reflex → subclass
`reflexes.base.Reflex`.

### Choregraphe animations and dances

`src/pepper_mcp/assets/choreography.json` lists 100 Choregraphe `.qianim` animations (greetings,
pointing, showing the tablet, idle fidgets, reactions…) and 11 dances, each a `play_gesture`
name (full list in `docs/MCP_ENDPOINTS.md` §2.5). They were imported from the `03_Pepper`
collection (Choregraphe animation library by O. Landais, dance apps from IMERIR's
pepperApplications):

```bash
python -m pepper_mcp.choregraphe_import /path/to/03_Pepper   # rewrites assets/animations, assets/dances
```

The dance music (`assets/dances/*.ogg`, ~19 MB) is committed with the dances, so a fresh clone
dances with music. The tool flattens each dance's nested timelines into one `.qianim`, keeps the
music offset and drops the base moves.

## Development

```bash
uv pip install -e ".[all,dev]"
pytest -q              # fake robot, no hardware, no API key
ruff check src tests
pepper-mcp-gemini --fake --preview-port 8765    # whole stack on the fake robot (needs the key)
```

When you change a tool, flag, default or event, update the matching doc in the same change
(`README.md` configuration, `docs/MCP_ENDPOINTS.md`). A robot module that starts calling a new
NAOqi method needs it in `robot/fake.py` too, or the tests and `--fake` break.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `uv: command not found` | Ubuntu: the `curl … install.sh` line in step 2, then open a new terminal; Arch: `sudo pacman -S uv`; macOS: `brew install uv` |
| `qi` wheel not found / `No solution found … qi` | venv on Python 3.13+: `rm -rf .venv && uv venv --python 3.12 .venv`; or the machine is not x86_64 Linux / Apple-silicon macOS (no `qi` wheels for ARM Linux or Intel Macs) |
| macOS: `No route to host` / `cannot reach NAOqi` although `ping` from another app works | Local Network permission for the terminal app (step 8, macOS) |
| `pepper-mcp-gemini` / `pepper-mcp-verify`: `command not found`, or no `.venv` folder | the venv is not in the repo, you create it once (step 3): `uv venv --python 3.12 .venv && source .venv/bin/activate && uv pip install -e ".[all,dev]"` in the repo root. Already created: it is only not active in this terminal, run `source .venv/bin/activate` |
| `audio_callback` FAIL with `"PepperMcpAudio" … is already registered`, or `pepper-mcp-gemini` cannot get the mic | another `pepper-mcp-gemini` (maybe in another terminal or over SSH) still owns Pepper: stop it with `Ctrl-C`, or `pkill -INT -f pepper-mcp-gemini` |
| starts but says `ALTextToSpeech` / `ALAudioDevice` / `ALVideoDevice` is not available, or `microphone is not available` | NAOqi was still starting (Pepper had just booted) and was given up on after 120 s (`NAOqi still lacks …`): wait until Pepper has said its IP / opened its eyes, then start again |
| `cannot reach NAOqi` | same network? IP still right (press the chest button)? `nc -zv <PEPPER_IP> 9559`; port closed while ping works → client isolation |
| Pepper says it is not connected | it is not on the router's Wi-Fi: step 10 |
| connects, but Gemini hears nothing | `pepper-mcp-verify` → `audio_callback` must show ≈ 6 callbacks/s; firewall rule from step 8; router isolation (step 7) |
| Pepper does not drive, turn or follow / "charging hatch is open" / `motion.blocked` | **close the charging hatch** (most common: NAOqi locks the wheels while it is open), unplug the charger, or something is inside the safety distance; lower it with `--safety-front` or the `set_safety` tool when the space is clear |
| motors limp, nothing moves | emergency stop at the back of the neck is pressed: release it, then `wake_up` |
| head snaps back after moves | `set_liveliness off`, or start with `--liveliness off` |
| tic-tac-toe (or another laptop page) shows a blank white page on the tablet | the tablet can only reach the head, through the relay the server starts over SSH: look for `tablet relay disabled` / `did not start` in the log (SSH password, `--preview-port` set?) and restart `pepper-mcp-gemini` |
| `ssh/scp failed` in the log, no sound files or tablet page | `openssh` client missing (step 1), or the robot password is not `pepper`: set `PEPPER_SSH_PASSWORD` |
| `nmcli: command not found` | NetworkManager not in use: configure the cable the same way in your network tool (step 6, Linux edge cases) |
| Internet drops when the router cable is plugged in | the wired profile took the default route: Linux: use the `pepper-router` profile (step 6), check `ip route get 1.1.1.1` says `dev wl…`; macOS: Wi-Fi above Ethernet in the service order (step 6) |
| Router cable plugged in but no link (`ethtool <ETH_DEV>`: `Link detected: no`) | with the OWC dock, the laptop must be on the dock's USB-C port with the laptop icon (step 6); otherwise swap cable / router LAN port |
| Pepper unreachable after unplugging its Ethernet | it is now on Wi-Fi with a **different** IP: press the chest button; if it says not connected, redo step 10 |
| no `person.waving` | `ALWavingDetection` needs a person facing the robot with the hand raised clearly above the shoulder |
| Pepper does not talk at all | `pepper-mcp-gemini` must be running (step 13); nothing else answers |
| Pepper mishears words | `[heard]` wrong but the reply fits: only the transcript is off; add the words to `vocabulary.txt`. Reply wrong too: talk from within about 1 m (the fans drown speech further away) and check the `microphone(s) … look dead` log line |
| Pepper says "ouch" (NAOqi's voice) when pushed | log says `NAOqi refused to turn off push recovery`: open `http://<PEPPER_IP>/` → Settings → Advanced, tick *Permit deactivation of the safety reflexes*, restart `pepper-mcp-gemini`. This only permits turning reflexes off; the server turns off push recovery alone and turns it back on at shutdown |
| Pepper hears itself | the mic is muted while Pepper makes sound, plus a 0.4 s tail: talk after it has finished speaking |
| `GEMINI_LIVE_KEY is not set` | `.env` missing or you are not in the repo root: step 4 |
| `pepper-mcp-gemini`: Pepper does not react to speech | log says `microphone(s) … look dead`: that head mic failed, `--mic auto` already skips it (or force one with `--mic left`); otherwise stand within ~1 m and speak after Pepper has finished (the mic is muted while it makes sound); `1011 … exceeded quota` at connect = the key's free-tier limit |
| `pepper-mcp-gemini`: `ignoring PEPPER_…` warning | an old `.env` or shell still sets a removed variable: delete it (see Configuration) |
