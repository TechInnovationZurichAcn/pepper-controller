# pepper-mcp

MCP server that lets an LLM drive a SoftBank **Pepper** (NAOqi 2.5.10.7) in real time:
106 tools, 10 resources, 2 prompts, an event bus and server-side reflexes. Branch
`mcp-server` holds only the server; the old keyboard teleop lives on `main`.

Docs: `README.md` (full bring-up from power-on to talking, config table, troubleshooting),
`docs/ARCHITECTURE.md` (latency design, threads, safety), `docs/MCP_ENDPOINTS.md` (every
tool/resource/prompt/event), `docs/ROBOT_VERIFICATION.md` (hardware checklist, firmware
quirks). When you change a tool, flag, default or event, update the matching doc in the
same change.

## Commands

```bash
source .venv/bin/activate                      # Python 3.12 venv; qi has no wheels for 3.13+
uv pip install -e ".[robot,speech,dev]"        # recreate deps (extras: agent, vision)
pytest -q                                      # whole suite runs against robot/fake.py, no hardware
ruff check src tests                           # lint (config in pyproject.toml)
pepper-mcp --fake --stt none --preview-port 8765 --transport streamable-http   # server without robot
pepper-mcp-verify --ip <PEPPER_IP> [--no-move --quiet]                          # real-robot checklist
```

Robot IP: press the chest button once and Pepper says it (DHCP, it changes). The robot
must be able to connect back to the laptop: `sudo ufw allow from <PEPPER_IP>`.

## Layout

- `cli.py` / `config.py`: every knob is one `Settings` field (env `PEPPER_*`) plus one
  `--flag`; the flag overrides the env.
- `runtime.py`: builds and starts/stops all subsystems; optional ones go through
  `_optional(...)` so one missing NAOqi service disables a feature, not the server.
- `server.py`: MCP server, resources, the `pepper_live_agent` / `pepper_quick_reference`
  prompts. `tools/<domain>.py` each export `register(mcp, rt)` and are auto-registered.
- `robot/`: one module per NAOqi concern, services resolved lazily via `conn.service("ALX")`.
  `robot/fake.py` is the in-memory NAOqi used by tests and `--fake`; extend it whenever a
  robot module starts calling a new NAOqi method.
- `speech/` (VAD → STT → `speech.final`), `reflexes/` (subclass `reflexes.base.Reflex`),
  `vision/` (optional MediaPipe), `preview.py` (HTTP side channel), `agent.py` (reference
  Claude loop, model `claude-opus-5-5`).

## Rules that are easy to break

- **stdout is the MCP protocol on stdio.** Log via `logging` (configured to stderr); never
  `print` from server code. `verify.py` is a standalone CLI and may print.
- **One process owns the robot.** Stop a running `pepper-mcp` before starting another or
  connecting Claude Code.
- **Never block before a timed motion.** `ALTabletService.showWebview` and other slow qi
  calls stall for seconds; run them off the motion thread or in a background task.
- **ALMemory.subscriber raises `Invalid signature` on this robot.** Go through
  `MemoryWatcher.watch` in `robot/events.py`, which falls back to polling; don't call
  `subscriber()` directly.
- **qi calls are futures, not threads.** Use `_async=True` + `RobotConnection.await_future`;
  long actions become `TaskRegistry` tasks that publish `task.finished`.
- **ALTracker:** `registerTarget` resets the mode, so set `Move` mode after registering,
  right before `track()`.
- **Base safety:** server starts with 0.15 / 0.05 m security distances and restores NAOqi's
  0.4 / 0.1 on shutdown (`motion.restore_defaults`). Keep the restore in every shutdown
  path; collision protection disabled for an emote must be re-enabled in `finally`.
- **Motion safety stays on:** joint targets are clamped (`motion.JOINT_LIMITS`), `move`
  durations expire (≤ 5 s watchdog), `stop_motion` must keep cancelling base, animations,
  tracking and navigation.
- **Secrets:** the Anthropic key comes from `pass`; robot SSH password is a `Settings`
  default and must never be logged (`Settings.summary` masks it).

## Tests

Add tests against the fake robot (`tests/conftest.py` fixtures). Bug fixes on hardware
behaviour get a fake-robot regression test (see `tests/test_robot_modules.py`,
`tests/test_attention.py`). Hardware-only assumptions go into `verify.py` and
`docs/ROBOT_VERIFICATION.md`.
