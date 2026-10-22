# Pepper MCP

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives a modern LLM a
body: **SoftBank Pepper** (NAOqi 2.5) seen, heard, spoken through and moved in real time.

* **Every robot feature as a tool** – speech with gestures, free-speech hearing, camera
  snapshots, people/face/touch/sound perception, base and joint motion, animations and
  custom gestures, LEDs, tablet, following, navigation and maps, behaviours. Full list in
  [`docs/MCP_ENDPOINTS.md`](docs/MCP_ENDPOINTS.md).
* **Real-time by design** – the robot pushes audio and ALMemory events to the laptop, the
  server turns them into events, and the LLM blocks on `wait_for_event` instead of polling.
  Speech is queued sentence by sentence so Pepper starts talking while the LLM is still
  writing. Design notes in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
* **Reflexes** – high five when somebody raises a hand, a wave when someone shows up,
  eye colours that follow the conversation, a nod when touched: sub-second reactions that
  run on the server and report back to the LLM as events. All toggleable.
* **Works without a robot** – `--fake` runs the whole stack against an in-memory NAOqi so
  you can wire up your LLM client first.

This branch contains only the MCP server. The keyboard teleop lives on `main`.

---

## 1. Requirements

* Pepper (NAOqi 2.5.x) on the same network as your computer, no client isolation
  (port 9559 **and** a callback port on your machine must be reachable).
* Python **3.10 – 3.12** (libqi-python has no 3.13 wheels yet).
* Optional for hearing: a CPU able to run `faster-whisper` `small` (~1 s per sentence) or an
  OpenAI-compatible transcription API key.

## 2. Install

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate     # or python3.12 -m venv .venv
uv pip install -e ".[robot,speech]"                            # qi + faster-whisper + webrtcvad
# extras: [agent] reference Claude loop, [vision] MediaPipe hand-raise detector, [dev] tests
```

## 3. Run

```bash
pepper-mcp-verify --ip <PEPPER_IP>            # hardware checklist (see docs/ROBOT_VERIFICATION.md)
pepper-mcp --ip <PEPPER_IP>                   # MCP over stdio (Claude Desktop / Claude Code)
pepper-mcp --ip <PEPPER_IP> --preview-port 8765   # + live dashboard at http://<laptop>:8765/
pepper-mcp --ip <PEPPER_IP> --transport streamable-http --host 0.0.0.0 --http-port 8808
pepper-mcp --fake --stt none                  # no robot, for client development
```

Useful flags: `--stt faster-whisper|openai|none`, `--stt-model tiny|base|small|medium`,
`--stt-language de`, `--language German`, `--liveliness off|calm|lively`, `--barge-in`,
`--reflexes-off greet,touch_reaction`, `--no-camera`, `--no-audio`,
`--keep-autonomous-life`. Everything is also an environment variable (`PEPPER_IP`,
`PEPPER_STT`, …) – see `pepper-mcp --help` and `src/pepper_mcp/config.py`.

### Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "pepper": {
      "command": "/path/to/.venv/bin/pepper-mcp",
      "args": ["--ip", "192.168.1.42", "--preview-port", "8765"]
    }
  }
}
```

Then ask Claude to load the `pepper_live_agent` prompt and to start its event loop with
`wait_for_event`.

### Reference agent loop

```bash
uv pip install -e ".[robot,speech,agent]"
export ANTHROPIC_API_KEY=...          # or `ant auth login`
pepper-mcp-agent --ip <PEPPER_IP>
```

`pepper-mcp-agent` starts the server in-process, streams Claude's answers and sends every
finished sentence to `say` while the rest is still being generated. It uses
`claude-opus-5-5` with low effort for snappy replies and server-side refusal fallbacks
(`--no-fallbacks` to turn those off).

## 4. How the interaction loop works

```
   person talks ──▶ mic (16 kHz) ──▶ VAD ──▶ STT ──▶ speech.final ──▶ LLM (wait_for_event)
   hand raised  ──▶ ALWavingDetection ──▶ person.waving ──▶ high_five reflex (≈0.2 s) ──▶ reflex.completed
   LLM ──▶ say("first sentence") ──▶ ALAnimatedSpeech (gestures) ──▶ tts.finished
       ──▶ play_gesture / look_at_person / set_leds / camera_snapshot …
```

The `pepper_live_agent` prompt (`prompts/get`) tells the model exactly how to run this
loop. Events are documented by `list_event_types`.

## 5. Project layout

```
src/pepper_mcp/
  cli.py, config.py, server.py, runtime.py     entry point, settings, MCP server, wiring
  robot/        NAOqi wrappers (motion, speech, camera, audio_in, perception, …) + fake.py
  speech/       VAD, STT backends, utterance listener (echo gating, barge-in)
  reflexes/     attention, high_five, greet, touch_reaction, low_battery
  tools/        MCP tools, one module per domain (auto-registered)
  vision/       optional MediaPipe hand-raise detector
  preview.py    MJPEG / live audio / SSE side channel; verify.py; agent.py
docs/           MCP_ENDPOINTS.md, ARCHITECTURE.md, ROBOT_VERIFICATION.md
tests/          pytest suite against the fake robot
```

Extending: new tool domain → `tools/<name>.py` with `register(mcp, rt)`; new gesture →
`robot/gestures.py` or the `define_gesture` tool at runtime; new reflex → subclass
`reflexes.base.Reflex`; new STT → implement `speech.stt.SpeechToText`.

## 6. Development

```bash
uv pip install -e ".[dev]"
pytest            # fake robot, no hardware
ruff check src tests
```

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `cannot reach NAOqi` | same Wi-Fi? right IP (chest button)? port 9559 blocked by client isolation → own router |
| no `speech.final` events | run `pepper-mcp-verify`: `audio_callback` must show ~6 callbacks/s; your firewall must allow inbound TCP from the robot (set `PEPPER_LISTEN_URL=tcp://0.0.0.0:9560` and open it) |
| hearing slow | `--stt-model base` or `--stt openai`; lower `--vad-end-silence` via `PEPPER_VAD_END_SILENCE_MS=300` |
| Pepper hears itself | echo gate is on while TTS plays; raise `PEPPER_ECHO_TAIL_MS` if the room echoes |
| head snaps back | `set_liveliness off` (BasicAwareness/BackgroundMovement fight explicit head moves) |
| no `person.waving` | `ALWavingDetection` needs the 3D sensor and a person facing the robot; or `--hand-raise-vision` |
| `qi` wheel not found | Python 3.13+ – use 3.12 |
