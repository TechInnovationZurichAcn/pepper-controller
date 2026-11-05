# Verifying Pepper MCP on the real robot

The code in this branch was developed and tested against an in-memory fake of NAOqi
(`pepper_mcp/robot/fake.py`) plus offline tests of the `qi` library mechanics
(futures, `registerService`, signals). The checklist below confirms the assumptions
that can only be proven on hardware. Run it once per robot/firmware.

## 0. Environment

```bash
# x86_64 Linux, Python 3.10-3.12 (libqi-python has no 3.13 or ARM Linux wheels).
# Fresh Arch/Ubuntu machine: README Part 1 (steps 1-5) installs everything.
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[all,dev]"
```

The robot must be powered on, reachable on port 9559 and allowed to connect back to the
laptop (firewall rule). README steps 6–10 cover getting there and finding `<PEPPER_IP>`.

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
| audio_callback | `processRemote` is called ≈ 6×/s with 2730 samples of 16-bit PCM; `energies` similar on all four mics | check `--qi-listen-url` and the firewall (README step 10). One mic far below the others = dead mic, `--mic auto` skips it |
| camera | QVGA RGB frame from top camera in < 100 ms, depth frame from camera 2 | lower fps / resolution |
| waving | `ALWavingDetection` subscribe + `PeoplePerception/Person/<id>/IsWaving` key | `hand_raise_vision` (MediaPipe) or LLM-vision only |
| animations | `ALBehaviorManager.getInstalledBehaviors()` contains `animations/Stand/...` | curated list in `animation.py` |
| tablet | `showWebview` with a data URL | – (laptop pages reach the tablet through the relay on the head, section 3) |
| leds | `fadeRGB` on `FaceLeds` | – |
| people | people perception extractors start; count of visible people | perception tools return empty lists |
| charging hatch | `Device/SubDeviceList/Platform/ILS/Sensor/Value` is 0 (closed) | close the flap on the lower back: NAOqi locks the wheels while it is open |
| move | wake up, `move(0.15, 0, 0)` for 300 ms, then stop | charging hatch closed? `wakeUp`, security distances |

## 2. Manual interaction test

```bash
pepper-mcp-gemini --ip <PEPPER_IP> --preview-port 8765 --liveliness off
```

1. Open `http://localhost:8765/` – you should see the live camera, the event log and the
   conversation panel.
2. Talk to the robot from ~1 m, after it has finished any sound: your words should appear as
   `[heard]` in the terminal and in the conversation panel, and Gemini should answer through the
   speakers.
3. Raise a hand and hold it up facing the robot: `person.waving` fires, the
   `high_five` reflex raises Pepper's right arm, touching the back of its hand ends the
   gesture with a celebration (`reflex.completed`).
4. Stand ~1 m in front of the robot and play the `wave` gesture (dashboard or
   `play_gesture`): the right hand must go well above the head and swing side to side
   three times, then lower. If it stops at shoulder height, arm collision protection is
   still on (`get_safety` → `collision_protection_arms`). Then put the arms in odd poses
   (`arm_pose` left `hug`, right `high_five`) and play `arms_down`: both arms must end fully
   down along the sides, forearms not twisted inward.
5. Stop with `Ctrl-C` before starting another process that owns the robot (README step 14).

## 3. Known firmware quirks handled in code

* `ALMemory.subscriber` can raise `Invalid signature` → polling fallback.
* qi service objects have no `.post` → use `_async=True` futures.
* Binary audio buffers may arrive as `str` → decoded as latin-1.
* External arm collision protection freezes an arm that rises toward a person in front
  → gestures aimed at people (`wave`, `high_five`, emotes) switch it off and back on in `finally`.
* `ALAudioPlayer.playFileInLoop` blocks → always run in its own thread; `stopAll` ends it.
* `ALTracker.registerTarget` resets the mode → Move mode re-applied after registering.
* `showWebview` can block for seconds → called from a worker thread, one call at a time, in order.
* The tablet's screen goes to sleep after a while (autonomous life disabled), and a sleeping
  tablet answers `showWebview(url)` with `False` and shows nothing (the tic-tac-toe board never
  appeared) → when a show call returns `False`, the worker calls `wakeUp` and retries (up to 3
  times, then logs `tablet showWebview refused`).
* The tablet is not on the LAN: it sits on the head's internal USB network (head `198.18.0.1`,
  tablet `198.18.0.2`), the head does not forward it (`ip_forward` = 0) and the tablet's own
  Wi-Fi is usually `DISCONNECTED` (`ALTabletService.getWifiStatus`). A page on the laptop's
  preview server therefore loads as a blank white page → with `--preview-port`, the server
  starts a small TCP relay on the head over SSH (`robot/tablet_relay.py`,
  `198.18.0.1:<port>` → laptop) and points the tablet there (`tablet_url` in `robot_status`).
  Without a relay and without tablet Wi-Fi, tic-tac-toe falls back to a static voice-only board.
* 16 kHz audio is single-channel only (channel 1 left, 2 right, 3 front, 4 rear); 4-channel
  needs 48 kHz and is interleaved left, right, front, rear.
* A head mic can fail on its own: on our robot (2026-11) the front mic went flat at the ADC
  floor (energy ~72, the others 270–450 from the fans), so Pepper heard nothing → `--mic auto`
  probes `get{Front,Left,Right,Rear}MicEnergy` at start and skips mics below 40 % of the loudest.
* NAOqi's default security distances (0.4 m / 0.1 m) abort most indoor base moves with
  `ALMotion/MoveFailed` → the server lowers them to 0.15 / 0.05 at start and reports aborted
  moves as `motion.blocked`.
* An open charging hatch (`Platform/ILS` = 1) locks the wheels and every base move fails with
  `ALMotion/MoveFailed` cause "Safety", the same cause as an obstacle; the lasers showed nothing
  closer than 0.43 m. On 2026-11-05 this made "turn 180" and follow-me do nothing until the flap
  was closed → base tools check the hatch first and say so.
* Follow distance: 0.5 m is the closest that keeps driving (base front ~0.2 m + 0.15 m front
  safety distance, ALTracker measuring to the head ~0.1 m behind the toes); `follow_person`
  clamps to it.
* `follow_person` right after start reported `robot_tracking: false` and Pepper never drove:
  BasicAwareness (liveliness `lively`) also drives ALTracker and took the target over → follow and
  track pause it (`pauseAwareness`) and resume it on stop; the follow loop re-arms the tracker
  whenever `isActive()` turns false.
