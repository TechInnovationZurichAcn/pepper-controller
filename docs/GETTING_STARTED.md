# Getting started: from router to talking with Pepper

Step-by-step bring-up on an Arch Linux laptop. Commands assume the repo is at
`~/Personal/pepper`. Replace `<PEPPER_IP>` with the robot's address (step 2).

## 0. What you need

* Pepper (NAOqi 2.5.x), charged, with the charging hatch on its back **closed**
  (the base refuses to move while it is open).
* A Wi-Fi router you control, with **client/AP isolation off**. The laptop needs internet
  for the LLM (Claude), so either give the router an internet uplink or plug the laptop
  into the router by Ethernet and keep Wi-Fi on your normal network.
* The laptop with this repo and `uv` (`~/.local/bin/uv`).

## 1. Router

1. Power the router and wait until its Wi-Fi is up.
2. In its admin page, turn off "AP isolation" / "client isolation" / "guest mode" on the
   network Pepper will join. The robot has to open connections *to* the laptop
   (microphone audio, ALMemory signals), not just the other way round.
3. Optional but convenient: add a DHCP reservation for Pepper so its IP stops changing.

## 2. Robot onto the network

1. Press the chest button once to power on. Booting takes ~2 minutes; it is done when
   Pepper stands up straight and the eyes light up.
2. If Pepper is not on this router's Wi-Fi yet: on the chest tablet open
   *Settings → Network*, pick the SSID, enter the password. (Alternative: Ethernet cable
   into the port on the back of the head, then open `http://<wired-ip>/` in a browser.)
3. Press the chest button once more: Pepper **says its IP address**. Write it down.

## 3. Laptop onto the network

```bash
nmcli dev wifi connect "<ROUTER_SSID>" password "<ROUTER_PASSWORD>"   # or plug in Ethernet
ip -br addr                     # note your laptop's address on the robot's subnet
ping -c 3 <PEPPER_IP>
nc -zv <PEPPER_IP> 9559         # NAOqi port must be open (pacman -S openbsd-netcat if missing)
```

If `ping` works but port 9559 does not, client isolation is still on in the router.

**Laptop on Ethernet to the router, internet over Wi-Fi:** stop the cable from taking the
default route, otherwise a router without uplink cuts Claude off:

```bash
nmcli con mod "Wired connection 1" ipv4.never-default yes ipv6.never-default yes
ip route get 1.1.1.1            # must say "dev wlp…"
```

No link light on a port means a bad cable: swap it before debugging anything else.

## 4. Open the laptop firewall for the robot

`ufw` is active on this machine. The robot calls back into the laptop on a random TCP
port (audio stream, signals), which ufw blocks by default, so hearing silently fails.
Allow everything from the robot's IP only:

```bash
sudo ufw allow from <PEPPER_IP>
sudo ufw status numbered        # later: sudo ufw delete <n> to remove the rule
```

If you also want to open the preview dashboard from another device (phone, Pepper's
tablet), add `sudo ufw allow 8765/tcp`.

## 5. Python environment (already done once)

`.venv` exists with Python 3.12, `qi 3.1.5`, faster-whisper and the dev extras, and the
faster-whisper `small` model is cached. To recreate it:

```bash
cd ~/Personal/pepper
uv venv --python 3.12 .venv            # qi has no wheels for 3.13+, system python is 3.14
source .venv/bin/activate
uv pip install -e ".[robot,speech,dev]"   # add ,agent for the reference Claude loop
pytest -q                               # 24 tests against the fake robot
```

Every command below assumes `source .venv/bin/activate` in that shell.

**GPU speech recognition (NVIDIA).** faster-whisper's ctranslate2 needs the CUDA **12**
cuBLAS/cuDNN libraries; Arch ships CUDA 13, so install the CUDA 12 wheels into the venv and
point the loader at them:

```bash
uv pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
SP="$PWD/.venv/lib/python3.12/site-packages"
export LD_LIBRARY_PATH="$SP/nvidia/cublas/lib:$SP/nvidia/cudnn/lib"
```

Then add `--stt-device cuda` to `pepper-mcp` / `pepper-mcp-agent`. Without the GPU, use
`--stt-device cpu` (and `--stt-model base` if `small` is too slow).

## 6. Dry run without the robot (optional, 1 minute)

```bash
pepper-mcp --fake --stt none --preview-port 8765 --transport streamable-http
```

Open <http://localhost:8765/> (event log + fake camera). `Ctrl-C` to stop. This proves the
server and the MCP plumbing work before hardware enters the picture.

## 7. Hardware checklist

Clear ~1 m around Pepper (the last check nudges the base forward for 0.3 s), then:

```bash
pepper-mcp-verify --ip <PEPPER_IP>            # --no-move / --quiet to skip motion / speech
```

You want `[OK ]` on at least `connect`, `tts`, `audio_callback` (≈6 callbacks/s), `camera`
and `move`. What each check means and its fallback: `docs/ROBOT_VERIFICATION.md`.
A `FAIL` on `audio_callback` almost always means step 4 (firewall) or step 1 (isolation).

## 8. Run the server and watch it

```bash
pepper-mcp --ip <PEPPER_IP> --preview-port 8765 --transport streamable-http
```

On start the server wakes Pepper and turns autonomous life off. Then:

1. Open <http://localhost:8765/>: live camera and event log.
2. Stand ~1 m in front and say a sentence. A `speech.final` event with your words
   should appear about a second after you stop talking (the first one is slower while
   whisper loads).
3. Raise a hand facing the robot: `person.waving`, then the high-five reflex.
4. Touch its head: `touch.*` event and a nod.

`Ctrl-C` stops it; on shutdown the server stops the base, restores motion defaults and
autonomous life.

## 9. Interact through Claude Code

Stop the step-8 server first (only one process should own the robot). Register the server
with Claude Code as a stdio server; Claude Code then starts and stops it for you:

```bash
cd ~/Personal/pepper
SP="$PWD/.venv/lib/python3.12/site-packages"
claude mcp add pepper -e LD_LIBRARY_PATH="$SP/nvidia/cublas/lib:$SP/nvidia/cudnn/lib" \
  -- "$PWD/.venv/bin/pepper-mcp" --ip <PEPPER_IP> --preview-port 8765 --stt-device cuda
claude
```

Inside Claude Code:

1. `/mcp` → `pepper` should be *connected* (it takes a few seconds; the robot wakes up).
2. Load the persona prompt: type `/mcp__pepper__pepper_live_agent` and send it.
3. Then say something like: *"Greet the room, then keep calling `wait_for_event` and
   respond to whoever talks to you."*
4. Talk to Pepper. Claude receives `speech.final` events, answers with `say`, and can use
   gestures, LEDs, the camera, motion and the tablet. Keep <http://localhost:8765/> open
   to see what it sees and hears.

Quick single-tool tests that do not need the event loop: *"Say hello and wave"*,
*"Take a camera snapshot and describe what you see"*, *"Set your eyes to green"*.

To change flags later: `claude mcp remove pepper` and add it again.

## 10. Alternative: the standalone reference agent

Lower latency than Claude Code (answers are streamed sentence by sentence to the robot):

```bash
uv pip install -e ".[robot,speech,agent]"
export ANTHROPIC_API_KEY="$(pass show <your-anthropic-entry>)"   # never write the key to a file in the repo
pepper-mcp-agent --ip <PEPPER_IP> --preview-port 8765
```

Just talk to Pepper. `Ctrl-C` to stop.

## 11. Shutting down

1. `Ctrl-C` the server / quit Claude Code (the server releases the robot).
2. Hold Pepper's chest button ~3 s until it says goodbye and goes to rest.
3. `sudo ufw delete <n>` for the rule from step 4 if you do not want it permanently.

## 12. When something goes wrong

| Symptom | Check |
|---|---|
| `cannot reach NAOqi` | same subnet as the robot? IP still right (press chest button)? `nc -zv <PEPPER_IP> 9559` |
| connects, but no `speech.final` | `pepper-mcp-verify` → `audio_callback`; ufw rule from step 4; router isolation |
| Pepper does not drive | charging hatch open, or someone/something within its safety distance |
| head snaps back after moves | ask Claude to `set_liveliness off`, or start with `--liveliness off` |
| speech recognition slow | `--stt-model base`; the first utterance after start is always slower |
| Pepper hears itself | `PEPPER_ECHO_TAIL_MS=600` in the server's environment (`-e` on `claude mcp add`) |
| `stt_error: libcublas.so.12 … not found` | GPU libs missing: see "GPU speech recognition" in step 5, or `--stt-device cpu` |
| Pepper never answers | expected without an LLM: the server only produces events; connect Claude Code (step 9) or run `pepper-mcp-agent` |
| `/mcp` shows pepper failed | run the exact command from step 9 in a terminal and read stderr |
