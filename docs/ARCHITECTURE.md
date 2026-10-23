# Pepper MCP – architecture and real-time design

## 1. Goal

Give a modern LLM a body: every Pepper capability as MCP tools, a continuous feed of
what the robot sees and hears, and a reaction path fast enough that talking to Pepper
feels like talking to a person. Two latency classes are designed for separately:

| Class | Budget | Path |
|---|---|---|
| **Reflex** (high five, look at speaker, greet, LED mood) | < 300 ms | robot event → laptop reflex → NAOqi call. No LLM in the loop. |
| **Conversation** (hear → think → speak) | first words < 1.5 s | mic → VAD end-of-utterance (450 ms) → STT (local `small` model ≈ 400-800 ms, or cloud) → `speech.final` event unblocks `wait_for_event`/`listen` → LLM streams text → `say` per sentence (TTS start ≈ 300 ms) |

Everything else (snapshots, state, long actions) is non-blocking: a tool call never
waits on the robot unless the LLM asks it to (`wait=True`).

## 2. Components

```
                ┌──────────────────────────── laptop ───────────────────────────────┐
                │                                                                   │
 LLM client ────┤ MCP (stdio / streamable-http)                                     │
 (Claude, …)    │   server.py ── tools/*  resources  prompts                        │
                │       │                                                           │
                │   Runtime (runtime.py) ──── EventBus (robot/events.py)            │
                │    │  │  │  │  │  │  │  │             ▲        ▲                   │
                │    │  │  │  │  │  │  │  └ Reflexes ───┘        │ speech.final       │
                │    │  │  │  │  │  │  └── Listener (speech/) ───┘  VAD → STT         │
                │    │  │  │  │  │  └───── CameraStreams → JPEG cache → preview/MJPEG │
                │    │  │  │  │  └──────── MicrophoneStream (qi service, processRemote)│
                │    │  │  │  └─────────── Perception (ALMemory watch → events)       │
                │    │  │  └────────────── Speech queue (ALAnimatedSpeech, futures)   │
                │    │  └───────────────── Motion / Animation / Gestures / Tracker   │
                │    └──────────────────── RobotConnection (qi.Application, services) │
                └────────────────────────────────┬──────────────────────────────────┘
                                                 │ tcp 9559 (qimessaging) + callbacks
                                         ┌───────┴────────┐
                                         │  Pepper NAOqi  │
                                         └────────────────┘
```

### 2.1 Package layout

```
src/pepper_mcp/
  cli.py            argparse entry point (`pepper-mcp`)
  config.py         Settings dataclass (env PEPPER_* + CLI flags)
  server.py         builds the MCPServer, lifespan starts/stops the Runtime
  runtime.py        wires subsystems together, owns start/stop order
  robot/            one module per robot concern, all built on RobotConnection
    connection.py   qi.Application (listening session!), service cache, futures→asyncio
    events.py       EventBus + MemoryWatcher (ALMemory subscribe-or-poll)
    tasks.py        TaskRegistry for long-running actions (task.finished events)
    motion.py       base / joints / posture / safety with velocity watchdog
    speech.py       TTS queue (ALAnimatedSpeech / ALTextToSpeech)
    animation.py    stock animations (discovered at runtime) + tags
    gestures.py     keyframe + procedural gesture registry (high five, 67, clown…)
    perception.py   people / waving / gaze / engagement / touch / sonar / sound
    camera.py       ALVideoDevice grabbers, JPEG cache
    audio_in.py     ALAudioDevice remote module → PCM fan-out
    audio_out.py    ALAudioPlayer / volume / upload
    leds.py, tablet.py, tracker.py, navigation.py, awareness.py, system.py
    fake.py         in-memory fake NAOqi (tests, `--fake` dev mode)
  speech/
    vad.py          WebRTC VAD or energy VAD (20 ms frames)
    stt.py          SpeechToText backends: faster-whisper, OpenAI-compatible HTTP, null
    listener.py     segmenter + echo gate + STT worker → speech.* events
  vision/
    hand_raise.py   optional laptop-side pose detector (MediaPipe) → gesture.hand_raised
  reflexes/         Reflex base + built-ins (attention, high_five, greet, touch, battery)
  tools/            MCP tool modules, one per domain, auto-registered
  preview.py        optional HTTP side channel: MJPEG, live WAV, SSE events, dashboard API
  dashboard.py      dashboard state snapshot + whitelisted actions (dashboard.html = page)
  verify.py         `pepper-mcp-verify`: checklist run against the real robot
  robot/asr.py      on-robot keyword recognition (ALSpeechRecognition) + QiChat dialogs
  agent.py          `pepper-mcp-agent`: reference Claude agent loop (streaming TTS)
```

Every `tools/<domain>.py` exports `register(mcp, rt)`. `tools/__init__.py` imports all
modules in the package and calls `register`, so adding a domain = adding a file.
Every robot module takes the `RobotConnection` and resolves NAOqi services lazily
through `conn.service("ALX")`, so a missing service disables one feature instead of
the server.

## 3. Low-latency decisions

1. **One `qi.Application` with a listening endpoint.** The robot must be able to call
   *us* (audio callbacks, signals). The connection is created with
   `--qi-url=tcp://<robot>:9559 --qi-listen-url=tcp://0.0.0.0:0` so `registerService`
   works (verified offline with libqi-python 3.1.5).
2. **Futures, not threads, for robot calls.** `service.method(args, _async=True)`
   returns a `qi.Future`; `RobotConnection.await_future()` bridges it into asyncio via
   `addCallback` + `loop.call_soon_threadsafe`. Long actions (animations, `moveTo`,
   navigation) become `Task`s whose completion publishes `task.finished`.
3. **Push, not poll, wherever NAOqi allows it.** `MemoryWatcher.watch(key)` first tries
   `ALMemory.subscriber(key).signal.connect(...)`. This qi build raised
   `Invalid signature` on that call in the teleop code, so the watcher falls back to a
   polling loop per key with an explicit period (50-200 ms) and edge detection. The
   rest of the code only sees `Event`s.
4. **Audio is pushed by the robot.** `ALAudioDevice.setClientPreferences(name, 16000, 3, 0)`
   + `subscribe(name)` makes NAOqi call our `processRemote(channels, samples, timestamp,
   buffer)` every ~170 ms with 16-bit PCM from the front microphone. The buffer arrives
   as `bytes` or (with libqi-python 3) as a latin-1 `str`; `audio_in.py` normalises both.
5. **Speech segmentation on the laptop.** 20 ms frames → VAD → utterance when 450 ms of
   trailing silence (`PEPPER_VAD_END_SILENCE_MS`). While the robot speaks (`tts.started`…`tts.finished`
   + 350 ms tail, `PEPPER_ECHO_TAIL_MS`) the gate drops frames so Pepper never transcribes itself. Optional
   barge-in stops TTS when a person starts talking.
6. **STT runs in a worker thread, results are events.** `speech.final` carries the text
   and the measured STT latency. `listen` is sugar for `wait_for_event(["speech.final"])`.
7. **Streaming TTS.** `say` enqueues and returns in < 5 ms; a single worker drains the
   queue with `ALAnimatedSpeech.say(_async=True)` so sentences play back-to-back with
   gestures while the LLM is still generating the next one. `stop_speaking` flushes.
8. **Camera frames are grabbed continuously, encoded lazily.** A grabber thread per
   camera keeps the newest RGB frame; JPEG encoding happens once per distinct frame on
   first request (snapshot tool, resource, MJPEG preview share the cache).
9. **Velocity watchdog.** `move()` re-sends `moveToward` and stops the base when the
   requested duration elapses or the connection drops, so an LLM that forgets to stop
   cannot drive Pepper into a wall.
10. **Reflexes are plain event subscribers** running on the EventBus thread pool.
    They publish `reflex.triggered/completed` so the LLM stays informed and can disable
    them with `set_reflex`.

## 4. Threading model

| Thread / loop | Work |
|---|---|
| asyncio loop (MCP) | tool handlers (`async def` ones), `wait_for_event`, notifications |
| MCP worker threads | synchronous tool bodies (blocking qi calls are fine here) |
| qi network threads | `processRemote`, signal callbacks → hand off immediately to queues |
| `speech-tts` | drains the TTS queue |
| `listener` + `stt` | VAD segmentation / transcription |
| `camera-<id>` | `getImageRemote` loop |
| `memory-poll` | polled ALMemory keys |
| `watchdog` | base velocity expiry, connection health |

Shared state is protected by locks or passed through `queue.Queue`; the EventBus is the
only cross-thread fan-out point and is thread-safe.

## 5. Safety

* Joint targets are clamped to conservative limits (`motion.JOINT_LIMITS`).
* Base velocity commands expire (max 5 s), `stop_motion` is always available and also
  cancels animations, tracking and navigation.
* Collision protection is only disabled for the duration of an emote that needs it and
  always restored in `finally`.
* Base security distances are lowered to 0.15 m front / 0.05 m side at start
  (`--safety-front/--safety-side`; NAOqi's 0.4/0.1 blocks most indoor moves) and restored to
  NAOqi's defaults on a clean shutdown. `set_safety` changes them at runtime and reports the
  applied values.
* `move_to` / `turn` report the distance actually travelled; when NAOqi aborts a base move
  (`ALMotion/MoveFailed`, usually an obstacle in the safety distance) the task ends with
  `ok=false` and a `motion.blocked` event is published.
* The preview dashboard's actions are a fixed whitelist with the same limits as the tools
  (moves clamped to 5 s, refused while follow/navigation own the base). Only loopback clients
  may use them unless `--preview-token` is set; JSON-only bodies, an `Origin` check and an
  IP-or-localhost `Host` check keep other web pages (including DNS rebinding) from posting to
  it. `stop` is accepted from any device so every viewer has an emergency stop.
* Shutdown restores autonomous life (if we disabled it), security distances, stops the
  base, unsubscribes camera and audio.

## 6. Extension points

* **New tool domain:** add `tools/<name>.py` with `register(mcp, rt)`.
* **New gesture:** add to `gestures.BUILTIN` (keyframes in degrees) or call
  `define_gesture` at runtime.
* **New reflex:** subclass `reflexes.base.Reflex`, declare `triggers`, implement
  `on_event`; register in `reflexes/__init__.py`.
* **New STT/VAD backend:** implement `speech.stt.SpeechToText` / `speech.vad.VAD`.
* **New perception source:** `perception.py` → `MemoryWatcher.watch(key, handler)`.

## 7. Verification plan

Offline (this repository, no robot): `pytest` runs the server against `robot/fake.py`,
covering tool registration, event bus wake-ups, listener segmentation with synthetic
audio, gesture limits, task lifecycle, the attention reflex, motion failure reporting and
the in-process MCP client.

On the robot: `pepper-mcp-verify --ip <PEPPER_IP>` walks a checklist (connect,
services, TTS, animated speech, camera frame, microphone callback rate, ALMemory
subscriber vs poll, waving detection availability, animation names, tablet, LEDs, a
tiny base move) and prints what works on this firmware. See
`docs/ROBOT_VERIFICATION.md`.
