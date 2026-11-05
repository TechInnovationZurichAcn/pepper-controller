# Pepper MCP

**Gemini Live drives a SoftBank Pepper** (NAOqi 2.5) in real time: it hears and sees through
Pepper's own head microphone and camera, talks through its speakers, and moves the body through
an MCP tool server that runs inside the same process.

* **Every robot feature as a tool** – speech with gestures, camera snapshots,
  people/face/touch/sound perception, base and joint motion, animations and custom gestures,
  LEDs, tablet, tic-tac-toe, following, navigation and maps, behaviours. 107 tools and
  10 resources (no prompts), listed in [`docs/MCP_ENDPOINTS.md`](docs/MCP_ENDPOINTS.md);
  Gemini is offered 94 of the tools.
* **Real-time by design** – the robot pushes audio and ALMemory events to the laptop. The
  microphone goes straight to Gemini Live and the voice comes back to the speakers; robot
  events reach it as short notes. Design notes in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
* **Reflexes** – high five when somebody raises a hand, a wave when someone shows up, a nod
  when touched: sub-second reactions that run on the server without the LLM. All toggleable.
* **Works without a robot** – `--fake` runs the whole stack against an in-memory NAOqi so
  you can check the install first.

This branch contains only the server and its voice agent. The keyboard teleop lives on `main`.

---

## Quick start (laptop and robot already set up)

```bash
cd pepper-controller && source .venv/bin/activate     # the cloned repo (step 3)
# close the charging hatch (flap on the lower back): while it is open Pepper cannot drive, turn or follow
# power Pepper on, wait for it to stand up, press the chest button once → it says its IP
ping -c 2 <PEPPER_IP> && nc -zv <PEPPER_IP> 9559
pepper-mcp-verify --ip <PEPPER_IP> --no-move          # optional sanity check
pepper-mcp-gemini --ip <PEPPER_IP> --preview-port 8765 --liveliness off   # step 12
```

New laptop? Do **Part 1** once. New robot or new network? Do **Part 2**.

---

## Part 1 – Install on the laptop (Arch or Ubuntu, no robot needed)

Tested on Arch Linux and Ubuntu 22.04 / 24.04 (desktop). Other distributions work if you
translate the package names. `<PEPPER_IP>` stands for the robot's address (step 8).

### 0. What you need

| Item | Notes |
|---|---|
| **x86_64** Linux laptop | `qi` (libqi-python, the NAOqi client) only ships Linux wheels for x86_64; ARM boards such as a Raspberry Pi can run `--fake` but cannot talk to the robot |
| Internet on the laptop | the LLM (Gemini Live) runs in the cloud |
| A Gemini API key | free from [Google AI Studio](https://aistudio.google.com/apikey); needed by `pepper-mcp-gemini`, also with `--fake` (step 4) |
| Pepper with NAOqi 2.5.x | charged (plug the charger into the back of the base; unplug it before driving) |
| A network Pepper and the laptop share | a router you control with **client/AP isolation off**, or an Ethernet cable straight from the laptop to Pepper (step 7, option C) |
| ~2 m × 2 m of clear floor | Pepper moves its base during the checks and when the LLM decides to walk |

Python itself is **not** a requirement: `qi` needs CPython 3.10 – 3.12, which is older than
Arch's and Ubuntu 25.04+'s system Python, so [`uv`](https://docs.astral.sh/uv/) downloads a
private 3.12 for the project. The system Python is never touched.

### 1. System packages

**Arch Linux**

```bash
sudo pacman -Syu --needed git openssh uv openbsd-netcat nmap avahi nss-mdns
```

**Ubuntu / Debian**

```bash
sudo apt update
sudo apt install -y git curl openssh-client netcat-openbsd nmap avahi-utils
```

What they are for: `git` to clone, `openssh` (`ssh`/`scp`) to copy sounds and tablet assets
onto the robot, `nc` / `nmap` / `avahi` to find and test the robot on the network (steps 8–9).
`uv` is installed in step 2 on Ubuntu.

The network commands in Part 2 use NetworkManager (`nmcli`), the default on Ubuntu Desktop
and on most Arch desktop installs. If you use something else (`iwd`, `systemd-networkd`,
netplan on Ubuntu Server), do the equivalent there.

### 2. Install `uv` (Ubuntu only; Arch got it in step 1)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # installs into ~/.local/bin
source ~/.local/bin/env                             # or open a new terminal
uv --version
```

### 3. Clone the repo and create the environment

```bash
git clone -b mcp-server https://github.com/Maelwalser/pepper-controller.git
cd pepper-controller
uv venv --python 3.12 .venv          # downloads CPython 3.12 if the system lacks it
source .venv/bin/activate
uv pip install -e ".[all,dev]"       # qi + google-genai + pytest/ruff
```

Extras: `robot` (`qi`, needed for real hardware), `all` (= `robot`), `dev` (tests, ruff). `google-genai` is a core dependency.

**Run every command from the repo root with the venv active** (`source .venv/bin/activate`
in each new terminal, or call `.venv/bin/pepper-mcp-gemini` directly). The `.env` key file and
saved maps (`pepper_maps.json`) are looked up relative to the current directory.

### 4. Add the Gemini API key

```bash
echo 'GEMINI_LIVE_KEY=<your key>' > .env && chmod 600 .env    # .env is git-ignored
```

`pepper-mcp-gemini` reads the key from `.env` in the current directory, or from an exported
`GEMINI_LIVE_KEY`. Other settings are not read from `.env`: pass them as flags or export the
`PEPPER_*` variables (see [Configuration](#configuration)).

### 5. Check the install without the robot

```bash
pytest -q                                                 # whole suite, fake robot
pepper-mcp-gemini --fake --preview-port 8765              # fake robot, real Gemini session
```

Open <http://localhost:8765/> (dashboard with event log and fake camera), `Ctrl-C` to stop.
`--fake` still needs the key from step 4 and internet: the Gemini session starts even with the
fake robot. A failure here is an install or key problem, not a robot problem.

---

## Part 2 – Bring up the robot

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
* **Ethernet port** – under the cover on the back of the head.

### 6. Power on

1. Close the charging hatch and unplug the charger.
2. Press the chest button once (short). The eyes and shoulders light up and the tablet
   boots.
3. Wait ~2–3 minutes. Boot is finished when Pepper lifts its head, stands up straight and
   its eyes light up.

If Pepper stays slumped forward after boot it is in rest mode – that is fine, the server
wakes it on start.

### 7. Put Pepper and the laptop on the same network

The robot has to open connections **to** the laptop (microphone audio, ALMemory signals),
not only the other way round. Networks with client isolation (guest Wi-Fi, campus/eduroam,
many hotel and office networks) break hearing even when `ping` works.

**Option A – your own router over Wi-Fi (recommended).**

1. Power the router and wait until its Wi-Fi is up.
2. In its admin page turn off "AP isolation" / "client isolation" / "guest mode" for the
   network Pepper will join.
3. Optional: add a DHCP reservation for Pepper so its IP stops changing.
4. If Pepper is not on this Wi-Fi yet, join it from the robot side:
   * on the tablet: the settings screen → *Network* / *Wi-Fi* → pick the SSID → password;
   * or, if Pepper is already reachable on some network, open its web page
     `http://<PEPPER_IP>/` in a browser (user `nao` and the robot's password) →
     *Network settings*;
   * or plug Pepper into the router by Ethernet first (option B, step B4).
5. Connect the laptop (or use the desktop's Wi-Fi menu):

   ```bash
   nmcli dev wifi connect "<ROUTER_SSID>" password "<ROUTER_PASSWORD>"
   ```

**Option B – dedicated router for Pepper, laptop wired to it, internet from the laptop's
normal Wi-Fi.** Use this with a new or offline router (no internet uplink needed). Pepper
ends up on the router's Wi-Fi and the laptop reaches it over the Ethernet cable, while the
laptop's Wi-Fi keeps carrying internet traffic (Gemini, MCP clients).

```
 internet ── venue Wi-Fi ── wlan ┐
                                 laptop
 Pepper ~~ router Wi-Fi ~~ router ── Ethernet ┘   (Pepper: Ethernet only for first setup)
```

*B1. Router (once).* Power it, connect to its admin page (address and login are on its
label; usually `192.168.0.1` / `192.168.1.1`), then:

* set a Wi-Fi SSID and a WPA2 password Pepper will use (2.4 GHz is the safe choice);
* turn off "AP / client isolation" and do not use the guest network;
* make sure its LAN subnet does **not** overlap the laptop's Wi-Fi or Docker networks
  (`ip -br addr`; Docker uses `172.17–172.31.x`, so a `192.168.x.0/24` LAN is safe);
* optional: once Pepper shows up in the DHCP client list, add a reservation so its IP stays
  the same.

*B2. Laptop profile (once).* A NetworkManager profile that takes an address from the router
but **never** a default route or DNS, so internet stays on Wi-Fi. It has no interface name,
so it works with any Ethernet port or USB adapter, and its high autoconnect priority beats the
auto-created "Wired connection N" profiles. Those would otherwise take the default route as
soon as the cable is plugged in, and since this router has no internet, the laptop goes
offline:

```bash
nmcli con add type ethernet con-name pepper-router \
  connection.autoconnect yes connection.autoconnect-priority 100 \
  ipv4.method auto ipv4.never-default yes ipv4.ignore-auto-dns yes ipv6.method disabled
```

Side effect: on any other wired network the cable is used for its local subnet only and
internet keeps going over Wi-Fi. To use a wired uplink for internet somewhere else, run
`nmcli con up "Wired connection N"` there (or delete this profile).

*B3. Plug in and bring it up.* Cable laptop ↔ a router **LAN** port (not WAN/Internet/Cable).
Do **not** join the laptop's Wi-Fi to the router: a laptop has one Wi-Fi card, so that
replaces the internet Wi-Fi and the internet drops. The router's Wi-Fi is for Pepper only.

```bash
ip -br link                                  # the Ethernet device that is now UP, e.g. enp0s20f0u3u4u5
nmcli con up pepper-router ifname <ETH_DEV>   # usually already up on its own
ip -br addr show <ETH_DEV>                   # an address from the router, e.g. 192.168.0.100/24
ip route                                     # "default via … dev wl…" must be the only default
ip route get 1.1.1.1                         # → dev wl…  (internet over Wi-Fi)
ip route get 192.168.0.1                     # → dev <ETH_DEV>  (router subnet over the cable)
```

If a `default via … dev <ETH_DEV>` route shows up anyway, another wired profile won the race:
`nmcli con down "Wired connection N"` and bring `pepper-router` up again. No link light on the
port means a bad cable or port: swap it before debugging anything else.

A default route with `metric 1024`, or `resolvectl status <ETH_DEV>` showing the router as DNS
server with `Default Route: yes`, means **`systemd-networkd`** is also configuring the cable
(`systemctl is-active systemd-networkd`, files in `/etc/systemd/network/`). Either stop it from
managing Ethernet, or give the adapter its own file that keeps gateway and DNS off it. Match on
the MAC (`ip -br link`), not the name: USB adapter names change with the port:

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

*B4. First time only: put Pepper on the router's Wi-Fi.* Pepper does not know the new SSID
yet, so plug its head Ethernet port (see Robot parts above) into another LAN port of the router, find its
wired IP (step 8: the router's client list, or `nmap -p 9559 --open <router subnet>`), then
join the Wi-Fi with connman's own client over SSH. **WPS push-button** avoids typing the key
at all (FRITZ!Box: the *Connect/WPS* button, ~1 s, until the WLAN LED blinks; you then have
~2 minutes):

```bash
ssh nao@<PEPPER_IP> connmanctl services     # find the router's id: wifi_…_managed_psk
ssh -t nao@<PEPPER_IP> connmanctl           # interactive shell on the robot, then:
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
means an earlier attempt is still pending: `disconnect <id>` first. The robot web page
(`http://<PEPPER_IP>/` → *Network settings*) works too. `ALConnectionManager.connect` over
NAOqi was unreliable on this robot (it stayed `idle`), so use `connmanctl`.

Read the Wi-Fi address with `ssh nao@<PEPPER_IP> ip -4 -o addr show wlan0` (BusyBox `ip`, no `-br`), or press the chest
button after unplugging.
Pepper (connman) remembers the network and rejoins it on every boot. Unplug Pepper's cable
and check
`ping` / `nc -zv <PEPPER_IP> 9559` against the **Wi-Fi** address. The Ethernet IP is no
longer valid once the cable is out.

*Every session after that:* power the router, plug the laptop's cable (`pepper-router`
comes up by itself; check with `ip route get 1.1.1.1`), power Pepper and press its chest
button once: it says its current IP. The address comes from the router's DHCP and can change
between sessions, so ask each time, then:

```bash
export PEPPER_IP=<the IP Pepper said>
pepper-mcp-gemini               # reads PEPPER_IP when --ip is omitted
```

The firewall rule from step 10 only needs adding once: with `ufw`,
`sudo ufw allow from 192.168.178.0/24` covers the whole FRITZ!Box subnet whatever IP Pepper
gets. Unplug the cable when done.

**Option C – no router, Ethernet cable laptop ↔ Pepper.** Let the laptop hand out an
address on the cable while Wi-Fi keeps the internet:

```bash
ip -br link                     # find the Ethernet device, e.g. enp3s0 / eno1 / enx…
nmcli con add type ethernet ifname <ETH_DEV> con-name pepper-direct ipv4.method shared ipv6.method ignore
nmcli con up pepper-direct      # laptop becomes 10.42.0.1 and runs DHCP for Pepper
```

`ipv4.method shared` needs `dnsmasq` (Arch: `sudo pacman -S dnsmasq`; Ubuntu Desktop has it).
Pepper gets a `10.42.0.x` address; step 8 tells you which.

### 8. Get Pepper's IP address

Use the first method that works:

1. **Ask Pepper.** With Pepper booted, press the chest button once (short). It says
   "… my internet address is 192.168.1.5" (or that it is not connected – go back to
   step 7). Write the address down.
2. **Router.** Look for a client called `pepper` / the robot's name in the router's DHCP
   client list.
3. **Zeroconf.** NAOqi advertises itself over mDNS (needs the avahi daemon running:
   `sudo systemctl enable --now avahi-daemon`, already on by default on Ubuntu):

   ```bash
   avahi-browse -rt _naoqi._tcp
   ```

4. **Scan the subnet** for the NAOqi port (adjust the subnet to yours, see `ip -br addr`;
   for option C it is `10.42.0.0/24`):

   ```bash
   nmap -p 9559 --open 192.168.1.0/24
   ```

Optional, so you can type it once per shell: `export PEPPER_IP=192.168.1.5` – every
command below also reads `PEPPER_IP` when `--ip` is omitted.

### 9. Check the laptop can reach the robot

```bash
ip -br addr                     # your laptop's address on the robot's subnet
ping -c 3 <PEPPER_IP>
nc -zv <PEPPER_IP> 9559         # NAOqi port
```

* `ping` fails → not on the same network, or wrong IP (ask Pepper again).
* `ping` works but port 9559 does not → client isolation is still on in the router.

### 10. Open the laptop firewall for the robot

The robot calls back into the laptop on a random TCP port (audio stream, signals). A
firewall that blocks inbound connections makes hearing **fail silently**. Find out which
firewall you have, then allow everything from the robot's IP only:

```bash
sudo ufw status                 # Ubuntu ships ufw; "inactive" = nothing to do
sudo firewall-cmd --state       # firewalld; "not running" / command not found = nothing to do
```

| Firewall | Allow the robot | Undo later |
|---|---|---|
| `ufw` (active) | `sudo ufw allow from <PEPPER_IP>` | `sudo ufw status numbered`, then `sudo ufw delete <n>` |
| `firewalld` | `sudo firewall-cmd --zone=trusted --add-source=<PEPPER_IP>` | `sudo firewall-cmd --zone=trusted --remove-source=<PEPPER_IP>` (the rule is runtime-only and gone after a reboot anyway) |
| none (default on a fresh Arch install) | nothing to do | – |

Using raw `nftables`/`iptables`? Allow inbound TCP from the robot's IP, or pin the callback
port with `PEPPER_LISTEN_URL=tcp://0.0.0.0:9560` and open just that.

To open the dashboard (step 13) from another device (phone, Pepper's tablet) also open TCP
8765 (`sudo ufw allow 8765/tcp` or `sudo firewall-cmd --add-port=8765/tcp`).

### 11. Hardware checklist

Clear ~1 m in front of Pepper (the last check drives the base forward for 0.3 s), then:

```bash
pepper-mcp-verify --ip <PEPPER_IP>        # --no-move skips the drive, --quiet skips speech
```

You want `[OK ]` on at least `connect`, `tts`, `audio_callback` (≈ 6 callbacks/s), `camera`
and `move`. A `FAIL` on `audio_callback` almost always means the firewall (step 10) or client
isolation (step 7). What every check proves and its fallback:
[`docs/ROBOT_VERIFICATION.md`](docs/ROBOT_VERIFICATION.md).

Playing sound files and showing pages on the tablet copies files onto the robot over SSH
as user `nao`, password `pepper` (the factory default). With `--preview-port` the server
also starts a small relay on the head over SSH, because the tablet cannot reach the laptop
directly. If your robot's password was
changed, export `PEPPER_SSH_PASSWORD=<password>` (and `PEPPER_SSH_USER` if needed) before
starting the server.

### 12. Start Gemini Live

```bash
pepper-mcp-gemini --ip <PEPPER_IP> --preview-port 8765 --liveliness off   # key from step 4
```

This is the one way to run the server. It owns the robot (only one process may: stop any other
`pepper-mcp-gemini` first) and prints `[heard]`, `[said]` and `[tool]` lines. On start it wakes
Pepper, turns autonomous life off, applies the liveliness preset and lowers the base safety
distances to 0.15 m / 0.05 m.

A speech-to-speech model hears Pepper's own head microphone (front, or the next working one),
watches its head camera (1 fps), answers through Pepper's loudspeakers and drives the body
through the MCP tools in the same process: 94 of the 107 (`say`, the speech and hearing tools,
`recognize_keywords`, `wait_for_event` and the other blocking ones are left out, because Gemini
is the voice and the ears). Actions run in the background while it keeps talking and report back
silently, and turns that would only say "no speech" or a gesture name are kept off the speakers.
Robot events (touch, people appearing, `motion.blocked`, failed tasks, high-five results) are
passed to it as short notes; it reacts with its body and speaks only when a person would, without
narrating what it sensed or which tool it used. Other reflexes (attention, touch) already
moved the body and are not reported. Nothing but Gemini speaks: reflexes and gestures stay silent
in NAOqi's voice, the `greet` reflex is off (Gemini greets), BasicAwareness is off (liveliness
`calm`) and NAOqi's push recovery, which says "ouch" when Pepper is pushed, is turned off while
the server runs. Nothing on the laptop is used except the network.

Gemini-only flags, on top of the [Configuration](#configuration) table: `--model` (default
`gemini-3.8-live`), `--voice` (`Kore`, `Puck`, `Charon`, …), `--persona`, `--no-video`,
`--no-transcripts`. Pepper's mic sits next to its speakers and has no echo cancellation, so it is
muted while Pepper makes any sound (Gemini's voice, NAOqi speech from reflexes, gestures with
sounds) plus a short tail: talk after Pepper finishes; interrupting mid-sentence is not possible.
The free API tier has no Google Search quota, so search is not enabled.

### 13. Watch and test it

1. Open <http://localhost:8765/>: the control dashboard. Header: connection, battery,
   posture, people, speaking/hearing state, running tasks and a **STOP** button (also `Esc`).
   Below it: live camera (top/bottom/depth), head and base nudge pads, a say box and volume,
   switches for every reflex (wave/greet, high five, attention, touch, battery), the Hearing card
   (microphone and voice agent, with a switch to mute the mic), liveliness and autonomous abilities, tracking/follow and
   perception extractors, one button per gesture, mood lights, the conversation panel (what
   Gemini heard and said), sensors and a filterable event log.
   Controls work from the laptop; another device (phone) needs `--preview-token <secret>` and
   `http://<laptop-ip>:8765/?token=<secret>`, otherwise it gets a view-only page (STOP still works).
2. Stand ~1 m in front, let Pepper finish any sound, and say a sentence. Your words show up as
   `[heard]` in the terminal and in the conversation panel, and Gemini answers through the speakers.
3. Raise a hand facing the robot: `person.waving`, then the high-five reflex. Touch the
   back of its raised hand to finish it.
4. Touch its head: a `touch.*` event and a nod.

`Ctrl-C` stops it; on shutdown the server stops the base and restores motion defaults and
autonomous life.

### 14. Shut down

1. `Ctrl-C` `pepper-mcp-gemini` (`kill`/SIGTERM does the same). It stops
   the base, restores the NAOqi safety distances and autonomous life, and releases camera and
   microphone. Only `kill -9` skips this.
2. Hold the chest button ~3 s until Pepper says goodbye and goes to rest; wait for the
   lights to go off before closing the charging hatch and plugging in the charger.
3. Optional: remove the firewall rule from step 10, and `nmcli con down pepper-direct` if
   you used option C.

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

`GEMINI_LIVE_KEY` (for `pepper-mcp-gemini`) is the only value read from `.env`.
The variables of removed features (`PEPPER_STT*`, `PEPPER_VAD_*`, `PEPPER_BARGE_IN`,
`PEPPER_LISTENING`, `PEPPER_ECHO_TAIL_MS`, `PEPPER_MCP_*`) are ignored, and
`pepper-mcp-gemini` logs a warning if one is set, so delete them from old `.env` files.

## How the interaction loop works

```
   person talks ──▶ head mic (16 kHz) ──▶ Gemini Live (cloud) ──▶ voice ──▶ Pepper's speakers
   hand raised  ──▶ ALWavingDetection ──▶ person.waving ──▶ high_five reflex (≈0.2 s) ──▶ reflex.completed
   Gemini ──▶ function call (play_gesture, look_at_person, set_leds, camera_snapshot, move_to …)
          ──▶ MCP tool (in-process) ──▶ robot;  robot events ──▶ short notes to Gemini
```

Gemini's instructions (persona, speaking rules, safety) are in [`persona.txt`](persona.txt) in the
repository root: edit it and restart `pepper-mcp-gemini`. `{language}` and `{name}` (the
`--persona` value) are filled in; any other text, braces included, is sent as written.
Names and words Pepper keeps mishearing go in [`vocabulary.txt`](vocabulary.txt) (one per line,
`#` comments), which biases the input transcriber; restart to apply. That transcriber only writes
the `[heard]` lines and the dashboard's Conversation card: Gemini answers from the audio itself.
So a misheard `[heard]` line with a sensible reply is only a log problem; a wrong reply means
Pepper really misheard, and the fix is usually standing closer (within about 1 m).
`list_event_types` and `pepper://events/types` document every event.

## Project layout

```
src/pepper_mcp/
  cli.py, config.py, server.py, runtime.py     robot options, settings, MCP server, wiring
  robot/        NAOqi wrappers (motion, speech, camera, audio_in, perception, …) + fake.py
  reflexes/     attention, high_five, greet, touch_reaction, low_battery
  tools/        MCP tools, one module per domain (auto-registered)
  games/        tic-tac-toe on the tablet
  assets/       files served to the tablet (GIF, audio)
  preview.py    MJPEG / live audio / SSE side channel; verify.py
  gemini_live.py  `pepper-mcp-gemini`: Gemini Live voice/video agent on Pepper's own hardware
  dashboard.py  dashboard state + whitelisted actions; dashboard.html is the page
docs/           MCP_ENDPOINTS.md, ARCHITECTURE.md, ROBOT_VERIFICATION.md
tests/          pytest suite against the fake robot
```

Extending: new tool domain → `tools/<name>.py` with `register(mcp, rt)`; new gesture →
`robot/gestures.py` or the `define_gesture` tool at runtime; new reflex → subclass
`reflexes.base.Reflex`.

## Development

```bash
uv pip install -e ".[all,dev]"
pytest -q              # fake robot, no hardware
ruff check src tests
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `uv: command not found` | Ubuntu: step 2, then open a new terminal; Arch: `sudo pacman -S uv` |
| `qi` wheel not found / `No solution found … qi` | venv on Python 3.13+: `rm -rf .venv && uv venv --python 3.12 .venv`; or the machine is not x86_64 (no `qi` wheels for ARM Linux) |
| `pepper-mcp-gemini: command not found` | venv not active in this terminal: `source .venv/bin/activate` from the repo root |
| `cannot reach NAOqi` | same network? IP still right (press the chest button)? `nc -zv <PEPPER_IP> 9559`; port closed while ping works → client isolation |
| Pepper says it is not connected | it is not on any network: step 7 |
| connects, but Gemini hears nothing | `pepper-mcp-verify` → `audio_callback` must show ≈ 6 callbacks/s; firewall rule from step 10; router isolation |
| Pepper does not drive, turn or follow / "charging hatch is open" / `motion.blocked` | **close the charging hatch** (most common: NAOqi locks the wheels while it is open), unplug the charger, or something is inside the safety distance; lower it with `--safety-front` or the `set_safety` tool when the space is clear |
| motors limp, nothing moves | emergency stop at the back of the neck is pressed: release it, then `wake_up` |
| head snaps back after moves | `set_liveliness off`, or start with `--liveliness off` |
| tic-tac-toe (or another laptop page) shows a blank white page on the tablet | the tablet can only reach the head, through the relay the server starts over SSH: look for `tablet relay disabled` / `did not start` in the log (SSH password, `--preview-port` set?) and restart `pepper-mcp-gemini` |
| `ssh/scp failed` in the log, no sound files or tablet page | `openssh` client missing (step 1), or the robot password is not `pepper`: set `PEPPER_SSH_PASSWORD` |
| `nmcli: command not found` / option C gives Pepper no address | NetworkManager not in use, or `dnsmasq` missing for `ipv4.method shared` (step 7) |
| Internet drops when the router cable is plugged in | the wired profile took the default route: use `pepper-router` (option B, step B2), check `ip route get 1.1.1.1` says `dev wl…` |
| Pepper unreachable after unplugging its Ethernet | it is now on Wi-Fi with a **different** IP: press the chest button; if it says not connected, redo step B4 |
| no `person.waving` | `ALWavingDetection` needs a person facing the robot with the hand raised clearly above the shoulder |
| Pepper does not talk at all | `pepper-mcp-gemini` must be running (step 12); nothing else answers |
| Pepper mishears words | `[heard]` wrong but the reply fits: only the transcript is off; add the words to `vocabulary.txt`. Reply wrong too: talk from within about 1 m (the fans drown speech further away) and check the `microphone(s) … look dead` log line |
| Pepper says "ouch" (NAOqi's voice) when pushed | log says `NAOqi refused to turn off push recovery`: open `http://<PEPPER_IP>/` → Settings → Advanced, tick *Permit deactivation of the safety reflexes*, restart `pepper-mcp-gemini`. This only permits turning reflexes off; the server turns off push recovery alone and turns it back on at shutdown |
| Pepper hears itself | the mic is muted while Pepper makes sound, plus a 0.4 s tail: talk after it has finished speaking |
| `GEMINI_LIVE_KEY is not set` | `.env` missing or you are not in the repo root: step 4 |
| `pepper-mcp-gemini`: Pepper does not react to speech | log says `microphone(s) … look dead`: that head mic failed, `--mic auto` already skips it (or force one with `--mic left`); otherwise stand within ~1.5 m and speak after Pepper has finished (the mic is muted while it makes sound); `1011 … exceeded quota` at connect = the key's free-tier limit |
| `pepper-mcp-gemini`: `ignoring PEPPER_…` warning | an old `.env` or shell still sets a removed variable: delete it (see Configuration) |
