# Pepper MCP – architecture and real-time design

## 1. Goal

Give a modern LLM a body: every Pepper capability as MCP tools, a continuous feed of
what the robot sees and hears, and a reaction path fast enough that talking to Pepper
feels like talking to a person. Two latency classes are designed for separately:

| Class | Budget | Path |
|---|---|---|
| **Reflex** (high five, greet, touch reaction) | < 300 ms | robot event → laptop reflex → NAOqi call. No LLM in the loop. |
| **Conversation** (hear → think → speak) | set by Gemini Live, not measured on this branch | head mic (16 kHz) → Gemini Live over the network (audio both ways, server-side turn detection) → voice (24 kHz) → speaker stream. The server adds only its own buffer (≤0.3 s prebuffer when a reply starts, §2.2) |

Everything else (snapshots, state, long actions) is non-blocking: a tool call never
waits on the robot unless the LLM asks it to (`wait=True`).

## 2. Components

```
  Gemini Live (cloud)                      laptop: pepper-mcp-gemini (gemini_live.py)
  ───────────────────                      ──────────────────────────────────────────────
  mic audio (16 kHz)     ◀──────────────── head mic: MicrophoneStream (processRemote)
  camera frame (1 fps)   ◀──────────────── CameraStreams.snapshot("top")
  voice (24 kHz)         ────────────────▶ AudioOut → SpeakerStream (ALAudioDevice, 48 kHz)
  function calls         ────────────────▶ in-memory MCP client → server.py → tools/*, resources
  [robot event] notes    ◀──────────────── EventBus subscriber (touch, people, motion.blocked, …)

         Runtime (runtime.py): reflexes, motion, gestures, tracker, navigation, tablet, perception,
         speech queue, camera grabbers → JPEG cache → preview/MJPEG
                                   │ tcp 9559 (qimessaging) + callbacks
                           ┌───────┴────────┐
                           │  Pepper NAOqi  │
                           └────────────────┘
```

### 2.1 Package layout

```
src/pepper_mcp/
  cli.py            robot options and signal handling shared by `pepper-mcp-gemini`
  config.py         Settings dataclass (env PEPPER_* + CLI flags)
  server.py         builds the MCPServer (tools and resources) around a Runtime
  runtime.py        wires subsystems together, owns start/stop order
  robot/            one module per robot concern, all built on RobotConnection
    connection.py   qi.Application (listening session!), service cache, futures→asyncio
    events.py       EventBus + MemoryWatcher (ALMemory subscribe-or-poll)
    tasks.py        TaskRegistry for long-running actions (task.finished events)
    motion.py       base / joints / posture / safety with velocity watchdog
    speech.py       TTS queue (ALAnimatedSpeech / ALTextToSpeech)
    animation.py    stock animations (discovered at runtime) + tags
    gestures.py     keyframe + procedural + Choregraphe timeline gestures and groups (high five,
                    67, clown, greeting, dances…)
    qianim.py       .qianim parser → clamped angleInterpolationBezier keys
    perception.py   people / waving / gaze / engagement / touch / sonar / sound
    camera.py       ALVideoDevice grabbers, JPEG cache
    audio_in.py     ALAudioDevice remote module → PCM fan-out
    audio_out.py    ALAudioPlayer / volume / upload
    leds.py, tablet.py, tracker.py, navigation.py, awareness.py, system.py
    tablet_relay.py TCP relay on the head so the tablet (not on the LAN) reaches the preview server
    ssh.py          password SSH to the robot (SSH_ASKPASS), shared by uploads and the relay
    fake.py         in-memory fake NAOqi (tests, `--fake` dev mode)
  reflexes/         Reflex base + built-ins (attention, high_five, greet, touch, battery, idle)
  tools/            MCP tool modules, one per domain, auto-registered (_common.py: @tool decorator,
                    READ_ONLY / ACTION / MOVES_ROBOT annotations, error mapping)
  games/            tic-tac-toe engine + the tablet page it serves (tictactoe.py / .html)
  assets/           files for the tablet and speakers (67 GIF + page, circus music)
  preview.py        optional HTTP side channel: MJPEG, live WAV, SSE events, dashboard API
  dashboard.py      dashboard state snapshot + whitelisted actions (dashboard.html = page)
  verify.py         `pepper-mcp-verify`: checklist run against the real robot
  robot/asr.py      on-robot keyword recognition (ALSpeechRecognition) + QiChat dialogs
  gemini_live.py    `pepper-mcp-gemini`: Gemini Live speech-to-speech agent (see 2.2)
persona.txt         Gemini's system instruction (read at every start)
vocabulary.txt      words that bias Gemini's input transcription
```

Start-up order (`Runtime.start`): connect → ALMemory watcher → disable autonomous life → wake up
(`StandInit`) → base security distances → push recovery off → motion watchdog → liveliness preset →
TTS worker and language → perception → top camera → microphone → reflexes → preview server →
tablet relay → `server.started`. Every step after the connection goes through `_optional`, so a
missing service logs `<feature> disabled: …` (also listed in `robot_status.warnings`) and the rest
starts anyway. `Runtime.stop` runs the reverse and ends by restoring autonomous life and the
NAOqi motion defaults.

### 2.2 Gemini Live agent

`pepper-mcp-gemini` runs the server in-process and calls the robot only through an in-memory
MCP client, driven by one streaming Gemini Live session instead of a text turn loop:

- **In:** `MicrophoneStream` consumer (16 kHz head mic, the exact Live input format) and
  `CameraStreams.snapshot("top")` once per second (the API's maximum). `camera_snapshot` calls
  push a frame into the video input instead of returning an image.
- **Out:** the 24 kHz voice goes to `AudioOut.speaker_stream` → `SpeakerStream`, which upsamples
  to the 48 kHz stereo `ALAudioDevice.sendRemoteBufferToOutput` wants. That call returns
  immediately, so the stream paces itself 0.5 s ahead of playback: enough queued on the robot to
  ride out a Wi-Fi hiccup on one call (150 ms was not; the voice broke off mid-sentence). A
  barge-in or reconnect therefore leaves at most ~0.6 s queued on the robot. When the robot is
  silent (reply start, or after a gap) the voice thread first gathers up to 0.3 s of audio
  (`VOICE_PREBUFFER_S`, waiting at most that long), because Gemini's first chunks trickle in
  at about real-time pace.
- **Tools:** every MCP tool except the 13 in `EXCLUDED_TOOLS`: the speech output tools (Gemini is
  the voice), tools that would fight the session (blocking waits such as `recognize_keywords`,
  `record_clip`, `wait_for_event`, `wait_task`; QiChat dialogs; `camera_stream`), and `upload_audio`,
  which reads files on the laptop. Schemas go through unchanged
  (`parameters_json_schema`), so the server's limits apply. Actions (tools without
  `read_only_hint`, except `analyze_face`) are declared `NON_BLOCKING`: the model keeps talking
  while they run. Their result goes back with `scheduling: SILENT` if the model spoke in the
  turn that called them (the bridge waits up to 0.8 s for it to start), otherwise `WHEN_IDLE`:
  a model that calls `wave` before saying hello is waiting for the result and must get a turn
  to answer. Failures are always `WHEN_IDLE`.
  A blocking call ends the model's turn and the turn after its result is where Gemini filled
  the air with "wave" or "no speech". Queries stay blocking because the answer depends on them.
  Tool calls run as tasks off the receive loop so the voice keeps flowing; each call times out
  after 20 s (`TOOL_TIMEOUT_S`) and the error goes back to the model. `wait` arguments are removed
  from every schema. Models without async function calling (`gemini-3.1-flash-live*`) get plain
  blocking declarations.
- **Hearing:** input transcription gets the default language plus English as `language_codes`
  hints and the phrases in `vocabulary.txt` as `custom_vocabulary`. Without hints it
  auto-detects, which on Pepper's noisy mic drifts into the wrong language. It only feeds the
  transcript and the `speech.*` events; the model listens to the audio itself and replies in
  any language. Turn detection keeps 300 ms before speech and waits for 800 ms of silence, so
  word onsets survive and short pauses don't split a sentence. Proactive audio is on, so the model
  may stay silent when speech in the room is not meant for it.
- **Speech filter:** native audio speaks whatever the model generates, including placeholders.
  `SpeechFilter` holds the first audio of each turn until the output transcript (always on)
  shows real words, usually the first fragment. A turn that is only a marker (`<no speech>`),
  a placeholder ("no speech") or a tool/gesture name ("wave") never reaches the speakers;
  held audio is released after 0.8 s at the latest.
- **Events:** a bus subscriber turns `FORWARDED_EVENTS` into `[robot event]` text notes: people
  appearing, leaving (if they stayed ≥ 3 s) and waving, `touch.start`, high-five results, failed tasks (successful ones
  are dropped: the silent tool result already covered them), `motion.blocked`, `follow.lost/found`,
  `robot.fell`, `battery.low`, `nav.status` and the tic-tac-toe `game.*` events. They are held back
  while Pepper is making sound and sent together afterwards (table in `MCP_ENDPOINTS.md` §4). Other
  `reflex.completed` events are dropped: the body already reacted, and every note makes Gemini
  take a turn, which it would spend narrating the reflex. The persona forbids narrating tools,
  gestures or sensed events.
- **Echo:** no AEC on the robot, so `EchoGate` drops mic audio while Gemini's voice plays, NAOqi
  speaks (`tts.*`, which also covers phrases said by reflexes and gestures) or a task that plays
  sound is active (`play_audio`, `play_sine`, `run_behavior`, gestures with music: `clown_dance`,
  the dances and the `dance` group, from `GestureRegistry.music_gestures()`), plus a
  0.4 s reverb tail. Body movement alone (gestures, animations, reflexes) never mutes the mic,
  so people can talk to Pepper while it moves. Every source expires on its own (≤ 30 s; a music
  gesture's task is held for its duration + 5 s) so a missed event cannot
  leave Pepper deaf. Separately, the dashboard's mic switch sets `rt.mic_muted`, which also sends
  silence and never expires: it stays muted until switched back on.
- **Sessions:** context-window compression keeps audio+video sessions alive past 2 min;
  `go_away` reconnects with the last session-resumption handle.

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
   buffer)` every ~170 ms with 16-bit PCM from the front microphone. With `--mic auto` (default)
   the stream first reads NAOqi's per-mic energy meters and moves to the next mic (left, right,
   rear) when one sits at the noise floor, because a dead head mic is pure silence. The buffer arrives
   as `bytes` or (with libqi-python 3) as a latin-1 `str`; `audio_in.py` normalises both.
5. **No laptop-side segmentation.** Gemini Live's server-side automatic activity detection finds
   the end of each turn in the 16 kHz stream. The bridge only mutes that stream while Pepper makes
   sound (`EchoGate`, §2.2), so Pepper does not transcribe itself.
6. **Streaming TTS.** `say` enqueues and returns in < 5 ms; a single worker drains the
   queue with `ALAnimatedSpeech.say(_async=True)` so sentences play back-to-back with
   gestures while the LLM is still generating the next one. `stop_speaking` flushes.
   Under `pepper-mcp-gemini` Gemini speaks through the speaker stream instead, so `say` is not offered to it.
7. **Camera frames are grabbed continuously, encoded lazily.** A grabber thread per
   camera keeps the newest RGB frame; JPEG encoding happens once per distinct frame on
   first request (snapshot tool, resource, MJPEG preview share the cache).
8. **Velocity watchdog.** `move()` only sets a velocity and a deadline (≤ 5 s); the
   `motion-watchdog` thread re-sends `moveToward` while it lasts and stops the base when it
   elapses, so an LLM that forgets to stop cannot drive Pepper into a wall.
9. **Reflexes are plain event subscribers** running on the EventBus thread pool.
    They publish `reflex.triggered/completed` so the LLM stays informed and can disable
    them with `set_reflex`.

## 4. Threading model

| Thread / loop | Work |
|---|---|
| asyncio loop (MCP) | tool handlers (`async def` ones), `wait_for_event`, notifications |
| MCP worker threads | synchronous tool bodies (blocking qi calls are fine here) |
| qi network threads | `processRemote`, signal callbacks → hand off immediately to queues |
| `speech-tts` | drains the TTS queue |
| asyncio loop (`gemini_live`) | Live session send/receive loops, tool-call tasks, `[robot event]` notes |
| `gemini-voice` | Gemini's voice → `SpeakerStream` (prebuffer, pacing, `tts.started/finished`) |
| `camera-<id>` | `getImageRemote` loop |
| `memory-poll` | polled ALMemory keys |
| `motion-watchdog` | base velocity expiry (stops the base when a `move` duration ends) and `moveToward` keep-alive |
| `task-<id>` | long actions (gestures, animations, moves) registered in `TaskRegistry` |
| `follow`, `attention-look` | follow-person loop; attention reflex head turns |
| `tablet` | serialised `ALTabletService` calls (`showWebview` can block for seconds) |
| `preview-http`, `tablet-relay-*` | dashboard HTTP server; SSH relay process I/O |

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
* An explicit `move` / `move_to` / `turn` ends `follow_person` instead of being refused (the
  dashboard still refuses); navigation keeps the base until `stop_navigation`.
* The preview dashboard's actions are a fixed whitelist with the same limits as the tools
  (moves clamped to 5 s, refused while follow/navigation own the base or a dance runs). Only loopback clients
  may use them unless `--preview-token` is set; JSON-only bodies, an `Origin` check and an
  IP-or-localhost `Host` check keep other web pages (including DNS rebinding) from posting to
  it. `stop` is accepted from any device so every viewer has an emergency stop.
* ALMotion push recovery is turned off at start: on a push it drives the base and plays its own
  "ouch" (`push_recovery_*.wav`, no separate mute) over Gemini's voice. NAOqi only allows it once
  *Permit deactivation of the safety reflexes* is ticked on the robot page; otherwise startup
  warns and it stays on. Fall manager, diagnosis effect and smart stiffness stay on.
* Shutdown restores autonomous life (if we disabled it), security distances, push recovery, stops the
  base, unsubscribes camera and audio. SIGTERM is turned into SIGINT (`cli.stop_on_signals`) so
  `kill`, systemd and the like take this path too; Python's default SIGTERM exit would skip it.

## 6. Extension points

* **New tool domain:** add `tools/<name>.py` with `register(mcp, rt)`.
* **New gesture:** add to `gestures.builtin_gestures()` (keyframes in degrees), call
  `define_gesture` at runtime, or import Choregraphe `.qianim` / `behavior.xar` timelines with
  `python -m pepper_mcp.choregraphe_import` (entries in `assets/choreography.json`).
* **New reflex:** subclass `reflexes.base.Reflex`, declare `triggers`, implement
  `on_event`; register in `reflexes/__init__.py`.
* **New perception source:** `perception.py` → `MemoryWatcher.watch(key, handler)`.

## 7. Verification plan

Offline (this repository, no robot): `pytest` runs the server against `robot/fake.py`,
covering tool registration, event bus wake-ups, gesture limits, task lifecycle, the attention
reflex, motion failure reporting, the dashboard and its access checks, the tablet relay,
tic-tac-toe, and the Gemini bridge helpers (`EchoGate`, `SpeechFilter`, tool declarations, event
notes) through the in-process MCP client. No test talks to Gemini.

On the robot: `pepper-mcp-verify --ip <PEPPER_IP>` walks a checklist (connect,
services, TTS, animated speech, camera frame, microphone callback rate, ALMemory
subscriber vs poll, waving detection availability, animation names, tablet, LEDs, a
tiny base move) and prints what works on this firmware. See
`docs/ROBOT_VERIFICATION.md`.
