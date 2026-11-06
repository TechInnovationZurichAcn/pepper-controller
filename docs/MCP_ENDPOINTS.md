# Pepper MCP – endpoint inventory

This is the complete list of what the MCP server exposes to the LLM: every robot feature the
`main` branch teleop already used on the real Pepper (NAOqi 2.5.10.7) plus the NAOqi 2.5 services
needed for a lively, real-time conversation partner.

**Status:** 107 tools and 10 resources (one of them a URI template), no prompts, all implemented
in `src/pepper_mcp/tools/*` and `src/pepper_mcp/server.py` and covered by `pytest` against the
in-memory fake robot. They run inside `pepper-mcp-gemini`, which offers 94 of the tools to Gemini
Live; the 13 marked † are not offered (`EXCLUDED_TOOLS` in `src/pepper_mcp/gemini_live.py`). The
tables list every argument with its default; the authoritative schema is the tool's signature.
Firmware-specific behaviour is checked with `pepper-mcp-verify` (see
`docs/ROBOT_VERIFICATION.md`).

To print the live inventory yourself (fake robot, no hardware, no API key):

```bash
python - <<'PY'
import asyncio
from mcp.client import Client
from pepper_mcp.config import Settings
from pepper_mcp.server import create_server
async def main():
    s = Settings(); s.fake = True
    async with Client(create_server(s)) as c:
        for t in (await c.list_tools()).tools:
            print(t.name, list((t.input_schema or {}).get("properties", {})))
asyncio.run(main())
PY
```

Legend – **Source**: where the capability comes from.
`main` = already used by the teleop code on `main`; `naoqi` = NAOqi 2.5 service not
used before; `laptop` = computed on the laptop side by this server.

Every tool is non-blocking unless it has a `wait` flag. Long actions return a
`task_id`; completion arrives as a `task.finished` event (see **Events**). Gemini never sees the
`wait` argument: the bridge strips it from every schema so a call can't freeze the conversation.

---

## 1. Features of Pepper collected from the existing code

| Feature (main branch) | NAOqi service(s) | Reused as |
|---|---|---|
| Connect / diagnose (`pepper_diag.py`) | session, `ALSystem` | `robot_status`, `robot_capabilities`, `list_services` |
| Holonomic base driving (`moveToward`, keep-alive, security distances) | `ALMotion` | `move`, `move_to`, `turn`, `stop_motion`, `set_safety` |
| Head / arm / hip / knee joint control with limits, hands open/close | `ALMotion` | `look`, `move_joint`, `set_joint_angles`, `arm_pose`, `set_hands`, `list_joints` |
| Postures (`StandInit`, `Sit`, …) | `ALRobotPosture` | `go_to_posture` |
| Disable autonomous life / basic awareness / background movement | `ALAutonomousLife`, `ALBasicAwareness`, `ALBackgroundMovement` | `set_liveliness`, `set_autonomous_ability` |
| Live head camera (top/bottom, QVGA/VGA, RGB) | `ALVideoDevice` | `camera_snapshot`, `camera_stream`, `pepper://camera/…` resource, MJPEG preview |
| Text to speech + tablet message | `ALTextToSpeech`, `ALTabletService` | `say`, `tablet_show_text` |
| Follow-me (ALTracker Move mode, people list polling) | `ALTracker`, `ALPeoplePerception`, `ALMemory` | `follow_person`, `track`, `tracking_status` |
| Touch reaction (tactile head + hand sensors) | `ALMemory` keys | `get_touch_state`, `touch.*` events, `touch_reaction` reflex |
| Offline voice commands (word spotting) | `ALSpeechRecognition` | `recognize_keywords` |
| Offline QiChat dialogue | `ALDialog` | `start_builtin_dialog`, `stop_builtin_dialog` |
| Face characteristics (age, gender, smile, expression) | `ALFaceCharacteristics`, `ALPeoplePerception` | `get_people`, `analyze_face` |
| Wave "Hey_1" animation | `ALAnimationPlayer` | `play_animation`, `list_animations` |
| 67 emote (procedural arm rocking + GIF on tablet) | `ALMotion`, `ALTabletService` | gesture `sixty_seven` (`play_gesture`) |
| Clown dance (procedural whole-body + looped music) | `ALMotion`, `ALAudioPlayer`, `ALAudioDevice` | gesture `clown_dance`, `play_audio`, `upload_audio` |
| LEDs: eyes / chest / ears colours | `ALLeds` | `set_leds`, `leds_off`, `eyes_rotate`, `set_mood_lights` |
| Mapping, remembered spots, go-to-spot, multiple maps | `ALNavigation` | `explore_area`, `remember_spot`, `go_to_spot`, `list_spots`, `list_maps`, `switch_map`, `new_map`, `load_map`, `forget_spot`, `navigate_to`, `stop_navigation`, `navigation_status` |
| Tablet settings page, web view hosting of assets | `ALTabletService` | `tablet_open_settings`, `tablet_show_url`, `tablet_show_html` |
| Behaviour autostart / installed behaviours | `ALBehaviorManager` | `list_behaviors`, `run_behavior`, `stop_behavior` |
| Audio playback test / volume | `ALAudioPlayer`, `ALAudioDevice` | `play_audio`, `play_sine`, `stop_audio`, `set_volume`, `get_volume` |

New capabilities added for real-time interaction (not on `main`):

| Capability | Service | Exposed as |
|---|---|---|
| Live microphone stream into the laptop (16 kHz, front mic or the first working one, `--mic`) | `ALAudioDevice` remote module (`processRemote`) | `mic_level`, `record_clip`, `/audio.wav` preview, Gemini Live input |
| Animated speech (gestures synchronised with words) | `ALAnimatedSpeech` | `say(animated=True)` |
| Wave / raised-hand detection (3D sensor) | `ALWavingDetection` | `person.waving` event, `high_five` reflex |
| Gaze (is the person looking at me) | `ALGazeAnalysis` | `get_people[].looking_at_robot`, `person.looking_at_robot` |
| Engagement zones (someone approached) | `ALEngagementZones` | `person.engaged` event, `greet` reflex |
| Sound source direction | `ALSoundLocalization` | `sound.located` event, `get_sound_direction`, `attention` reflex |
| Front/back sonar, battery, fall/push | `ALMemory` keys | `get_sonar`, `get_battery`, `robot.*` events |
| Depth camera snapshot | `ALVideoDevice` camera 2 | `camera_snapshot(camera="depth")` |
| Look-at / point-at a 3D point or a person | `ALTracker` | `look_at_person`, `look_at_point`, `point_at` |
| Local obstacle-avoiding move | `ALNavigation.navigateTo` | `navigate_to` |
| Keyframe gestures defined at runtime by the LLM | laptop | `define_gesture`, `play_gesture` |
| Choregraphe animation library (100 `.qianim` timelines) and 11 dances with music | `ALMotion.angleInterpolationBezier`, `ALAudioPlayer.loadFile`/`play` | `play_gesture`, `list_gestures(tag=…)` |
| Reflex layer (sub-second reactions without an LLM round-trip) | laptop | `list_reflexes`, `set_reflex`, `reflex.*` events |

---

## 2. Tools

† = not offered to Gemini Live (`EXCLUDED_TOOLS`); every other tool is.

### 2.1 Robot & system

| Tool | Arguments | Returns | Source |
|---|---|---|---|
| `robot_status` | – | connection, robot name, NAOqi version, battery %, posture, autonomous-life state, speaking flag, people count, active reflexes, open tasks | main |
| `robot_capabilities` | – | available NAOqi services, cameras, mic state, voice agent, optional features detected | main |
| `list_services` | – | NAOqi service names | main |
| `wake_up` / `rest` | – | ok | main |
| `get_battery` | – | charge %, charging flag | naoqi |
| `set_liveliness` | `preset: off\|calm\|lively`, optional overrides `basic_awareness`, `background_movement`, `listening_movement`, `speaking_movement`, `blinking` | applied settings | main/naoqi |
| `set_autonomous_ability` | `ability: BasicAwareness\|BackgroundMovement\|ListeningMovement\|SpeakingMovement\|AutonomousBlinking`, `enabled` | ok | main |
| `configure_awareness` | `engagement_mode?: Unengaged\|SemiEngaged\|FullyEngaged`, `tracking_mode?: Head\|BodyRotation\|WholeBody\|MoveContextually`, `engage_person_id?` | BasicAwareness state | naoqi |
| `list_behaviors` | `filter?`, `include_animations=false` | installed Choregraphe behaviours | main |
| `run_behavior` / `stop_behavior` | `name`, `wait=false` / `name?` (all running if omitted) | task id / ok | main |
| `get_volume` / `set_volume` | `level 0-100` | level | main |

### 2.2 Speech (output)

| Tool | Arguments | Returns |
|---|---|---|
| `say` † | `text`, `animated=True`, `wait=False`, `language?`, `speed? (50-400 %)`, `pitch? (50-200 %)`, `volume? (0-100)`, `body_language: contextual\|random\|disabled` | `utterance_id`; `tts.finished` event when done |
| `stop_speaking` † | – | ok (also flushes the queue) |
| `speech_status` † | – | speaking flag, queue length, current utterance |
| `set_voice` † | `language?`, `speed?`, `pitch?`, `voice?` | current voice settings |
| `list_languages` † | – | installed TTS languages and voices |

`say` returns immediately and speech is queued in order, so the client can send a
sentence as soon as the LLM finishes generating it (streaming TTS pipeline).

### 2.3 Hearing (input)

| Tool | Arguments | Returns |
|---|---|---|
| `mic_level` | – | the head mic in use, per-mic energies, RMS level, callback rate, age of the last buffer |
| `record_clip` † | `seconds=3 (≤10)` | WAV audio content (16 kHz mono) |
| `recognize_keywords` † | `words[]`, `timeout_s=8`, `word_spotting=True` | the recognised word + confidence (on-robot `ALSpeechRecognition`) |
| `start_builtin_dialog` / `stop_builtin_dialog` † | `qichat_topic` | ok (offline QiChat; exclusive with `recognize_keywords`) |

There is no free-speech tool any more: Gemini Live hears the microphone stream itself
(`pepper-mcp-gemini`, see README step 12).

### 2.4 Motion

| Tool | Arguments | Returns |
|---|---|---|
| `move` | `vx, vy, vtheta ∈ [-1,1]` (default 0), `duration_s=1 (≤ 5)` | ok (velocity, auto-stops when the duration ends – safety watchdog) |
| `move_to` | `x_m, y_m, theta_deg` (default 0), `wait=False` | task id (odometry-based `moveTo`); result reports the distance actually moved, `ok=false` + reason if NAOqi stopped it |
| `turn` | `degrees` (left positive), `wait=False` | task id; result reports the angle actually turned |
| `stop_motion` | – | ok (stops base, animations, tracker, navigation) |
| `go_to_posture` | `name: StandInit\|Stand\|StandZero\|Crouch` (default `StandInit`), `speed=0.6`, `wait=False` | ok |
| `look` | `yaw_deg`, `pitch_deg=0`, `speed=0.3` | ok |
| `look_toward` | `direction: left\|right\|slightly_left\|slightly_right\|up\|down\|center\|up_left\|up_right\|down_left\|down_right`, `speed=0.3` | ok |
| `look_at_person` | `person_id?` (nearest if omitted) | ok |
| `look_at_point` | `x, y, z=1.2`, `frame: robot\|world\|torso`, `speed=0.5`, `whole_body=False` | ok |
| `point_at` | `x, y, z=1.0`, `effector: RArm\|LArm\|Arms`, `frame: robot\|world\|torso`, `speed=0.5` | ok |
| `get_joint_angles` | `names[]?` | angles (rad + deg) |
| `move_joint` | `joint` (enum of all 17 joints), `degrees`, `relative=false`, `speed=0.3` | `{joint, degrees, clamped?}`; hands take 0–100 % open → `{joint, open_percent}` |
| `set_joint_angles` | `angles: {joint: deg}`, `speed=0.3` | ok (clamped to safe limits) |
| `list_joints` | – | names, limits, descriptions |
| `arm_pose` | `side: left\|right\|both`, `pose: down\|up\|forward\|out\|hug\|wave_ready\|high_five`, `speed=0.4` | ok |
| `set_hands` | `left_open?`, `right_open?` | ok |
| `get_pose` | – | odometry x, y, theta |
| `set_safety` | `orthogonal_m?`, `tangential_m?`, `arm_collision_protection?`, `move_collision_protection?` | applied values (server default 0.15 / 0.05 m, NAOqi default 0.4 / 0.1 m) |
| `set_body_lean` | `hip_pitch_deg`, `hip_roll_deg`, `knee_pitch_deg` (default 0), `speed=0.2` | ok (expressive lean / crouch) |

`move`, `move_to`, `turn` and `follow_person` fail at once with "charging hatch is open" when
the flap over the charger socket is open (`Device/SubDeviceList/Platform/ILS/Sensor/Value` = 1):
NAOqi locks the wheels then. `robot_status` reports it as `charging_hatch_open`.

`move`, `move_to` and `turn` take the base from `follow_person` (the result has
`stopped_following: true`), are refused while navigation runs, and wake a resting robot first;
so do the joint tools.

### 2.5 Animations & gestures

| Tool | Arguments | Returns |
|---|---|---|
| `play_animation` | `name` (full path or tag such as `hello`, `yes`, `thinking`), `wait=False` | task id |
| `list_animations` | `filter?`, `category?`, `limit=80` | installed animation paths (discovered live from `ALBehaviorManager`, falls back to the curated list) |
| `play_gesture` | `name`, `wait=False`, `params?` (gesture-specific, e.g. `sixty_seven` `{speak: true}`, a dance `{music: false}`, a group `{variant: "hello_3"}`) | task id |
| `list_gestures` | `tag?` (`dance`, `group`, `greeting`, `deictic`, `emotion`, `idle`, `library`…) | built-in, library and user-defined gestures with descriptions, tags, durations; groups list their `variants`, dances whether their `music` file is present |
| `define_gesture` | `name`, `description`, `keyframes: [{t_s, joints:{name: deg}}]`, `return_to_rest=True`, `speech?` | ok (available immediately in `play_gesture`) |
| `remove_gesture` | `name` | ok (user-defined gestures only) |
| `stop_animation` | – | ok |

Built-in gestures (`robot/gestures.py`): `high_five`, `wave`, `celebrate`, `nod`, `shake_head`,
`hug_open`, `point_forward`, `bow`, `arms_down`, `sixty_seven` (67 meme, GIF on the tablet),
`clown_dance` (~20 s whole-body dance to looped circus music).

Library gestures (`assets/choreography.json`, imported from Choregraphe projects by
`python -m pepper_mcp.choregraphe_import <03_Pepper folder>`), played with
`angleInterpolationBezier`; every key and Bezier handle is clamped to the joint limits, the
first keys are reached over a 0.8 s lead-in, arm collision protection is off while they run and
back on in `finally`, and `stop_motion` / `stop_animation` interrupt them (and their music):

| Kind | Names |
|---|---|
| Random-variant groups (`params.variant` picks one) | `greeting` (10 hellos), `attract` (beckon, 14), `show_tablet` (6), `enumerate` (4), `make_space` (4), `nice_reaction` (2), `sad_reaction` (2), `idle` (20 fidgets), `loading` (8 waiting poses), `back_to_stand` (3), `dance` (the 10 dances with music) |
| Pointing | `point_{far,near}_{front,medium,half}_{left,right}` (front = ahead, medium ≈ 30°, half ≈ 60° to that side; near = something close/low) |
| Showing itself | `show_self`, `show_head`, `show_micro`, `show_shoulders`, `show_body_1..3`, `show_hand_{left,right}_1..2`, `show_hand_both_1..2`, `show_bumper_{left,right}` |
| Single variants | `hello_1..10`, `attract_{left,right}_1..7`, `show_tablet_1..6`, `enumeration_1..4`, `make_space_1..4`, `nice_reaction_1..2`, `sad_reaction_1..2`, `loading_{left,right}_1..4`, `back_to_stand_1..3`, idle ones (`looking_around`, `look_far_left`, `play_with_hand_right`, `motor_sound_left`…) |
| Dances (whole song, on the spot) | `dance_gangnam_style`, `dance_la_bamba`, `dance_relax`, `dance_walk_this_way`, `dance_asereje`, `dance_knights`, `dance_reed_flutes`, `dance_hungarian`, `dance_dango`, `dance_date` (50 s – 1.8 min, with music), `dance_disco` (16 s, no music) |

A running dance (any gesture tagged `dance`: the dances above, the `dance` group, `clown_dance`)
owns the robot until it ends or is stopped (`stop_motion`, `stop_animation`, the dashboard):
`play_gesture` is refused instead of preempting it, background gestures skip, reflexes stay
quiet, and `pepper-mcp-gemini` mutes the mic, drops Gemini's voice, holds event notes and
answers every tool call with an error until the dance is over.

Dance music is uploaded to the robot over SSH on first use (a few seconds), preloaded and started
on the beat at a volume of at least 70 (the previous volume comes back afterwards); without SSH or
without its `.ogg` file in `assets/dances/` the dance runs silently. A joint a dance only uses later holds
still until 1 s before its first key. A new gesture stops the running one and waits for its
cleanup (collision protection, music) before it starts; the `idle` reflex never interrupts one. The
Choregraphe originals also drove the base (`Move To`); those moves are left out. While a gesture
with music runs, Gemini's mic stays muted for its whole length.

### 2.6 Perception

| Tool | Arguments | Returns |
|---|---|---|
| `get_people` | – | per person: id, distance, bearing, position, face seen, waving, looking at robot, engagement zone, age/gender/smile/expression if analysed |
| `analyze_face` | `person_id?` | age, gender, smile, expression |
| `get_touch_state` | – | which tactile sensors are touched |
| `get_sonar` | – | front/back distance (m) |
| `get_sound_direction` | – | last located sound (azimuth, elevation, energy, age) |
| `set_perception` | `people?, waving?, gaze?, engagement?, sound_localization?, face_detection?, face_characteristics?` | active extractors |

### 2.7 Camera

| Tool | Arguments | Returns |
|---|---|---|
| `camera_snapshot` | `camera: top\|bottom\|depth`, `max_width=640`, `quality=80` | JPEG image content (+ timestamp, age ms) |
| `camera_stream` † | `camera`, `enabled=True`, `fps?`, `resolution?: qqvga\|qvga\|vga` | stream state |
| `camera_status` | – | per camera: running, fps achieved, last frame age |

### 2.8 Audio output

| Tool | Arguments | Returns |
|---|---|---|
| `play_audio` | `source` (robot path or http(s) URL), `volume?`, `loop=False`, `wait=False` | task id |
| `play_sine` | `frequency_hz=880`, `duration_s=0.3`, `volume=40` | ok |
| `stop_audio` | – | ok |
| `upload_audio` † | `local_path` | robot path (scp, needs the `nao` password) |

### 2.9 LEDs

| Tool | Arguments | Returns |
|---|---|---|
| `set_leds` | `group: eyes\|left_eye\|right_eye\|chest\|ears\|shoulders\|all`, `color` (name or `#rrggbb`), `fade_s=0.3` | ok |
| `leds_off` / `leds_reset` | `group=all` | ok |
| `eyes_rotate` | `color=cyan`, `duration_s=2`, `speed_s=0.5` | ok |
| `set_mood_lights` | `mood: neutral\|listening\|thinking\|speaking\|happy\|alert\|sad\|error` | ok |
| `list_led_groups` | – | group names |

### 2.10 Tablet

| Tool | Arguments | Returns |
|---|---|---|
| `tablet_show_text` | `text`, `background=#0b1020`, `color=#eaf2ff`, `font_px=64` | ok |
| `tablet_show_url` / `tablet_show_image` / `tablet_show_html` | `url` / `url` / `html` | ok |
| `tablet_hide` | – | ok |
| `tablet_execute_js` | `script` | ok |
| `tablet_play_video` / `tablet_stop_video` | `url` | ok |
| `tablet_set_brightness` | `level 0-1` | ok |
| `tablet_open_settings` | – | ok |
| `tablet_status` | – | availability, what is shown, Wi-Fi status |

Tablet calls reach the robot one at a time, in the order they were made, so the last thing asked
for is what stays on screen. While a tic-tac-toe board is shown, the tablet is held by the game:
`tablet_show_*`, `tablet_hide` and `tablet_play_video` fail with "the tictactoe board is on the
tablet …" until `tictactoe_stop`.

#### Games

| Tool | Arguments | Returns |
|---|---|---|
| `tictactoe_start` | `human_symbol: X\|O` (default X), `human_first=True`, `difficulty: easy\|medium\|hard` (default medium) | state + `display` (`interactive` with `--preview-port`, `static`, `unavailable`) + `hint` |
| `tictactoe_move` | `cell: 1-9` (the person's spoken move) | state after Pepper's reply |
| `tictactoe_state` | – | board rows, turn, result, score |
| `tictactoe_stop` | `hide_tablet` (default true) | final state; releases the tablet |

How a game runs: `tictactoe_start` puts the board on the tablet. `display` says how:
`interactive` = the page `/game/tictactoe` served by the preview server and loaded through the
tablet relay; the person taps a square, the page posts the move to `/api/game/tictactoe/move`
and Pepper answers about 0.7 s later on its own (Gemini only comments). The page's *Play again*
button starts a new game (`game.started`). `static` = no preview server or relay: a numbered board
is shown and moves come by voice through `tictactoe_move`. `unavailable` = no tablet; the board is
described in words. Moves and results arrive as `game.*` events.

### 2.11 Tracking & following

| Tool | Arguments | Returns |
|---|---|---|
| `follow_person` | `enabled`, `distance_m=0.5` (0.5–2; 0.5 is the closest the base can still drive) | state; `hint` when nobody is visible yet, `person_lost` while the person is out of sight |
| `track` | `target: face\|people\|sound`, `mode: head\|body_rotation\|whole_body\|move`, `enabled` | state |
| `tracking_status` | – | active target/mode |

While `follow_person` or `track` runs, BasicAwareness is paused (it drives ALTracker too and would
re-target it) and resumed when tracking stops. The follow loop re-arms ALTracker if NAOqi stops it.

### 2.12 Navigation & maps

| Tool | Arguments | Returns |
|---|---|---|
| `navigate_to` | `x_m`, `y_m=0`, `wait=False` | task id (obstacle-avoiding, robot frame) |
| `explore_area` | `radius_m=5` | task id (minutes; `nav.status` events) |
| `stop_navigation` | – | ok |
| `navigation_status` | – | busy, status text, current map, localized |
| `remember_spot` | `name?` | the saved spot (current position, needs localization) |
| `forget_spot` | `name?` (omit = all spots of the current map) | remaining spots |
| `go_to_spot` | `name`, `wait=False` | task id |
| `list_spots` / `list_maps` | – | names |
| `new_map` / `switch_map` | `name?` / `name` | map state |
| `load_map` | – | reloads the current map and localizes at its origin |

### 2.13 Real-time events & tasks

| Tool | Arguments | Returns |
|---|---|---|
| `wait_for_event` † | `types[]?`, `timeout_s=30`, `since_seq?`, `ignore_internal=True` | events that arrived (returns as soon as the first matching event arrives) |
| `get_events` | `since_seq=0`, `types[]?`, `limit=50` | recent events |
| `list_event_types` | – | event catalogue with payload descriptions |
| `list_tasks` | `limit=20` | running and recent tasks |
| `wait_task` † / `cancel_task` | `task_id`, `timeout_s=30` (wait only) | task state |

### 2.14 Reflexes

| Tool | Arguments | Returns |
|---|---|---|
| `list_reflexes` | – | name, enabled, description, parameters, trigger count |
| `set_reflex` | `name`, `enabled?`, `params?` | new state |

Built-in reflexes (`src/pepper_mcp/reflexes/`). Switch them with `set_reflex`, the dashboard,
`--reflexes-off a,b` or `--no-reflexes`; `list_reflexes` shows each one's `params`.

| Reflex | Default | Trigger → reaction |
|---|---|---|
| `attention` | on | `speech.*` / `tts.*` / `sound.located` → eyes cyan while a person talks, purple until Pepper answers (back to neutral after 8 s), white while Pepper talks; the head turns toward whoever is talking. Pepper's own voice is never taken for a speaker |
| `high_five` | on | `person.waving` → right arm up, wait for a touch on the back of the hand, celebrate. Silent unless `params.phrase` / `success_phrase` are set; the result goes to Gemini |
| `greet` | **off** | `person.appeared` after nobody was around → the `greeting` gesture, a random one of ten hello animations (`params.gesture`; Gemini greets with words itself) |
| `touch_reaction` | on | head touched → eyes flash green and the head tilts briefly; hand touched → that hand closes for a moment; bumpers ignored |
| `low_battery` | on | battery below 15 % → chest LED orange; speaks only if `params.phrase` is set (Gemini gets `battery.low`) |
| `idle` | **off** | nobody seen, no task or gesture and no speech/touch for `params.idle_after_s` (30 s) → the `idle` gesture group every `params.interval_s` (30 s); publishes nothing, so Gemini is not told about each fidget |

No reflex or built-in gesture speaks in NAOqi's voice by default, so nothing talks over Gemini;
`sixty_seven` only chants "six seven" with `params.speak=true`.

---

## 3. Resources

| URI | Content |
|---|---|
| `pepper://status` | same as `robot_status` (JSON) |
| `pepper://capabilities` | same as `robot_capabilities` |
| `pepper://people` | current people snapshot |
| `pepper://events` | last 100 events |
| `pepper://transcript` | the Gemini conversation (`role` user or robot, `text`, `at`) plus robot TTS history |
| `pepper://camera/{camera}/latest.jpg` | latest JPEG frame (`top`, `bottom`, `depth`) |
| `pepper://gestures` | gesture registry |
| `pepper://animations` | animation list |
| `pepper://reflexes` | reflex state |
| `pepper://events/types` | event catalogue |

## 4. Events (payload summary)

Everything that happens on the robot goes through one in-process `EventBus` (`robot/events.py`).
`get_events`, `list_event_types`, `pepper://events`, the dashboard's event log and `/events` (SSE)
all read it. The **→ Gemini** column marks the events the voice bridge forwards to Gemini as
`[robot event] <type> {json}` notes (`FORWARDED_EVENTS` and `format_event` in `gemini_live.py`);
the notes wait until Pepper has stopped making sound.

| Type | Payload | → Gemini |
|---|---|---|
| `speech.started` | `{at}` a person started talking; published by the Gemini bridge from Gemini's input transcription, `at` backdated by ~0.8 s for the transcription delay | – |
| `speech.final` | `{text, started_at, ended_at}` the utterance ended (Gemini started answering, the turn ended, or 1.5 s without new words) | – |
| `speech.aborted` | `{reason, started_at}` the "utterance" had no words (e.g. only a noise marker) | – |
| `speech.keyword` | `{word, confidence}` from `recognize_keywords` | – |
| `tts.started` / `tts.finished` | `{utterance_id, text}`; NAOqi TTS (`say`, reflex phrases) and Gemini's voice (`utterance_id` `gemini-N`, empty `text`, one pair per reply) | – |
| `person.appeared` / `person.left` | `{person_id, distance_m, bearing_deg, …}` / `{person_id, last_distance_m, present_for_s}` | `appeared` yes; `left` only if the person was there ≥ 3 s |
| `person.waving` | `{person_id, side: left\|right\|center}` | yes |
| `person.looking_at_robot` | `{person_id, looking}` | – |
| `person.engaged` | `{person_id, zone}` | – |
| `touch.start` / `touch.end` | `{sensor}` | `touch.start` |
| `sound.located` | `{azimuth_deg (robot frame, left positive), head_azimuth_deg, elevation_deg, energy, confidence}` | – |
| `reflex.triggered` / `reflex.completed` | `{reflex, result}` e.g. high five `{touched: true}` | `high_five` results only |
| `task.finished` | `{task_id, kind, ok, error?}` | failures only (`ok=false`) |
| `nav.status` | `{status, busy}` | yes |
| `motion.blocked` | `{reason}` – NAOqi stopped a base move, usually an obstacle inside the safety distance | yes |
| `follow.lost` / `follow.found` | `{unseen_s}` / `{people}` – while following, nobody perceived for 3 s / someone seen again (both forwarded to Gemini) | yes |
| `robot.fell` / `robot.pushed` / `robot.hot_joint` / `battery.low` | sensor payloads (`hot_joint`: `{joints}`, `battery.low`: `{percent}` below 15 %) | `robot.fell`, `battery.low` |
| `tablet.touched` | `{x, y}` | – |
| `game.started` | `{game_id, human, pepper, first, difficulty}` – tic-tac-toe started (tool or the tablet's *Play again*) | yes |
| `game.move` | `{player: human\|pepper, cell 1-9, rows, next}` | yes, except a human move Pepper is about to answer |
| `game.over` | `{result: human\|pepper\|draw\|stopped, winning_cells, rows, score}` | yes |
| `tracking.changed` | follow/track state changed | – |
| `robot.stopped` | `stop_motion` (or the dashboard STOP) ran; what was stopped | – |
| `server.started` | `{version, robot, startup_s}` | – |

## 5. Transports

There is no MCP network endpoint: no stdio, streamable-HTTP or SSE transport. `pepper-mcp-gemini`
runs the server in-process, and Gemini Live's function calls reach the tools through an in-memory
MCP client. External MCP clients (Claude Desktop, Claude Code, …) are not a supported way to run it.

* Side channel for humans and non-MCP consumers (optional, `--preview-port`, `preview.py`):

  | Path | Method | What |
  |---|---|---|
  | `/` | GET | the control dashboard (`dashboard.html`) |
  | `/api/state` | GET | dashboard state (see below) |
  | `/api/action/<name>` | POST | dashboard actions (see below) |
  | `/status` | GET | `robot_status` as JSON |
  | `/events` | GET | server-sent events, one per bus event |
  | `/camera/<top\|bottom\|depth>.mjpg` / `.jpg` | GET | live MJPEG stream / latest JPEG |
  | `/audio.wav` | GET | live head-mic audio (16 kHz PCM) |
  | `/assets/<file>` | GET | bundled files from `src/pepper_mcp/assets/` (67 GIF, circus music) for the tablet |
  | `/game/tictactoe` | GET | the tic-tac-toe page shown on the tablet |
  | `/api/game/tictactoe` | GET | game state |
  | `/api/game/tictactoe/move` / `/new` | POST | a tapped cell `{cell}` / *Play again*; open to the LAN because the tablet is another device and a game move cannot move the robot (Host, JSON and Origin checks still apply) |

  The tablet itself reaches these URLs through the TCP relay on Pepper's head
  (`robot/tablet_relay.py`), never directly.
* Dashboard at `/` on the same port: `GET /api/state` (status, reflexes, perception,
  abilities, cameras, volume, gestures, `playing` (running gestures), `dancing` (the running
  dance or null), transcript; `control` says whether this client may act)
  and `POST /api/action/<name>` with a JSON body. Actions: `stop`, `wake_up`, `rest`,
  `reflex {name, enabled}`, `perception {name, enabled}`,
  `liveliness {preset}`, `ability {name, enabled}`,
  `tracking {enabled, target, mode}`, `follow {enabled, distance_m?}`, `gesture {name}`,
  `stop_animation`, `stop_gesture` (stops the running gesture or dance and its music, then
  returns to StandInit; the dashboard sends it when a playing gesture button is pressed again),
  `move {vx, vy, vtheta, duration_s}`, `turn {degrees}`, `look {direction}`,
  `say {text}`, `stop_speaking`, `volume {level}`, `mood {mood}`, `camera {camera, enabled}`,
  `mic {enabled}` (false mutes: the voice agent gets silence until it is switched back on).
  While a dance runs, `gesture`, `say`, `move`, `turn` and `look` are refused.
  Allowed from loopback; other hosts need `--preview-token` sent as `X-Pepper-Token`
  (except `stop`, which any client may send).
