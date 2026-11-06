# pepper-mcp

Gemini Live drives a SoftBank **Pepper** (NAOqi 2.5.10.7) in real time through `pepper-mcp-gemini`,
which runs this package's MCP server in-process: 107 tools, 10 resources (no prompts), an event bus
and server-side reflexes. Branch `mcp-server` holds only the server and voice agent; the old keyboard
teleop lives on `main`.

Docs: `README.md` (full bring-up from power-on to talking, config table, troubleshooting),
`docs/ARCHITECTURE.md` (latency design, threads, safety), `docs/MCP_ENDPOINTS.md` (every
tool/resource/event), `docs/ROBOT_VERIFICATION.md` (hardware checklist, firmware
quirks). When you change a tool, flag, default or event, update the matching doc in the
same change.

## Commands

```bash
source .venv/bin/activate                      # Python 3.12 venv; qi has no wheels for 3.13+
uv pip install -e ".[all,dev]"                 # recreate deps (all = robot)
pytest -q                                      # whole suite runs against robot/fake.py, no hardware
ruff check src tests                           # lint (config in pyproject.toml)
pepper-mcp-gemini --fake --preview-port 8765                                  # voice agent on the fake robot (needs .env key)
pepper-mcp-verify --ip <PEPPER_IP> [--no-move --quiet]                          # real-robot checklist
```

Robot IP: press the chest button once and Pepper says it (DHCP, it changes). The robot
must be able to connect back to the laptop: `sudo ufw allow from <PEPPER_IP>` (firewalld:
`firewall-cmd --zone=trusted --add-source=<PEPPER_IP>`). Fresh Arch/Ubuntu/macOS install: README Part 1.

## Layout

- `cli.py` / `config.py`: every knob is one `Settings` field (env `PEPPER_*`) plus one
  `--flag`; the flag overrides the env.
- `runtime.py`: builds and starts/stops all subsystems; optional ones go through
  `_optional(...)` so one missing NAOqi service disables a feature, not the server.
- `server.py`: MCP server and resources (no prompts). `tools/<domain>.py` each export
  `register(mcp, rt)` and are auto-registered.
- `robot/`: one module per NAOqi concern, services resolved lazily via `conn.service("ALX")`.
  `robot/fake.py` is the in-memory NAOqi used by tests and `--fake`; extend it whenever a
  robot module starts calling a new NAOqi method.
- `reflexes/` (subclass `reflexes.base.Reflex`), `preview.py`
  (HTTP side channel), `gemini_live.py`
  (`pepper-mcp-gemini`, Gemini Live on Pepper's own mic/camera/speakers; key `GEMINI_LIVE_KEY` from the git-ignored `.env`;
  system instruction read from `persona.txt` in the repo root at startup, plus the optional event knowledge in `knowledge.txt`; transcription vocabulary from `vocabulary.txt`).

## Rules that are easy to break

- **stdout is for the conversation.** `gemini_live.py` prints the `[heard]`/`[said]`/`[tool]`
  lines there; everything else logs via `logging` (configured to stderr). Never `print` from robot,
  tool or reflex code. `verify.py` is a standalone CLI and may print.
- **One process owns the robot.** Stop a running `pepper-mcp-gemini` before starting another.
- **The tablet is not on the LAN.** Pages it loads must use `rt.tablet_base_url()` (relay on the
  head, `robot/tablet_relay.py`), never `preview.base_url`, or the tablet shows a blank page.
- **Never block before a timed motion.** `ALTabletService.showWebview` and other slow qi
  calls stall for seconds; run them off the motion thread or in a background task.
- **ALMemory.subscriber raises `Invalid signature` on this robot.** Go through
  `MemoryWatcher.watch` in `robot/events.py`, which falls back to polling; don't call
  `subscriber()` directly.
- **qi calls are futures, not threads.** Use `_async=True` + `RobotConnection.await_future`;
  long actions become `TaskRegistry` tasks that publish `task.finished`.
- **ALTracker:** `registerTarget` resets the mode, so set `Move` mode after registering,
  right before `track()`.
- **Charging hatch closed or nothing drives.** NAOqi locks the wheels while the flap on the lower
  back is open and reports it as a "Safety" stop; `Motion.check_base_ready` turns that into a clear
  error. Check it first when the base won't move.
- **Base safety:** server starts with 0.15 / 0.05 m security distances and restores NAOqi's
  0.4 / 0.1 on shutdown (`motion.restore_defaults`, which also turns push recovery back on).
  Keep the restore in every shutdown path; collision protection disabled for an emote must be re-enabled in `finally`.
- **Motion safety stays on:** joint targets are clamped (`motion.JOINT_LIMITS`), `move`
  durations expire (≤ 5 s watchdog), `stop_motion` must keep cancelling base, animations,
  tracking and navigation.
- **Secrets:** the Gemini key lives only in the git-ignored `.env`; robot SSH password is a
  `Settings` default and must never be logged (`Settings.summary` masks it).

## Tests

Add tests against the fake robot (`tests/conftest.py` fixtures). Bug fixes on hardware
behaviour get a fake-robot regression test (see `tests/test_robot_modules.py`,
`tests/test_attention.py`). Hardware-only assumptions go into `verify.py` and
`docs/ROBOT_VERIFICATION.md`.
