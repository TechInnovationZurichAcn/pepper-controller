# Pepper MCP – endpoint inventory

This is the complete list of what the MCP server exposes to the LLM, derived from
every robot feature the repository already proved on the real Pepper (NAOqi
2.5.10.7, `main` branch teleop code) plus the NAOqi 2.5 services needed for a
lively, real-time conversation partner.

**Status:** all tools, resources and prompts listed here are implemented in
`src/pepper_mcp/tools/*` and exercised end-to-end against the in-memory fake robot
(`pytest`, 106 tools over stdio and streamable-HTTP). Hardware behaviour still has to be
confirmed once with `pepper-mcp-verify` – see `docs/ROBOT_VERIFICATION.md`.

Legend – **Source**: where the capability comes from.
`main` = already used by the teleop code on `main`; `naoqi` = NAOqi 2.5 service not
used before; `laptop` = computed on the laptop side by this server.

Every tool is non-blocking unless it has a `wait` flag. Long actions return a
`task_id`; completion arrives as a `task.finished` event (see **Events**).

---

## 1. Features of Pepper collected from the existing code

| Feature (main branch) | NAOqi service(s) | Reused as |
|---|---|---|
| Connect / diagnose (`pepper_diag.py`) | session, `ALSystem` | `robot_status`, `robot_capabilities`, `list_services` |
| Holonomic base driving (`moveToward`, keep-alive, security distances) | `ALMotion` | `move`, `move_to`, `turn`, `stop_motion`, `set_safety` |
| Head / arm / hip / knee joint control with limits, hands open/close | `ALMotion` | `look`, `set_joint_angles`, `arm_pose`, `set_hands`, `list_joints` |
| Postures (`StandInit`, `Sit`, …) | `ALRobotPosture` | `go_to_posture` |
| Disable autonomous life / basic awareness / background movement | `ALAutonomousLife`, `ALBasicAwareness`, `ALBackgroundMovement` | `set_autonomous_life`, `set_liveliness`, `set_autonomous_ability` |
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
| Live microphone stream into the laptop (16 kHz front mic) | `ALAudioDevice` remote module (`processRemote`) | `listen`, `speech.*` events, `mic_level`, `record_clip`, `/audio.wav` preview |
| Speech-to-text (local faster-whisper or OpenAI-compatible HTTP) with VAD and echo gating | laptop | `listen`, `set_listening`, `get_transcript` |
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
| Reflex layer (sub-second reactions without an LLM round-trip) | laptop | `list_reflexes`, `set_reflex`, `reflex.*` events |

---

## 2. Tools

### 2.1 Robot & system

| Tool | Arguments | Returns | Source |
|---|---|---|---|
| `robot_status` | – | connection, robot name, NAOqi version, battery %, posture, autonomous-life state, speaking/listening flags, people count, active reflexes, open tasks | main |
| `robot_capabilities` | – | available NAOqi services, cameras, STT backend, mic state, optional features detected | main |
| `list_services` | – | NAOqi service names | main |
| `wake_up` / `rest` | – | ok | main |
| `get_battery` | – | charge %, charging flag | naoqi |
| `set_autonomous_life` | `state: disabled\|solitary\|interactive\|safeguard` | new state | main |
| `set_liveliness` | `preset: off\|calm\|lively`, optional overrides | applied settings | main/naoqi |
| `set_autonomous_ability` | `ability: BasicAwareness\|BackgroundMovement\|ListeningMovement\|SpeakingMovement\|AutonomousBlinking`, `enabled` | ok | main |
| `configure_awareness` | `engagement_mode?`, `tracking_mode?`, `engage_person_id?` | BasicAwareness state | naoqi |
| `list_behaviors` | `filter?` | installed Choregraphe behaviours | main |
| `run_behavior` / `stop_behavior` | `name`, `wait?` | task id / ok | main |
| `get_volume` / `set_volume` | `level 0-100` | level | main |

### 2.2 Speech (output)

| Tool | Arguments | Returns |
|---|---|---|
| `say` | `text`, `animated=True`, `wait=False`, `language?`, `speed? (50-400 %)`, `pitch? (50-200 %)`, `volume? (0-100)`, `body_language: contextual\|random\|disabled` | `utterance_id`; `tts.finished` event when done |
| `stop_speaking` | – | ok (also flushes the queue) |
| `speech_status` | – | speaking flag, queue length, current utterance |
| `set_voice` | `language?`, `speed?`, `pitch?`, `voice?` | current voice settings |
| `list_languages` | – | installed TTS languages and voices |

`say` returns immediately and speech is queued in order, so the client can send a
sentence as soon as the LLM finishes generating it (streaming TTS pipeline).

### 2.3 Hearing (input)

| Tool | Arguments | Returns |
|---|---|---|
| `listen` | `timeout_s=20` | the next recognised utterance `{text, language, started_at, ended_at, stt_latency_ms}` or `{timeout: true}` |
| `set_listening` | `enabled`, `barge_in?` | listener state |
| `get_transcript` | `limit=20` | recent utterances with timestamps |
| `mic_level` | – | current RMS level, speech-detected flag |
| `record_clip` | `seconds (≤10)` | WAV audio content (16 kHz mono) |
| `recognize_keywords` | `words[]`, `timeout_s`, `word_spotting=True` | the recognised word + confidence (on-robot `ALSpeechRecognition`, no STT needed) |
| `start_builtin_dialog` / `stop_builtin_dialog` | `qichat_topic` | ok (offline QiChat; exclusive with `recognize_keywords`) |

### 2.4 Motion

| Tool | Arguments | Returns |
|---|---|---|
| `move` | `vx, vy, vtheta ∈ [-1,1]`, `duration_s ≤ 5` | ok (velocity, auto-stops when the duration ends – safety watchdog) |
| `move_to` | `x_m, y_m, theta_deg`, `wait=False` | task id (odometry-based `moveTo`) |
| `turn` | `degrees`, `wait=False` | task id |
| `stop_motion` | – | ok (stops base, animations, tracker, navigation) |
| `go_to_posture` | `name: StandInit\|Stand\|StandZero\|Crouch\|Sit?`, `speed` | ok |
| `look` | `yaw_deg`, `pitch_deg`, `speed=0.3` | ok |
| `look_toward` | `direction: left\|right\|up\|down\|center\|slightly_left…` | ok |
| `look_at_person` | `person_id?` (nearest if omitted) | ok |
| `look_at_point` / `point_at` | `x,y,z`, `frame: robot\|world`, `effector?` | ok |
| `get_joint_angles` | `names[]?` | angles (rad + deg) |
| `set_joint_angles` | `angles: {joint: deg}`, `speed`, `wait` | ok (clamped to safe limits) |
| `list_joints` | – | names, limits, descriptions |
| `arm_pose` | `side: left\|right\|both`, `pose: down\|up\|forward\|out\|hug\|wave_ready` | ok |
| `set_hands` | `left_open?`, `right_open?` | ok |
| `get_pose` | – | odometry x, y, theta |
| `set_safety` | `orthogonal_m?`, `tangential_m?`, `arm_collision_protection?`, `move_collision_protection?` | applied values |
| `set_body_lean` | `hip_pitch_deg`, `hip_roll_deg`, `knee_pitch_deg`, `speed` | ok (expressive lean / crouch) |

### 2.5 Animations & gestures

| Tool | Arguments | Returns |
|---|---|---|
| `play_animation` | `name` (full path or tag such as `hello`, `yes`, `thinking`), `wait=False` | task id |
| `list_animations` | `filter?`, `category?` | installed animation paths (discovered live from `ALBehaviorManager`, falls back to the curated list) |
| `play_gesture` | `name`, `wait=False`, `params?` | task id |
| `list_gestures` | – | built-in + user-defined gestures with descriptions |
| `define_gesture` | `name`, `description`, `keyframes: [{t_s, joints:{name: deg}}]`, `return_to_rest`, `speech?` | ok (available immediately in `play_gesture`) |
| `remove_gesture` | `name` | ok (user-defined gestures only) |
| `stop_animation` | – | ok |

Built-in gestures: `high_five`, `wave`, `celebrate`, `nod`, `shake_head`,
`hug_open`, `point_forward`, `thinking_pose`, `bow`, `sixty_seven`, `clown_dance`.

### 2.6 Perception

| Tool | Arguments | Returns |
|---|---|---|
| `get_people` | – | per person: id, distance, bearing, position, face seen, waving, looking at robot, engagement zone, age/gender/smile/expression if analysed |
| `analyze_face` | `person_id?` | age, gender, smile, expression |
| `get_touch_state` | – | which tactile sensors are touched |
| `get_sonar` | – | front/back distance (m) |
| `get_sound_direction` | – | last located sound (azimuth, elevation, energy, age) |
| `set_perception` | `people?, waving?, gaze?, engagement?, sound_localization?, face_detection?, hand_raise_vision?` | active extractors |

### 2.7 Camera

| Tool | Arguments | Returns |
|---|---|---|
| `camera_snapshot` | `camera: top\|bottom\|depth`, `max_width=640`, `quality=80` | JPEG image content (+ timestamp, age ms) |
| `camera_stream` | `camera`, `enabled`, `fps`, `resolution: qvga\|vga` | stream state |
| `camera_status` | – | per camera: running, fps achieved, last frame age |

### 2.8 Audio output

| Tool | Arguments | Returns |
|---|---|---|
| `play_audio` | `source` (robot path or http(s) URL), `volume`, `loop`, `wait` | task id |
| `play_sine` | `frequency_hz`, `duration_s`, `volume` | ok |
| `stop_audio` | – | ok |
| `upload_audio` | `local_path` | robot path (scp, needs the `nao` password) |

### 2.9 LEDs

| Tool | Arguments | Returns |
|---|---|---|
| `set_leds` | `group: eyes\|chest\|ears\|shoulders\|all`, `color` (name or `#rrggbb`), `fade_s` | ok |
| `leds_off` / `leds_reset` | `group` | ok |
| `eyes_rotate` | `color`, `duration_s`, `speed_s` | ok |
| `set_mood_lights` | `mood: neutral\|listening\|thinking\|speaking\|happy\|alert\|sad` | ok |
| `list_led_groups` | – | group names |

### 2.10 Tablet

| Tool | Arguments | Returns |
|---|---|---|
| `tablet_show_text` | `text`, `background`, `color`, `font_px` | ok |
| `tablet_show_url` / `tablet_show_image` / `tablet_show_html` | `url` / `url` / `html` | ok |
| `tablet_hide` | – | ok |
| `tablet_execute_js` | `script` | ok |
| `tablet_play_video` / `tablet_stop_video` | `url` | ok |
| `tablet_set_brightness` | `0-1` | ok |
| `tablet_open_settings` | – | ok |
| `tablet_status` | – | availability, what is shown, Wi-Fi status |

### 2.11 Tracking & following

| Tool | Arguments | Returns |
|---|---|---|
| `follow_person` | `enabled`, `distance_m=0.6` | state |
| `track` | `target: face\|people\|sound`, `mode: head\|body_rotation\|whole_body\|move`, `enabled` | state |
| `tracking_status` | – | active target/mode |

### 2.12 Navigation & maps

| Tool | Arguments | Returns |
|---|---|---|
| `navigate_to` | `x_m, y_m`, `wait` | task id (obstacle-avoiding, robot frame) |
| `explore_area` | `radius_m` | task id (minutes; `nav.status` events) |
| `stop_navigation` | – | ok |
| `navigation_status` | – | busy, status text, current map, localized |
| `remember_spot` / `forget_spot` | `name?` | spot |
| `go_to_spot` | `name` | task id |
| `list_spots` / `list_maps` | – | names |
| `new_map` / `switch_map` / `load_map` | `name?` | map state |

### 2.13 Real-time events & tasks

| Tool | Arguments | Returns |
|---|---|---|
| `wait_for_event` | `types[]?`, `timeout_s=30`, `since_seq?` | events that arrived (returns as soon as the first matching event arrives) |
| `get_events` | `since_seq?`, `types[]?`, `limit=50` | recent events |
| `list_event_types` | – | event catalogue with payload descriptions |
| `list_tasks` / `wait_task` / `cancel_task` | `task_id`, `timeout_s` | task state |

### 2.14 Reflexes

| Tool | Arguments | Returns |
|---|---|---|
| `list_reflexes` | – | name, enabled, description, parameters, trigger count |
| `set_reflex` | `name`, `enabled?`, `params?` | new state |

Built-in reflexes: `attention` (eye colour + look toward the person talking; plain noises are ignored),
`high_five` (raised hand → arm up, wait for touch, celebrate), `greet` (new person
→ look + wave), `touch_reaction` (head/hand touch → LED + gesture), `low_battery`.

---

## 3. Resources

| URI | Content |
|---|---|
| `pepper://status` | same as `robot_status` (JSON) |
| `pepper://capabilities` | same as `robot_capabilities` |
| `pepper://people` | current people snapshot |
| `pepper://events` | last 100 events |
| `pepper://transcript` | recent heard utterances + spoken sentences |
| `pepper://camera/{camera}/latest.jpg` | latest JPEG frame (`top`, `bottom`, `depth`) |
| `pepper://gestures` | gesture registry |
| `pepper://animations` | animation list |
| `pepper://reflexes` | reflex state |
| `pepper://events/types` | event catalogue |

## 4. Prompts

| Prompt | Purpose |
|---|---|
| `pepper_live_agent` | System prompt that turns the LLM into a lively robot persona: event loop with `wait_for_event`, short sentences, `say` early and often, gestures with speech, how to handle high fives, safety rules |
| `pepper_quick_reference` | One-page cheat sheet of tools and events |

## 5. Events (payload summary)

| Type | Payload |
|---|---|
| `speech.started` / `speech.final` / `speech.aborted` | user speech detected / `{text, language, stt_latency_ms, duration_s}` |
| `tts.started` / `tts.finished` | `{utterance_id, text}` |
| `person.appeared` / `person.left` | `{person_id, distance_m, bearing_deg}` |
| `person.waving` | `{person_id, side: left\|right\|center}` |
| `person.looking_at_robot` | `{person_id, looking}` |
| `person.engaged` | `{person_id, zone}` |
| `gesture.hand_raised` | `{side, confidence}` (laptop vision, optional) |
| `touch.start` / `touch.end` | `{sensor}` |
| `sound.located` | `{azimuth_deg, elevation_deg, energy, confidence}` |
| `reflex.triggered` / `reflex.completed` | `{reflex, result}` e.g. high five `{touched: true}` |
| `task.finished` | `{task_id, kind, ok, error?}` |
| `nav.status` | `{status, busy}` |
| `robot.fell` / `robot.pushed` / `battery.low` / `hot_joint` | sensor payloads |
| `tablet.touched` | `{x, y}` |
| `connection.lost` / `connection.restored` | – |

## 6. Transports

* `stdio` (default) – for Claude Desktop / Claude Code / any local MCP client.
* `streamable-http` – `pepper-mcp --transport streamable-http --host 0.0.0.0 --port 8808`.
* Side channel for humans and non-MCP consumers (optional, `--preview-port`):
  `http://host:port/camera/top.mjpg` (live MJPEG), `/camera/top.jpg`,
  `/audio.wav` (live 16 kHz PCM), `/events` (server-sent events), `/status`.
