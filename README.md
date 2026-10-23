# Pepper MCP

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives a modern LLM a
body: **SoftBank Pepper** (NAOqi 2.5) seen, heard, spoken through and moved in real time.

* **Every robot feature as a tool** – speech with gestures, free-speech hearing, camera
  snapshots, people/face/touch/sound perception, base and joint motion, animations and
  custom gestures, LEDs, tablet, following, navigation and maps, behaviours. 106 tools,
  10 resources and 2 prompts, listed in [`docs/MCP_ENDPOINTS.md`](docs/MCP_ENDPOINTS.md).
* **Real-time by design** – the robot pushes audio and ALMemory events to the laptop, the
  server turns them into events, and the LLM blocks on `wait_for_event` instead of polling.
  Speech is queued sentence by sentence so Pepper starts talking while the LLM is still
  writing. Design notes in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
* **Reflexes** – high five when somebody raises a hand, a wave when someone shows up,
  eye colours and head turns that follow the conversation, a nod when touched: sub-second
  reactions that run on the server and report back to the LLM as events. All toggleable.
* **Works without a robot** – `--fake` runs the whole stack against an in-memory NAOqi so
  you can wire up your LLM client first.

This branch contains only the MCP server. The keyboard teleop lives on `main`.

---

## Quick start (robot already set up)

```bash
cd ~/Personal/pepper && source .venv/bin/activate
# 1. power Pepper on, wait for it to stand up, press the chest button once → it says its IP
ping -c 2 <PEPPER_IP> && nc -zv <PEPPER_IP> 9559
pepper-mcp-verify --ip <PEPPER_IP> --no-move      # optional sanity check
claude                                            # pepper MCP server is registered, see step 11
# inside Claude Code: /mcp__pepper__pepper_live_agent
```

If the IP changed since you registered the server, re-register it (step 11). Everything
else is explained below, from switching the robot on to talking to it.

---

## Step-by-step setup

Commands assume Arch Linux, `zsh`, the repo at `~/Personal/pepper`, and `<PEPPER_IP>`
standing for the robot's address (step 3).

### 0. What you need

| Item | Notes |
|---|---|
| Pepper with NAOqi 2.5.x | charged (plug the charger into the back of the base; unplug it before driving) |
| A network Pepper and the laptop share | a router you control with **client/AP isolation off**, or an Ethernet cable straight from the laptop to Pepper (step 2, option C) |
| Laptop with internet | the LLM (Claude) runs in the cloud; only the robot traffic is local |
| Python 3.10 – 3.12 and [`uv`](https://docs.astral.sh/uv/) | `qi` (libqi-python) has no wheels for 3.13+; Arch's system Python is newer, so `uv` fetches 3.12 |
| ~2 m × 2 m of clear floor | Pepper moves its base during the checks and when the LLM decides to walk |

Robot parts you will touch:

* **Chest button** – just below the tablet. Short press: power on, or (when running) say
  the IP. Hold ~3 s: shut down.
* **Emergency stop** – the button at the back of the neck. Press it if Pepper does anything
  unsafe: the motors stop and the robot goes to a safe rest. Release it, then the server's
  `wake_up` tool (or a server restart) brings the motors back.
* **Charging hatch** – the flap at the back of the base. While it is open the base
  **refuses to move**.
* **Ethernet port** – under the cover on the back of the head.

### 1. Power on

1. Close the charging hatch and unplug the charger.
2. Press the chest button once (short). The eyes and shoulders light up and the tablet
   boots.
3. Wait ~2–3 minutes. Boot is finished when Pepper lifts its head, stands up straight and
   its eyes light up.

If Pepper stays slumped forward after boot it is in rest mode – that is fine, the server
wakes it on start.

### 2. Put Pepper and the laptop on the same network

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
   * or plug Pepper in by Ethernet (option C) and use the web page from there.
5. Connect the laptop:

   ```bash
   nmcli dev wifi connect "<ROUTER_SSID>" password "<ROUTER_PASSWORD>"
   ```

**Option B – router without internet, laptop keeps its normal Wi-Fi.** Plug the laptop into
the router by Ethernet and stop the cable from taking the default route, otherwise Claude
is cut off:

```bash
nmcli con mod "Wired connection 1" ipv4.never-default yes ipv6.never-default yes
nmcli con up "Wired connection 1"
ip route get 1.1.1.1            # must say "dev wlp…", not the Ethernet device
```

No link light on a port means a bad cable: swap it before debugging anything else.

**Option C – no router, Ethernet cable laptop ↔ Pepper.** Let the laptop hand out an
address on the cable while Wi-Fi keeps the internet:

```bash
nmcli con add type ethernet ifname <ETH_DEV> con-name pepper-direct ipv4.method shared ipv6.method ignore
nmcli con up pepper-direct      # laptop becomes 10.42.0.1 and runs DHCP for Pepper
```

(`ip -br link` lists the device names.) Pepper gets a `10.42.0.x` address; step 3 tells
you which.

### 3. Get Pepper's IP address

Use the first method that works:

1. **Ask Pepper.** With Pepper booted, press the chest button once (short). It says
   "… my internet address is 192.168.1.5" (or that it is not connected – go back to
   step 2). Write the address down.
2. **Router.** Look for a client called `pepper` / the robot's name in the router's DHCP
   client list.
3. **Zeroconf.** NAOqi advertises itself over mDNS:

   ```bash
   avahi-browse -rt _naoqi._tcp     # pacman -S avahi nss-mdns if missing
   ```

4. **Scan the subnet** for the NAOqi port (adjust the subnet to yours, see `ip -br addr`;
   for option C it is `10.42.0.0/24`):

   ```bash
   nmap -p 9559 --open 192.168.1.0/24     # pacman -S nmap if missing
   ```

Optional, so you can type it once per shell: `export PEPPER_IP=192.168.1.5` – every
command below also reads `PEPPER_IP` when `--ip` is omitted.

### 4. Check the laptop can reach the robot

```bash
ip -br addr                     # your laptop's address on the robot's subnet
ping -c 3 <PEPPER_IP>
nc -zv <PEPPER_IP> 9559         # NAOqi port; pacman -S openbsd-netcat if nc is missing
```

* `ping` fails → not on the same network, or wrong IP (ask Pepper again).
* `ping` works but port 9559 does not → client isolation is still on in the router.

### 5. Open the laptop firewall for the robot

The robot calls back into the laptop on a random TCP port (audio stream, signals). `ufw`
blocks that by default and hearing then **fails silently**. Allow everything from the
robot's IP only:

```bash
sudo ufw allow from <PEPPER_IP>
sudo ufw status numbered        # later: sudo ufw delete <n> to remove the rule
```

To open the live preview (step 10) from another device (phone, Pepper's tablet) also run
`sudo ufw allow 8765/tcp`. Not using `ufw`? Allow inbound TCP from the robot in whatever
firewall you have, or pin the callback port with `PEPPER_LISTEN_URL=tcp://0.0.0.0:9560`
and open just that.

### 6. Python environment

```bash
cd ~/Personal/pepper
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[robot,speech,dev]"   # qi + faster-whisper + webrtcvad + pytest/ruff
pytest -q                                 # all tests run against the fake robot
```

Extras: `robot` (`qi`, needed for real hardware), `speech` (local STT + VAD), `agent`
(reference Claude loop, step 12), `vision` (MediaPipe hand-raise detector), `dev` (tests,
ruff). Every command below assumes `source .venv/bin/activate` in that shell (or call
`.venv/bin/pepper-mcp` directly).

The first start with hearing on downloads the faster-whisper model (`small`, ~500 MB)
into the Hugging Face cache; do it once on a good connection.

**GPU speech recognition (NVIDIA, optional).** faster-whisper's ctranslate2 needs the
CUDA **12** cuBLAS/cuDNN libraries; Arch ships a newer CUDA, so install the CUDA 12 wheels
into the venv and point the loader at them:

```bash
uv pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
SP="$PWD/.venv/lib/python3.12/site-packages"
export LD_LIBRARY_PATH="$SP/nvidia/cublas/lib:$SP/nvidia/cudnn/lib"
```

Then add `--stt-device cuda`. Without a GPU use `--stt-device cpu` (and `--stt-model base`
if `small` is too slow).

### 7. Dry run without the robot (optional, 1 minute)

```bash
pepper-mcp --fake --stt none --preview-port 8765 --transport streamable-http
```

Open <http://localhost:8765/> (event log + fake camera), `Ctrl-C` to stop. This proves the
server and the MCP plumbing work before hardware enters the picture.

### 8. Hardware checklist

Clear ~1 m in front of Pepper (the last check drives the base forward for 0.3 s), then:

```bash
pepper-mcp-verify --ip <PEPPER_IP>        # --no-move skips the drive, --quiet skips speech
```

You want `[OK ]` on at least `connect`, `tts`, `audio_callback` (≈ 6 callbacks/s), `camera`
and `move`. A `FAIL` on `audio_callback` almost always means the firewall (step 5) or client
isolation (step 2). What every check proves and its fallback:
[`docs/ROBOT_VERIFICATION.md`](docs/ROBOT_VERIFICATION.md).

### 9. Run the server once by hand

```bash
pepper-mcp --ip <PEPPER_IP> --preview-port 8765 --transport streamable-http
```

On start the server wakes Pepper, turns autonomous life off, applies the liveliness preset
and lowers the base safety distances to 0.15 m / 0.05 m.

### 10. Watch and test it

1. Open <http://localhost:8765/>: the control dashboard. Header: connection, battery,
   posture, people, speaking/hearing state, running tasks and a **STOP** button (also `Esc`).
   Below it: live camera (top/bottom/depth), head and base nudge pads, a say box and volume,
   switches for every reflex (wave/greet, high five, attention, touch, battery), hearing,
   liveliness and autonomous abilities, tracking/follow and perception extractors, one button
   per gesture, mood lights, the conversation transcript, sensors and a filterable event log.
   Controls work from the laptop; another device (phone) needs `--preview-token <secret>` and
   `http://<laptop-ip>:8765/?token=<secret>`, otherwise it gets a view-only page (STOP still works).
2. Stand ~1 m in front and say a sentence. A `speech.final` event with your words should
   appear about a second after you stop talking (the first one is slower while whisper
   loads).
3. Raise a hand facing the robot: `person.waving`, then the high-five reflex. Touch the
   back of its raised hand to finish it.
4. Touch its head: a `touch.*` event and a nod.

Pepper does **not** answer yet – the server only produces events. The LLM comes next.
`Ctrl-C` stops it; on shutdown the server stops the base and restores motion defaults and
autonomous life. Only one process may own the robot, so stop this one before step 11.

### 11. Talk to Pepper through Claude Code

Register the server once as a stdio server; Claude Code then starts and stops it for you:

```bash
cd ~/Personal/pepper
claude mcp add pepper -- "$PWD/.venv/bin/pepper-mcp" --ip <PEPPER_IP> --preview-port 8765
```

With the GPU setup from step 6, pass the library path and device as well:

```bash
SP="$PWD/.venv/lib/python3.12/site-packages"
claude mcp add pepper -e LD_LIBRARY_PATH="$SP/nvidia/cublas/lib:$SP/nvidia/cudnn/lib" \
  -- "$PWD/.venv/bin/pepper-mcp" --ip <PEPPER_IP> --preview-port 8765 --stt-device cuda
```

Then start `claude` in the repo:

1. `/mcp` → `pepper` should be *connected* (takes a few seconds; the robot wakes up).
2. Load the persona prompt: type `/mcp__pepper__pepper_live_agent` and send it.
3. Say something like: *"Greet the room, then keep calling `wait_for_event` and respond to
   whoever talks to you."*
4. Talk to Pepper. Claude receives `speech.final` events, answers with `say`, and uses
   gestures, LEDs, the camera, motion and the tablet. Keep <http://localhost:8765/> open to
   see what it sees and hears.

Quick single-tool tests that need no event loop: *"Say hello and wave"*, *"Take a camera
snapshot and describe what you see"*, *"Set your eyes to green"*, *"Turn around"*.

New IP or different flags: `claude mcp remove pepper`, then add it again.

### 12. Other clients

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "pepper": {
      "command": "/home/<you>/Personal/pepper/.venv/bin/pepper-mcp",
      "args": ["--ip", "192.168.1.5", "--preview-port", "8765"]
    }
  }
}
```

Then ask Claude to load the `pepper_live_agent` prompt and start its loop with
`wait_for_event`.

**Reference agent loop** – lowest latency, because Claude's answer is streamed to the robot
sentence by sentence while the rest is still being generated:

```bash
uv pip install -e ".[robot,speech,agent]"
export ANTHROPIC_API_KEY="$(pass show <your-anthropic-entry>)"   # never write the key into the repo
pepper-mcp-agent --ip <PEPPER_IP> --preview-port 8765
```

Just talk to Pepper; `Ctrl-C` to stop. It takes every `pepper-mcp` flag plus `--model`
(default `claude-opus-5-5`), `--effort low|medium|high` (default `low`), `--persona`,
`--idle-timeout` and `--no-fallbacks` (turns off server-side refusal fallbacks).

**Any MCP client over HTTP:**

```bash
pepper-mcp --ip <PEPPER_IP> --transport streamable-http --host 0.0.0.0 --http-port 8808
```

### 13. Shut down

1. Quit Claude Code / `Ctrl-C` the server or agent. The server stops the base, restores the
   NAOqi safety distances and autonomous life, and releases camera and microphone.
2. Hold the chest button ~3 s until Pepper says goodbye and goes to rest; wait for the
   lights to go off before closing the charging hatch and plugging in the charger.
3. Optional: `sudo ufw delete <n>` for the rule from step 5, and
   `nmcli con down pepper-direct` if you used option C.

If the server was killed instead of stopped, the robot keeps the lowered safety distances
until the next server start or a reboot.

---

## Configuration

Every flag has a `PEPPER_*` environment variable; the flag wins. Full list:
`pepper-mcp --help` and [`src/pepper_mcp/config.py`](src/pepper_mcp/config.py).

| Flag | Env | Default | Purpose |
|---|---|---|---|
| `--ip` / `--port` | `PEPPER_IP` / `PEPPER_PORT` | – / 9559 | robot address |
| `--fake` | `PEPPER_FAKE` | off | in-memory robot, no hardware |
| `--transport` | `PEPPER_MCP_TRANSPORT` | `stdio` | `stdio`, `streamable-http`, `sse` |
| `--host` / `--http-port` | `PEPPER_MCP_HOST` / `PEPPER_MCP_PORT` | 127.0.0.1 / 8808 | HTTP transport bind |
| `--preview-port` | `PEPPER_PREVIEW_PORT` | 0 (off) | control dashboard, MJPEG, WAV, SSE |
| `--preview-token` | `PEPPER_PREVIEW_TOKEN` | empty | lets other devices use the dashboard controls (`/?token=…`); empty = laptop only |
| `--language` | `PEPPER_LANGUAGE` | `English` | TTS language |
| `--stt` | `PEPPER_STT` | `faster-whisper` | `faster-whisper`, `openai`, `none` |
| `--stt-model` / `--stt-device` | `PEPPER_STT_MODEL` / `PEPPER_STT_DEVICE` | `small` / `auto` | whisper size, `cpu`/`cuda` |
| `--stt-language` | `PEPPER_STT_LANGUAGE` | `en` | ISO code, empty = auto-detect |
| – | `PEPPER_STT_URL` / `PEPPER_STT_API_KEY` | OpenAI endpoint / – | for `--stt openai` |
| `--liveliness` | `PEPPER_LIVELINESS` | `calm` | `off`, `calm`, `lively` autonomous movement |
| `--keep-autonomous-life` | `PEPPER_AUTONOMOUS_LIFE=keep` | disable | leave NAOqi autonomous life on |
| `--safety-front` / `--safety-side` | `PEPPER_SAFETY_FRONT_M` / `PEPPER_SAFETY_SIDE_M` | 0.15 / 0.05 | base safety distances in m (NAOqi: 0.4 / 0.1, which blocks most indoor moves) |
| `--barge-in` | `PEPPER_BARGE_IN` | off | people can interrupt Pepper |
| `--reflexes-off a,b` / `--no-reflexes` | `PEPPER_REFLEXES_OFF` / `PEPPER_REFLEXES` | all on | disable reflexes |
| `--no-audio` / `--no-camera` / `--no-perception` | `PEPPER_AUDIO` / `PEPPER_CAMERA` / `PEPPER_PERCEPTION` | on | switch subsystems off |
| `--hand-raise-vision` | `PEPPER_HAND_RAISE_VISION` | off | laptop MediaPipe detector (`vision` extra) |
| `--camera-fps` / `--camera-res` | `PEPPER_CAMERA_FPS` / `PEPPER_CAMERA_RES` | 10 / `qvga` | camera stream |
| `--audio-rate` | `PEPPER_AUDIO_RATE` | 16000 | 16000 = front mic, 48000 = 4 channels |
| – | `PEPPER_LISTEN_URL` | `tcp://0.0.0.0:0` | callback endpoint for the robot |
| – | `PEPPER_VAD_END_SILENCE_MS` / `PEPPER_ECHO_TAIL_MS` | 450 / 350 | end-of-utterance silence, echo gate tail |
| `--log-level` | `PEPPER_LOG_LEVEL` | `INFO` | logs go to stderr |

## How the interaction loop works

```
   person talks ──▶ mic (16 kHz) ──▶ VAD ──▶ STT ──▶ speech.final ──▶ LLM (wait_for_event)
   hand raised  ──▶ ALWavingDetection ──▶ person.waving ──▶ high_five reflex (≈0.2 s) ──▶ reflex.completed
   LLM ──▶ say("first sentence") ──▶ ALAnimatedSpeech (gestures) ──▶ tts.finished
       ──▶ play_gesture / look_at_person / set_leds / camera_snapshot / move_to …
```

The `pepper_live_agent` prompt tells the model how to run this loop; `list_event_types`
documents every event.

## Project layout

```
src/pepper_mcp/
  cli.py, config.py, server.py, runtime.py     entry point, settings, MCP server, wiring
  robot/        NAOqi wrappers (motion, speech, camera, audio_in, perception, …) + fake.py
  speech/       VAD, STT backends, utterance listener (echo gating, barge-in)
  reflexes/     attention, high_five, greet, touch_reaction, low_battery
  tools/        MCP tools, one module per domain (auto-registered)
  vision/       optional MediaPipe hand-raise detector
  assets/       files served to the tablet (GIF, audio)
  preview.py    MJPEG / live audio / SSE side channel; verify.py; agent.py
  dashboard.py  dashboard state + whitelisted actions; dashboard.html is the page
docs/           MCP_ENDPOINTS.md, ARCHITECTURE.md, ROBOT_VERIFICATION.md
tests/          pytest suite against the fake robot
```

Extending: new tool domain → `tools/<name>.py` with `register(mcp, rt)`; new gesture →
`robot/gestures.py` or the `define_gesture` tool at runtime; new reflex → subclass
`reflexes.base.Reflex`; new STT → implement `speech.stt.SpeechToText`.

## Development

```bash
uv pip install -e ".[dev]"
pytest -q              # fake robot, no hardware
ruff check src tests
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `cannot reach NAOqi` | same network? IP still right (press the chest button)? `nc -zv <PEPPER_IP> 9559`; port closed while ping works → client isolation |
| Pepper says it is not connected | it is not on any network: step 2 |
| connects, but no `speech.final` | `pepper-mcp-verify` → `audio_callback` must show ≈ 6 callbacks/s; ufw rule from step 5; router isolation |
| Pepper does not drive / `motion.blocked` | charging hatch open, charger plugged in, or something inside the safety distance; lower it with `--safety-front` or the `set_safety` tool when the space is clear |
| motors limp, nothing moves | emergency stop at the back of the neck is pressed: release it, then `wake_up` |
| head snaps back after moves | `set_liveliness off`, or start with `--liveliness off` |
| speech recognition slow | `--stt-model base`, GPU (step 6) or `--stt openai`; the first utterance after start is always slower |
| Pepper hears itself | echo gate is on while TTS plays; raise `PEPPER_ECHO_TAIL_MS` (e.g. 600; `-e` on `claude mcp add`) |
| `stt_error: libcublas.so.12 … not found` | GPU libs missing: step 6, or `--stt-device cpu` |
| no `person.waving` | `ALWavingDetection` needs a person facing the robot; or `--hand-raise-vision` |
| Pepper never answers | expected without an LLM: connect Claude Code (step 11) or run `pepper-mcp-agent` |
| `/mcp` shows pepper failed | run the exact command from step 11 in a terminal and read stderr; another process may own the robot |
| `qi` wheel not found | Python 3.13+: recreate the venv with `--python 3.12` |
