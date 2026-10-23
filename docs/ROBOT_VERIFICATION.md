# Verifying Pepper MCP on the real robot

The code in this branch was developed and tested against an in-memory fake of NAOqi
(`pepper_mcp/robot/fake.py`) plus offline tests of the `qi` library mechanics
(futures, `registerService`, signals). The checklist below confirms the assumptions
that can only be proven on hardware. Run it once per robot/firmware.

## 0. Environment

```bash
# Python 3.10, 3.11 or 3.12 (libqi-python has no 3.13 wheels yet)
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[robot,speech,dev]"
```

The robot must be powered on, reachable on port 9559 and allowed to connect back to the
laptop (firewall rule). README steps 1–5 cover getting there and finding `<PEPPER_IP>`.

## 1. Automated checklist

```bash
pepper-mcp-verify --ip <PEPPER_IP>     # --port, --no-move (skip the drive), --quiet (skip speech)
```

It prints one line per check, then `N/M checks passed` (exit code 1 if any failed):

| Check | What it proves | Fallback if it fails |
|---|---|---|
| connect | `qi.Application` reaches NAOqi and listens for callbacks | network / firewall on the laptop (callbacks need an inbound TCP port) |
| services | which `AL*` services exist on this firmware | features whose service is missing are disabled automatically |
| tts | `ALTextToSpeech.say` async future completes, `ALTextToSpeech/TextDone` fires | speech queue falls back to blocking `say` in its own thread |
| animated_speech | `ALAnimatedSpeech.say` with `bodyLanguageMode=contextual` | `say(animated=False)` |
| memory_subscriber | `ALMemory.subscriber(...).signal.connect` works | polling watcher (automatic) |
| audio_callback | `processRemote` is called ≈ 6×/s with 2730 samples of 16-bit PCM | check `--qi-listen-url`, firewall; try `--audio-rate 48000` |
| camera | QVGA RGB frame from top camera in < 100 ms, depth frame from camera 2 | lower fps / resolution |
| waving | `ALWavingDetection` subscribe + `PeoplePerception/Person/<id>/IsWaving` key | `hand_raise_vision` (MediaPipe) or LLM-vision only |
| animations | `ALBehaviorManager.getInstalledBehaviors()` contains `animations/Stand/...` | curated list in `animation.py` |
| tablet | `showWebview` with a data URL | `tablet_show_url` to the preview server |
| leds | `fadeRGB` on `FaceLeds` | – |
| people | people perception extractors start; count of visible people | perception tools return empty lists |
| move | wake up, `move(0.15, 0, 0)` for 300 ms, then stop | charging hatch closed? `wakeUp`, security distances |

## 2. Manual interaction test

```bash
pepper-mcp --ip <PEPPER_IP> --preview-port 8765 --transport streamable-http
```

1. Open `http://localhost:8765/` – you should see the live camera and an event log.
2. Talk to the robot from ~1 m: a `speech.final` event with your words should appear
   within ~1 s of you finishing the sentence.
3. Raise a hand and hold it up facing the robot: `person.waving` fires, the
   `high_five` reflex raises Pepper's right arm, touching the back of its hand ends the
   gesture with a celebration (`reflex.completed`).
4. Stop the server, connect an MCP client (README step 11 for Claude Code, step 12 for
   Claude Desktop) and run the `pepper_live_agent` prompt, or run
   `pepper-mcp-agent --ip <PEPPER_IP>`.

## 3. Known firmware quirks handled in code

* `ALMemory.subscriber` can raise `Invalid signature` → polling fallback.
* qi service objects have no `.post` → use `_async=True` futures.
* Binary audio buffers may arrive as `str` → decoded as latin-1.
* `ALAudioPlayer.playFileInLoop` blocks → always run in its own thread; `stopAll` ends it.
* `ALTracker.registerTarget` resets the mode → Move mode re-applied after registering.
* `showWebview` can block for seconds → called from a worker thread.
* 16 kHz audio is single-channel only (front mic); 4-channel needs 48 kHz.
* NAOqi's default security distances (0.4 m / 0.1 m) abort most indoor base moves with
  `ALMotion/MoveFailed` → the server lowers them to 0.15 / 0.05 at start and reports aborted
  moves as `motion.blocked`.
