"""
Pepper teleop: drive the robot from your laptop with the keyboard while
watching a live feed from its head camera.

A single pygame window shows the camera POV and captures keys. Movement uses
ALMotion.moveToward (normalized velocities, non-blocking), so held keys drive
continuously and releasing them stops the base.

Controls (holonomic base, "strafe" scheme):
    W / S ......... drive forward / backward   (x)
    A / D ......... strafe left / right         (y)
    Q / E ......... rotate left / right         (theta)
    CTRL + WASD/QE  move the HEAD instead of the base (W/S tilt, A/D/Q/E pan)
    SHIFT + WASD/QE  move the HIP instead of the base: W/S lean forward/back,
                    A/D lean left/right, Q/E crouch/rise (knee)
    G ............. recenter the head
    H / J ......... left arm up / down
    L / K ......... right arm up / down
    O / P ......... open / close both hands (grab & release; hands are open/close only)
    ENTER ......... type a message; ENTER again shows it on the tablet + says it (ESC cancels)
    CTRL + M ...... open/close the menu (follow-me, voice commands, dialogue,
                    don't-touch-me, face characteristics, hi emote, 67 emote, clown
                    dance, navigation/mapping, LEDs) — Up/Down move, ENTER select, ESC back
                    Navigation submenu: explore & map the area, remember the current
                    spot, then drive back to any remembered spot (ALNavigation).
                    Voice commands (menu): say "move forward/backward", "turn left/right",
                    "stop", "look left", "raise left arm", "follow me", "wave", "red eyes",
                    "explore the area", "remember this spot"...
    [ / ] ......... decrease / increase speed scale
    1 / 2 / ... ... select camera by index (1=top, 2=bottom, 3/4 if present)
    C ............. cycle top <-> bottom camera
    F ............. toggle fullscreen
    SPACE ......... emergency stop (halts the base)
    ESC ........... quit (stops the base, unsubscribes camera)

The window is resizable; drag its edge to resize. Frames are scaled to fit.

Requires: Python 3 venv with `qi` and `pygame`.
Usage:
    python pepper_teleop.py --ip <PEPPER_IP>
Find <PEPPER_IP> by pressing Pepper's chest button (it says its IP aloud).
"""

from __future__ import print_function
import argparse
import html as html_lib
import os
import sys
import threading
import time

try:
    from urllib.parse import quote  # Python 3
except ImportError:
    from urllib import quote  # Python 2 fallback

try:
    import qi
except ImportError:
    print("ERROR: couldn't import `qi`. Activate the venv: source ~/pepper-venv/bin/activate")
    sys.exit(1)

try:
    import pygame
except ImportError:
    print("ERROR: couldn't import `pygame`. Install it: uv pip install pygame")
    sys.exit(1)

import pepper_features as feat
import pepper_navigation as nav_feat


# --- NAOqi video constants (ALVideoDevice) ---
CAM_TOP = 0
CAM_BOTTOM = 1
RES_QVGA = 1          # 320x240
RES_VGA = 2           # 640x480
COLOR_RGB = 11        # kRGBColorSpace, 3 bytes/pixel

# --- teleop tuning ---
DEFAULT_SPEED = 0.9   # fraction of max velocity for normal driving
# Pepper brakes early to keep a safety bubble; lower it so teleop actually moves.
# Trade-off: less obstacle margin. Restored to defaults on exit. (Base still caps ~0.35 m/s.)
TELEOP_ORTHO_SECURITY = 0.1    # default 0.4
TELEOP_TANGENT_SECURITY = 0.05  # default 0.1
DEFAULT_ORTHO_SECURITY = 0.4
DEFAULT_TANGENT_SECURITY = 0.1
SPEED_STEP = 0.1
SPEED_MIN, SPEED_MAX = 0.1, 1.0
KEEPALIVE_MS = 250    # re-send the current velocity at least this often while held

# Head (hold CTRL) + arm control. We keep our own target angle for each managed
# joint and re-assert it with setAngles EVERY frame, which overrides Pepper's
# background head-recentering (the reason manual head moves snapped back).
# Rates are in rad/SECOND (frame-rate independent) so motion is smooth and fast.
HEAD_RATE = 1.6       # rad/s the head target moves while a head key is held
ARM_RATE = 2.0        # rad/s the arm target moves while an arm key is held
HIP_RATE = 0.8        # rad/s the hip/knee target moves while a hip key is held (slower = stable)
JOINT_SPEED = 0.5     # fraction of max joint speed; high so joints track the target

HEAD_JOINTS = ["HeadYaw", "HeadPitch"]
ARM_JOINTS = ["LShoulderPitch", "RShoulderPitch"]
HIP_JOINTS = ["HipRoll", "HipPitch", "KneePitch"]  # lean L/R, lean fwd/back, crouch/rise
MANAGED_JOINTS = HEAD_JOINTS + ARM_JOINTS + HIP_JOINTS
# (min, max) rad clamps, kept safely inside the real joint limits.
JOINT_LIMITS = {
    "HeadYaw": (-2.0, 2.0),
    "HeadPitch": (-0.6, 0.5),      # negative = look up
    "LShoulderPitch": (-2.0, 2.0),  # negative = arm up, positive = arm down
    "RShoulderPitch": (-2.0, 2.0),
    "HipRoll": (-0.45, 0.45),      # real limit ~+-0.51; + = lean robot's left
    "HipPitch": (-0.9, 0.9),       # real limit ~+-1.04; + = lean forward
    "KneePitch": (-0.45, 0.45),    # real limit ~+-0.51; - = crouch, + = rise
}
# Voice commands: discrete phrases move the base at a fixed velocity for a short
# burst, then it auto-stops. Rates are fractions of max; durations in ms.
VOICE_MOVE_SPEED = 0.7
VOICE_TURN_SPEED = 0.6
VOICE_MOVE_MS = 1400
VOICE_TURN_MS = 1200

WINDOW_SCALE = 2      # initial display magnification of the camera frame
HUD_COLOR = (0, 255, 0)

# starting window size per requested camera resolution (frames are scaled to fit)
RES_DIMS = {RES_QVGA: (320, 240), RES_VGA: (640, 480)}


def connect(ip, port):
    session = qi.Session()
    url = "tcp://{}:{}".format(ip, port)
    try:
        session.connect(url)
    except RuntimeError:
        print("ERROR: can't reach NAOqi at {}. Same Wi-Fi? Right IP? Port 9559 open?".format(url))
        sys.exit(1)
    return session


def quiet_autonomous_head(session):
    """Stop every autonomous behavior that moves the head on its own — tracking
    people (ALBasicAwareness), idle 'looking around' (ALBackgroundMovement), and
    listening/speaking head sway. Otherwise these fight our held head position."""
    try:
        life = session.service("ALAutonomousLife")
        if life.getState() != "disabled":
            life.setState("disabled")
        # Belt-and-suspenders: also switch off each head-moving ability by name.
        for ability in ("BasicAwareness", "BackgroundMovement",
                        "ListeningMovement", "SpeakingMovement"):
            try:
                life.setAutonomousAbilityEnabled(ability, False)
            except Exception:
                pass
    except Exception as exc:
        print("WARN: couldn't disable AutonomousLife: {}".format(exc))

    # ALBasicAwareness (head tracks people/sound) can run independently of
    # AutonomousLife; stop it every way this build might expose.
    try:
        aware = session.service("ALBasicAwareness")
        for method, arg in (("setEnabled", False), ("pauseAwareness", None),
                            ("stopAwareness", None)):
            try:
                fn = getattr(aware, method)
                fn(arg) if arg is not None else fn()
            except Exception:
                pass
    except Exception:
        pass

    try:
        session.service("ALBackgroundMovement").setEnabled(False)
    except Exception as exc:
        print("WARN: couldn't disable ALBackgroundMovement: {}".format(exc))


def prepare_robot(session):
    """Take the robot out of autonomous life and wake it so it can drive."""
    quiet_autonomous_head(session)
    motion = session.service("ALMotion")
    try:
        motion.wakeUp()
        motion.setStiffnesses("Head", 1.0)
        motion.setStiffnesses("LArm", 1.0)
        motion.setStiffnesses("RArm", 1.0)
        motion.setStiffnesses(["HipRoll", "HipPitch", "KneePitch"], 1.0)  # for SHIFT hip control
    except Exception as exc:
        print("WARN: wakeUp/stiffness failed: {}".format(exc))
    try:
        posture = session.service("ALRobotPosture")
        posture.goToPosture("StandInit", 0.6)
    except Exception as exc:
        print("WARN: goToPosture failed: {}".format(exc))
    set_security_distances(motion, TELEOP_ORTHO_SECURITY, TELEOP_TANGENT_SECURITY)
    return motion


def set_security_distances(motion, ortho, tangent):
    """Adjust Pepper's move safety bubble; smaller = brakes less = feels faster."""
    try:
        motion.setOrthogonalSecurityDistance(ortho)
        motion.setTangentialSecurityDistance(tangent)
    except Exception as exc:
        print("WARN: couldn't set security distances: {}".format(exc))


class Camera(object):
    """Thin wrapper over ALVideoDevice remote frame grabbing."""

    def __init__(self, session, cam_id, resolution, fps):
        self.video = session.service("ALVideoDevice")
        self.resolution = resolution
        self.fps = fps
        self.cam_id = cam_id
        self.handle = None
        self._subscribe()

    def _subscribe(self):
        # unique-ish name; unsubscribe stale handles from crashed runs
        name = "teleop_{}".format(self.cam_id)
        try:
            self.video.unsubscribe(name)
        except Exception:
            pass
        self.handle = self.video.subscribeCamera(
            name, self.cam_id, self.resolution, COLOR_RGB, self.fps
        )

    def set_camera(self, cam_id):
        """Switch to camera `cam_id`, reverting if that camera can't subscribe."""
        if cam_id == self.cam_id:
            return
        previous = self.cam_id
        self.close()
        self.cam_id = cam_id
        try:
            self._subscribe()
        except Exception as exc:
            print("WARN: camera {} unavailable ({}), staying on {}".format(cam_id, exc, previous))
            self.cam_id = previous
            self._subscribe()

    def switch(self):
        self.set_camera(CAM_BOTTOM if self.cam_id == CAM_TOP else CAM_TOP)

    def frame(self):
        """Return (width, height, rgb_bytes) or None if no frame is ready."""
        img = self.video.getImageRemote(self.handle)
        if img is None:
            return None
        width, height, data = img[0], img[1], img[6]
        self.video.releaseImage(self.handle)
        return width, height, bytes(data)

    def close(self):
        if self.handle is not None:
            try:
                self.video.unsubscribe(self.handle)
            except Exception:
                pass
            self.handle = None


class FrameGrabber(threading.Thread):
    """Grabs camera frames in the background so the slow remote getImageRemote()
    never stalls the control/render loop. The main loop just reads the latest frame.
    All ALVideoDevice access (grab + camera switch) happens on this one thread."""

    def __init__(self, camera):
        super(FrameGrabber, self).__init__(daemon=True)
        self.camera = camera
        self._lock = threading.Lock()
        self._latest = None
        self._pending_cam = None
        self._running = True

    def run(self):
        while self._running:
            if self._pending_cam is not None:
                self.camera.set_camera(self._pending_cam)
                self._pending_cam = None
            try:
                frame = self.camera.frame()
            except Exception as exc:
                frame = None
                print("WARN: camera.frame() failed: {}".format(exc))
            if frame is not None:
                with self._lock:
                    self._latest = frame
            else:
                time.sleep(0.005)  # avoid a busy spin when no frame is ready

    def latest(self):
        with self._lock:
            return self._latest

    def request_camera(self, cam_id):
        self._pending_cam = cam_id

    def stop(self):
        self._running = False
        self.join(timeout=1.0)


def compute_velocity(keys, speed):
    """Map the currently-pressed keys to (x, y, theta) in [-1, 1]."""
    x = y = theta = 0.0
    if keys[pygame.K_w]:
        x += 1.0
    if keys[pygame.K_s]:
        x -= 1.0
    if keys[pygame.K_a]:
        y += 1.0
    if keys[pygame.K_d]:
        y -= 1.0
    if keys[pygame.K_q]:
        theta += 1.0
    if keys[pygame.K_e]:
        theta -= 1.0
    return x * speed, y * speed, theta * speed


def compute_head(keys):
    """While CTRL is held, map keys to head (yaw, pitch) directions in {-1, 0, 1}.

    Yaw: A/Q = left (+), D/E = right (-).  Pitch: W = up (-), S = down (+).
    """
    dyaw = dpitch = 0.0
    if keys[pygame.K_a] or keys[pygame.K_q]:
        dyaw += 1.0
    if keys[pygame.K_d] or keys[pygame.K_e]:
        dyaw -= 1.0
    if keys[pygame.K_w]:
        dpitch -= 1.0   # NAOqi HeadPitch: negative = look up
    if keys[pygame.K_s]:
        dpitch += 1.0
    return dyaw, dpitch


def compute_hip(keys):
    """While SHIFT is held, map keys to hip/knee directions in {-1, 0, 1}.

    Roll: A = lean left (+), D = lean right (-).  Pitch: W = lean forward (+),
    S = lean back (-).  Knee: Q = crouch (-), E = rise (+).
    """
    droll = dpitch = dknee = 0.0
    if keys[pygame.K_a]:
        droll += 1.0
    if keys[pygame.K_d]:
        droll -= 1.0
    if keys[pygame.K_w]:
        dpitch += 1.0
    if keys[pygame.K_s]:
        dpitch -= 1.0
    if keys[pygame.K_q]:
        dknee -= 1.0
    if keys[pygame.K_e]:
        dknee += 1.0
    return droll, dpitch, dknee


def clamp_joint(name, value):
    lo, hi = JOINT_LIMITS[name]
    return max(lo, min(hi, value))


def update_targets(targets, keys, head_mode, hip_mode, dt):
    """Nudge managed-joint targets from held keys, scaled by elapsed time `dt` (s)."""
    head_step = HEAD_RATE * dt
    arm_step = ARM_RATE * dt
    hip_step = HIP_RATE * dt
    if head_mode:
        dyaw, dpitch = compute_head(keys)
        targets["HeadYaw"] = clamp_joint("HeadYaw", targets["HeadYaw"] + dyaw * head_step)
        targets["HeadPitch"] = clamp_joint("HeadPitch", targets["HeadPitch"] + dpitch * head_step)
    elif hip_mode:
        droll, dpitch, dknee = compute_hip(keys)
        targets["HipRoll"] = clamp_joint("HipRoll", targets["HipRoll"] + droll * hip_step)
        targets["HipPitch"] = clamp_joint("HipPitch", targets["HipPitch"] + dpitch * hip_step)
        targets["KneePitch"] = clamp_joint("KneePitch", targets["KneePitch"] + dknee * hip_step)
    # Arms move any time (not while driving-key-blocked): H/J = left up/down, L/K = right up/down.
    if keys[pygame.K_h]:
        targets["LShoulderPitch"] = clamp_joint("LShoulderPitch", targets["LShoulderPitch"] - arm_step)
    if keys[pygame.K_j]:
        targets["LShoulderPitch"] = clamp_joint("LShoulderPitch", targets["LShoulderPitch"] + arm_step)
    if keys[pygame.K_l]:
        targets["RShoulderPitch"] = clamp_joint("RShoulderPitch", targets["RShoulderPitch"] - arm_step)
    if keys[pygame.K_k]:
        targets["RShoulderPitch"] = clamp_joint("RShoulderPitch", targets["RShoulderPitch"] + arm_step)


def assert_targets(motion, targets, joints):
    """Command the given joints to their target every frame (beats recentering).

    `joints` is a subset of MANAGED_JOINTS — head/arm assertion is skipped when a
    feature (follow-me, an emote) is temporarily driving those joints itself.
    """
    if not joints:
        return
    try:
        motion.setAngles(joints, [targets[j] for j in joints], JOINT_SPEED)
    except Exception as exc:
        print("WARN: setAngles failed: {}".format(exc))


def build_tablet_html(text):
    """A self-contained page showing big centered text on the robot's tablet."""
    safe = html_lib.escape(text)
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>html,body{margin:0;height:100%;background:#0b1020;}"
        "body{display:-webkit-box;-webkit-box-pack:center;-webkit-box-align:center;"
        "display:flex;justify-content:center;align-items:center;}"
        "p{color:#eaf2ff;font-family:sans-serif;font-size:64px;font-weight:bold;"
        "text-align:center;padding:0 40px;word-wrap:break-word;}</style></head>"
        "<body><p>" + safe + "</p></body></html>"
    )


def show_on_tablet(tablet, text):
    """Render `text` on Pepper's tablet via a self-contained data: URL."""
    if tablet is None:
        return
    uri = "data:text/html;charset=utf-8," + quote(build_tablet_html(text))
    try:
        tablet.showWebview(uri)
        print("tablet <- {!r}".format(text))
    except Exception as exc:
        print("WARN: tablet show failed: {}".format(exc))


def speak(tts, text):
    """Say `text` out loud without blocking the control loop (runs in a thread)."""
    if tts is None:
        return

    def _say():
        try:
            tts.say(text)
        except Exception as exc:
            print("WARN: TTS say failed: {}".format(exc))

    threading.Thread(target=_say, daemon=True).start()


def draw_hud(surface, font, lines):
    y = 6
    for text in lines:
        # black outline for readability over any camera background
        shadow = font.render(text, True, (0, 0, 0))
        label = font.render(text, True, HUD_COLOR)
        surface.blit(shadow, (9, y + 1))
        surface.blit(label, (8, y))
        y += font.get_height() + 2


# --- Menu (Ctrl+M) -------------------------------------------------------------
# A menu is a dict: {"title", "items", "index", "parent"}. Each item is
# {"label": callable->str, "action": callable->effects_dict}. Effects keys:
#   "close" -> close the menu, "back" -> go to parent, "submenu" -> open that menu,
#   "suspend" -> seconds to stop asserting head+arm joints (for emotes).

def _do(fn, **effects):
    """Run a side-effecting action, then return the menu effects dict."""
    fn()
    return effects


def make_led_menu(leds, parent):
    return {
        "title": "LEDs",
        "index": 0,
        "parent": parent,
        "items": [
            {"label": lambda: "Eyes: {}".format(leds.eye_color),
             "action": lambda: _do(leds.cycle_eyes)},
            {"label": lambda: "Ears: {}".format("on" if leds.ears_on else "off"),
             "action": lambda: _do(leds.toggle_ears)},
            {"label": lambda: "Chest: {}".format(leds.chest_color),
             "action": lambda: _do(leds.cycle_chest)},
            {"label": lambda: "All LEDs off",
             "action": lambda: _do(leds.all_off)},
            {"label": lambda: "< Back", "action": lambda: {"back": True}},
        ],
    }


def make_nav_menu(nav, parent):
    """Mapping + remembered-spots submenu. Rebuilt each time it's opened, so the
    spot list and status reflect the current Navigator state."""

    def go_menu():
        names = nav.spot_names()
        items = [{"label": (lambda n=n: "Go to {}".format(n)),
                  "action": (lambda n=n: _do(lambda: nav.go_to_spot(n), close=True))}
                 for n in names]
        if not items:
            items.append({"label": lambda: "(no spots yet)", "action": lambda: {}})
        items.append({"label": lambda: "< Back", "action": lambda: {"back": True}})
        return {"title": "Go to spot", "index": 0, "parent": menu, "items": items}

    def maps_menu():
        names = nav.map_names()
        items = []
        for n in names:
            spots = len(nav.maps.get(n, {}).get("spots", {}))
            marker = "* " if n == nav.current else "  "
            items.append({
                "label": (lambda n=n, s=spots, m=marker: "{}{} ({} spots)".format(m, n, s)),
                "action": (lambda n=n: _do(lambda: nav.switch_map(n), close=True))})
        if not items:
            items.append({"label": lambda: "(no maps yet)", "action": lambda: {}})
        items.append({"label": lambda: "+ New map (explore next)",
                      "action": lambda: _do(nav.new_map)})
        items.append({"label": lambda: "< Back", "action": lambda: {"back": True}})
        return {"title": "Maps (switch room)", "index": 0, "parent": menu, "items": items}

    menu = {
        "title": "Navigation",
        "index": 0,
        "parent": parent,
        "items": [
            {"label": lambda: "Map: {} ({} maps)  [{}]".format(
                nav.current_map, len(nav.maps), nav.status),
             "action": lambda: {"submenu": maps_menu()}},
            {"label": lambda: "Explore & map '{}' ({:.0f}m)".format(
                nav.current_map, nav.explore_radius),
             "action": lambda: _do(nav.explore, close=True)},
            {"label": lambda: "Map radius: {:.0f}m (press to cycle)".format(nav.explore_radius),
             "action": lambda: _do(nav.cycle_radius)},
            {"label": lambda: "Remember this spot ({} saved)".format(len(nav.spots)),
             "action": lambda: _do(nav.remember_spot, close=True)},
            {"label": lambda: "Go to spot...",
             "action": lambda: {"submenu": go_menu()}},
            {"label": lambda: "Load current map & localize",
             "action": lambda: _do(nav.load_map, close=True)},
            {"label": lambda: "Stop navigation",
             "action": lambda: _do(nav.stop)},
            {"label": lambda: "Forget spots in this map",
             "action": lambda: _do(nav.forget_spots)},
            {"label": lambda: "< Back", "action": lambda: {"back": True}},
        ],
    }
    return menu


def make_main_menu(session, follow, leds, tts, dialogue, voice, tablet, gif_url, nav=None,
                   clown_audio=None, touch=None):
    menu = {"title": "Menu", "index": 0, "parent": None, "items": []}
    items = []
    if follow is not None:
        items.append({
            "label": lambda: "Follow me: {}".format("ON" if follow.active else "OFF"),
            "action": lambda: _do(follow.toggle)})
    if voice is not None:
        items.append({
            "label": lambda: "Voice commands: {}".format("ON" if voice.active else "OFF"),
            "action": lambda: _do(lambda: toggle_exclusive(voice, dialogue))})
    if dialogue is not None:
        items.append({
            "label": lambda: "Dialogue (talk to me): {}".format("ON" if dialogue.active else "OFF"),
            "action": lambda: _do(lambda: toggle_exclusive(dialogue, voice))})
    if touch is not None:
        items.append({
            "label": lambda: "Don't-touch-me: {}".format("ON" if touch.active else "OFF"),
            "action": lambda: _do(touch.toggle)})
    items.append({
        "label": lambda: "Say face characteristics",
        "action": lambda: _do(lambda: feat.say_face_characteristics(session, tts), close=True)})
    items.append({
        "label": lambda: "Wave hi",
        "action": lambda: _do(lambda: feat.do_hi_emote(session), close=True, suspend=4.0)})
    items.append({
        "label": lambda: "67 emote",
        "action": lambda: _do(lambda: feat.do_67_emote(session, tts, tablet=tablet, gif_url=gif_url),
                              close=True, suspend=feat.emote_67_duration())})
    items.append({
        "label": lambda: "Clown dance",
        "action": lambda: _do(lambda: feat.do_clown_dance(session, tts, audio_path=clown_audio),
                              close=True, suspend=feat.clown_dance_duration())})
    if nav is not None:
        items.append({
            "label": lambda: "Navigation (map & spots)...",
            "action": lambda: {"submenu": make_nav_menu(nav, menu)}})
    if leds is not None:
        items.append({
            "label": lambda: "LEDs...",
            "action": lambda: {"submenu": make_led_menu(leds, menu)}})
    items.append({"label": lambda: "Close", "action": lambda: {"close": True}})
    menu["items"] = items
    return menu


def apply_effects(effects, menu, suspend_until):
    """Return (new_menu, new_suspend_until) after applying an item's effects."""
    if effects.get("suspend"):
        suspend_until = pygame.time.get_ticks() + int(effects["suspend"] * 1000)
    if effects.get("submenu"):
        return effects["submenu"], suspend_until
    if effects.get("back"):
        return menu["parent"], suspend_until
    if effects.get("close"):
        return None, suspend_until
    return menu, suspend_until


def render_menu(screen, font, menu):
    items = menu["items"]
    sw, sh = screen.get_size()
    line_h = font.get_height() + 10
    panel_w = min(420, sw - 20)
    panel_h = line_h * (len(items) + 2) + 10
    px, py = (sw - panel_w) // 2, (sh - panel_h) // 2
    overlay = pygame.Surface((panel_w, panel_h))
    overlay.set_alpha(230)
    overlay.fill((10, 16, 32))
    screen.blit(overlay, (px, py))
    title = font.render("== {} ==".format(menu["title"]), True, (255, 255, 255))
    screen.blit(title, (px + 16, py + 10))
    for i, item in enumerate(items):
        label = item["label"]() if callable(item["label"]) else item["label"]
        row_y = py + 10 + line_h * (i + 1)
        selected = (i == menu["index"])
        if selected:
            pygame.draw.rect(screen, (0, 220, 120), (px + 8, row_y - 2, panel_w - 16, line_h))
        color = (0, 0, 0) if selected else (220, 230, 245)
        text = font.render("{} {}. {}".format(">" if selected else " ", i + 1, label), True, color)
        screen.blit(text, (px + 16, row_y))
    hint = font.render("Up/Down move  Enter select  Esc back  Ctrl+M close",
                       True, (150, 160, 180))
    screen.blit(hint, (px + 16, py + 10 + line_h * (len(items) + 1)))


def _safe(fn):
    """Call fn(), swallowing/logging any exception (for fire-and-forget threads)."""
    try:
        fn()
    except Exception as exc:
        print("WARN: {}".format(exc))


def toggle_exclusive(primary, other):
    """Toggle `primary`; if turning it on, turn `other` off first (voice and dialogue
    share the recognizer, so only one may run)."""
    if primary.active:
        primary.stop()
    else:
        if other is not None and other.active:
            other.stop()
        primary.start()


# --- Voice commands (ALSpeechRecognition) --------------------------------------
# Maps spoken phrases to actions. Base moves write a timed velocity into `state`
# (the main loop drives it); head/arm moves write into the shared `targets` dict
# (the main loop asserts it); features call their services directly.

def build_voice_commands(motion, posture, session, tts, follow, leds, dialogue, targets, state,
                         tablet, gif_url, nav=None, clown_audio=None, touch=None):
    def timed_move(vx, vy, vth, ms):
        state["vx"], state["vy"], state["vth"] = vx, vy, vth
        state["move_until"] = pygame.time.get_ticks() + ms

    def stop_base():
        state["move_until"] = 0
        state["vx"] = state["vy"] = state["vth"] = 0.0
        try:
            motion.stopMove()
        except Exception:
            pass

    def set_head(yaw, pitch):
        targets["HeadYaw"] = clamp_joint("HeadYaw", yaw)
        targets["HeadPitch"] = clamp_joint("HeadPitch", pitch)

    def set_arm(joint, value):
        targets[joint] = clamp_joint(joint, value)

    def set_hands(value):
        # Pepper hands are single-DOF: 1.0 = open, 0.0 = closed (grip).
        try:
            motion.setAngles(["LHand", "RHand"], [value, value], 0.5)
        except Exception as exc:
            print("WARN: hand move failed: {}".format(exc))

    def go_posture(name, hold_ms):
        state["suspend_until"] = max(state["suspend_until"], pygame.time.get_ticks() + hold_ms)
        if posture is not None:
            threading.Thread(
                target=lambda: _safe(lambda: posture.goToPosture(name, 0.6)), daemon=True).start()

    def hi():
        state["suspend_until"] = max(state["suspend_until"], pygame.time.get_ticks() + 4000)
        feat.do_hi_emote(session)

    def sixty_seven():
        state["suspend_until"] = max(
            state["suspend_until"],
            pygame.time.get_ticks() + int(feat.emote_67_duration() * 1000))
        feat.do_67_emote(session, tts, tablet=tablet, gif_url=gif_url)

    def clown_dance():
        state["suspend_until"] = max(
            state["suspend_until"],
            pygame.time.get_ticks() + int(feat.clown_dance_duration() * 1000))
        feat.do_clown_dance(session, tts, audio_path=clown_audio)

    cmds = {}

    def add(phrases, fn):
        for phrase in phrases:
            cmds[phrase] = fn

    mv, tv = VOICE_MOVE_SPEED, VOICE_TURN_SPEED
    add(("move forward", "go forward", "forward", "walk forward"),
        lambda: timed_move(mv, 0.0, 0.0, VOICE_MOVE_MS))
    add(("move backward", "move backwards", "go back", "backward", "backwards"),
        lambda: timed_move(-mv, 0.0, 0.0, VOICE_MOVE_MS))
    add(("move left", "strafe left", "step left"),
        lambda: timed_move(0.0, mv, 0.0, VOICE_MOVE_MS))
    add(("move right", "strafe right", "step right"),
        lambda: timed_move(0.0, -mv, 0.0, VOICE_MOVE_MS))
    add(("turn left", "rotate left"), lambda: timed_move(0.0, 0.0, tv, VOICE_TURN_MS))
    add(("turn right", "rotate right"), lambda: timed_move(0.0, 0.0, -tv, VOICE_TURN_MS))
    add(("stop", "stop moving", "halt", "freeze"), stop_base)

    add(("look left",), lambda: set_head(0.6, targets["HeadPitch"]))
    add(("look right",), lambda: set_head(-0.6, targets["HeadPitch"]))
    add(("look up",), lambda: set_head(targets["HeadYaw"], -0.4))
    add(("look down",), lambda: set_head(targets["HeadYaw"], 0.3))
    add(("look forward", "look center", "look straight"), lambda: set_head(0.0, 0.0))

    add(("raise left arm",), lambda: set_arm("LShoulderPitch", -1.2))
    add(("lower left arm",), lambda: set_arm("LShoulderPitch", 1.4))
    add(("raise right arm",), lambda: set_arm("RShoulderPitch", -1.2))
    add(("lower right arm",), lambda: set_arm("RShoulderPitch", 1.4))
    add(("arms down", "put your arms down"),
        lambda: (set_arm("LShoulderPitch", 1.5), set_arm("RShoulderPitch", 1.5)))

    add(("open hands", "open your hands", "let go", "release"), lambda: set_hands(1.0))
    add(("close hands", "close your hands", "grab", "grab it"), lambda: set_hands(0.0))

    add(("stand up", "stand"), lambda: go_posture("StandInit", 3000))
    add(("sit down", "sit"), lambda: go_posture("Sit", 3000))

    if follow is not None:
        add(("follow me",), lambda: (follow.start() if not follow.active else None))
        add(("stop following", "stop follow"), lambda: (follow.stop() if follow.active else None))

    if nav is not None:
        add(("explore", "explore the area", "map the area", "map this room"), nav.explore)
        add(("new map", "start a new map", "map a new room"), lambda: nav.new_map())
        add(("next map", "switch map", "change map", "other room"), nav.next_map)
        add(("bigger map", "change map size", "change map radius"), nav.cycle_radius)
        add(("remember this spot", "remember spot", "save this spot"),
            lambda: nav.remember_spot())
        add(("stop navigation", "stop exploring", "stop mapping"), nav.stop)
        add(("load map", "load the map"), nav.load_map)
        add(("forget spots", "forget all spots", "forget the spots"), nav.forget_spots)
        # Return to a numbered spot by voice (menu auto-names them "spot 1".."spot 5").
        _spot_words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
        for _word, _num in _spot_words.items():
            add(("go to spot {}".format(_word),),
                lambda n=_num: nav.go_to_spot("spot {}".format(n)))

    add(("read my face", "who am i", "how do i look", "guess my age"),
        lambda: feat.say_face_characteristics(session, tts))
    add(("wave", "say hi", "say hello", "wave hello"), hi)
    add(("six seven", "sixty seven", "do the six seven", "sixty seven emote"), sixty_seven)
    add(("clown dance", "dance", "do a dance", "circus dance", "dance for me"), clown_dance)

    if leds is not None:
        add(("red eyes",), lambda: leds.set("FaceLeds", 1, 0, 0))
        add(("green eyes",), lambda: leds.set("FaceLeds", 0, 1, 0))
        add(("blue eyes",), lambda: leds.set("FaceLeds", 0, 0, 1))
        add(("white eyes", "normal eyes"), lambda: leds.set("FaceLeds", 1, 1, 1))
        add(("red chest",), lambda: leds.set("ChestLeds", 1, 0, 0))
        add(("blue chest",), lambda: leds.set("ChestLeds", 0, 0, 1))
        add(("turn off lights", "lights off", "lights out"), leds.all_off)
        # Mirror the LED submenu's cycle/toggle actions.
        add(("cycle eyes", "change eye color", "next eye color"), leds.cycle_eyes)
        add(("cycle chest", "change chest color", "next chest color"), leds.cycle_chest)
        add(("toggle ears", "ears on", "ears off"), leds.toggle_ears)

    if dialogue is not None:
        # Starting dialogue must stop voice first (shared recognizer); run it in a
        # thread so the voice poll thread doesn't join itself while dispatching.
        def start_chat():
            if not dialogue.active:
                threading.Thread(
                    target=lambda: toggle_exclusive(dialogue, state.get("voice")),
                    daemon=True).start()
        add(("start dialogue", "let's chat", "let us chat", "chat with me"), start_chat)
        add(("stop dialogue", "stop chatting", "stop talking"),
            lambda: dialogue.stop() if dialogue.active else None)

    if touch is not None:
        add(("stop reacting to touch", "you can touch me", "touch is okay"),
            lambda: touch.stop() if touch.active else None)
        add(("don't let me touch you", "react to touch", "no touching"),
            lambda: touch.start() if not touch.active else None)

    # Turn voice recognition off by voice (threaded: stop() joins the poll thread,
    # which is the very thread dispatching this command).
    def voice_off():
        v = state.get("voice")
        if v is not None and v.active:
            threading.Thread(target=v.stop, daemon=True).start()
    add(("stop listening", "turn off voice", "stop voice commands", "voice off"), voice_off)

    add(("introduce yourself",),
        lambda: speak(tts, "Hello, I am Pepper, a humanoid robot. I can move, "
                           "follow you, recognize faces, and change my lights."))
    add(("what can you do",),
        lambda: speak(tts, "I can move around, move my head and arms, follow you, "
                           "read faces, wave, and change my lights."))
    return cmds


def main(ip, port, cam_id, resolution, fps):
    session = connect(ip, port)
    motion = prepare_robot(session)
    camera = Camera(session, cam_id, resolution, fps)
    grabber = FrameGrabber(camera)
    grabber.start()
    try:
        tablet = session.service("ALTabletService")
    except Exception as exc:
        tablet = None
        print("WARN: ALTabletService unavailable, tablet text disabled: {}".format(exc))
    try:
        tts = session.service("ALTextToSpeech")
    except Exception as exc:
        tts = None
        print("WARN: ALTextToSpeech unavailable, speech disabled: {}".format(exc))
    try:
        follow = feat.FollowMe(session)
    except Exception as exc:
        follow = None
        print("WARN: ALTracker unavailable, follow-me disabled: {}".format(exc))
    try:
        leds = feat.Leds(session)
    except Exception as exc:
        leds = None
        print("WARN: ALLeds unavailable, LED control disabled: {}".format(exc))
    try:
        dialogue = feat.Dialogue(session)
    except Exception as exc:
        dialogue = None
        print("WARN: ALDialog unavailable, dialogue disabled: {}".format(exc))
    try:
        nav = nav_feat.Navigator(session)
    except Exception as exc:
        nav = None
        print("WARN: ALNavigation unavailable, mapping disabled: {}".format(exc))
    try:
        touch = feat.TouchMonitor(session)
        touch.start()   # react to touch by default; toggle from the menu
    except Exception as exc:
        touch = None
        print("WARN: ALMemory touch unavailable, touch reaction disabled: {}".format(exc))
    # 67 GIF: upload it onto the robot so its tablet serves it locally/offline
    # (same-origin, the reliable path). Fall back to laptop-hosting if that fails.
    assets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    has_gif = os.path.exists(os.path.join(assets_dir, "67.gif"))
    gif_server = feat.GifServer(assets_dir, ip)
    gif_url = None
    if has_gif:
        if feat.deploy_gif_to_robot(ip, assets_dir):
            gif_url = feat.robot_gif_url(ip)          # robot-hosted (offline, same-origin)
        else:
            gif_server.start()
            gif_url = gif_server.url("67.html")        # laptop-hosted fallback

    # Clown dance: upload the music onto the robot so ALAudioPlayer plays it offline.
    # None if the file is missing or the upload fails (the dance then runs silently).
    clown_audio = feat.deploy_audio_to_robot(ip, assets_dir)

    # Seed managed-joint targets from the robot's current pose so nothing jumps.
    try:
        current = motion.getAngles(MANAGED_JOINTS, True)
        targets = dict(zip(MANAGED_JOINTS, current))
    except Exception as exc:
        print("WARN: getAngles failed, defaulting targets to 0: {}".format(exc))
        targets = {j: 0.0 for j in MANAGED_JOINTS}

    try:
        posture = session.service("ALRobotPosture")
    except Exception:
        posture = None
    # Shared state written by voice commands and read by the main loop: a timed base
    # velocity (move_until) and a joint-assertion suspend window (for posture/emotes).
    state = {"vx": 0.0, "vy": 0.0, "vth": 0.0, "move_until": 0, "suspend_until": 0}
    voice_commands = build_voice_commands(
        motion, posture, session, tts, follow, leds, dialogue, targets, state, tablet, gif_url, nav,
        clown_audio, touch)
    try:
        voice = feat.VoiceRecognizer(session, voice_commands)
    except Exception as exc:
        voice = None
        print("WARN: ALSpeechRecognition unavailable, voice commands disabled: {}".format(exc))
    # Expose the recognizer to voice commands that toggle it / dialogue (built before
    # it existed) via the shared state dict.
    state["voice"] = voice

    pygame.init()
    pygame.display.set_caption("Pepper teleop")
    base_w, base_h = RES_DIMS.get(resolution, (320, 240))
    windowed_size = (base_w * WINDOW_SCALE, base_h * WINDOW_SCALE)
    fullscreen = False
    # create the window ONCE; every frame is scaled to fit it (no per-frame recreate).
    # NOT resizable: calling set_mode() inside a VIDEORESIZE handler makes SDL emit
    # another resize event on Linux -> endless window open/close loop.
    screen = pygame.display.set_mode(windowed_size)
    last_surface = None  # keeps the window rendering the last good frame
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("monospace", 16, bold=True)

    speed = DEFAULT_SPEED
    last_vel = None
    last_send = 0
    text_input = None  # None = driving; a string = typing a tablet message
    menu = None        # None = closed; else the active menu dict
    suspend_until = 0  # ticks until which head+arm assertion is paused (emotes)
    prev_head_ours = True
    prev_arms_ours = True
    running = True
    print("Connected. Drive with WASD/QE, F fullscreen, SPACE stop, ESC quit.")
    print("Head: hold CTRL + WASD/QE, G recenters. Hip: hold SHIFT + WASD/QE.")
    print("Arms: H/J left up/down, L/K right up/down.")
    print("Hands: O open, P close (grab). Move an arm into place, then P to grip.")
    print("Cameras: 1=top, 2=bottom (3/4 if present), or C to cycle.")
    print("Press ENTER to type a tablet message. Press CTRL+M for the menu.")

    try:
        while running:
            # Cap at 60 FPS and get the real elapsed time; dt makes joint motion
            # frame-rate independent (rad/s * dt), clamped so a hitch can't jump joints.
            dt = min(clock.tick(60) / 1000.0, 0.05)
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    ctrl_held = pygame.key.get_mods() & pygame.KMOD_CTRL
                    if event.key == pygame.K_m and ctrl_held:
                        # CTRL+M toggles the menu (works whether open or closed).
                        menu = None if menu is not None else make_main_menu(
                            session, follow, leds, tts, dialogue, voice, tablet, gif_url, nav,
                            clown_audio, touch)
                    elif menu is not None:
                        items = menu["items"]
                        if event.key in (pygame.K_UP, pygame.K_w):
                            menu["index"] = (menu["index"] - 1) % len(items)
                        elif event.key in (pygame.K_DOWN, pygame.K_s):
                            menu["index"] = (menu["index"] + 1) % len(items)
                        elif event.key == pygame.K_ESCAPE:
                            menu = menu["parent"]
                        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                            effects = items[menu["index"]]["action"]() or {}
                            menu, suspend_until = apply_effects(effects, menu, suspend_until)
                        elif pygame.K_1 <= event.key <= pygame.K_9:
                            i = event.key - pygame.K_1
                            if i < len(items):
                                menu["index"] = i
                                effects = items[i]["action"]() or {}
                                menu, suspend_until = apply_effects(effects, menu, suspend_until)
                    elif text_input is not None:
                        # Typing a tablet message: capture text, ignore drive controls.
                        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                            message = text_input.strip()
                            text_input = None
                            if message:
                                show_on_tablet(tablet, message)
                                speak(tts, message)
                        elif event.key == pygame.K_ESCAPE:
                            text_input = None  # cancel typing
                        elif event.key == pygame.K_BACKSPACE:
                            text_input = text_input[:-1]
                        elif event.unicode and event.unicode.isprintable():
                            text_input += event.unicode
                    elif event.key == pygame.K_ESCAPE:
                        running = False
                    elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        text_input = ""  # enter typing mode
                    elif event.key == pygame.K_SPACE:
                        motion.stopMove()
                        last_vel = (0.0, 0.0, 0.0)
                    elif event.key == pygame.K_g:
                        targets["HeadYaw"] = 0.0   # recenter head (asserted next frame)
                        targets["HeadPitch"] = 0.0
                    elif event.key in (pygame.K_o, pygame.K_p):
                        value = 1.0 if event.key == pygame.K_o else 0.0  # O=open, P=close/grab
                        try:
                            motion.setAngles(["LHand", "RHand"], [value, value], 0.5)
                        except Exception as exc:
                            print("WARN: hand move failed: {}".format(exc))
                    elif event.key == pygame.K_c:
                        grabber.request_camera(CAM_BOTTOM if camera.cam_id == CAM_TOP else CAM_TOP)
                    elif pygame.K_1 <= event.key <= pygame.K_9:
                        grabber.request_camera(event.key - pygame.K_1)  # 1->cam 0, 2->cam 1, ...
                    elif event.key == pygame.K_f:
                        fullscreen = not fullscreen
                        if fullscreen:
                            screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
                        else:
                            screen = pygame.display.set_mode(windowed_size)
                    elif event.key == pygame.K_LEFTBRACKET:
                        speed = max(SPEED_MIN, round(speed - SPEED_STEP, 2))
                    elif event.key == pygame.K_RIGHTBRACKET:
                        speed = min(SPEED_MAX, round(speed + SPEED_STEP, 2))

            keys = pygame.key.get_pressed()
            typing = text_input is not None
            menu_open = menu is not None
            following = follow is not None and follow.active
            nav_busy = nav is not None and nav.busy
            # Keyboard direct control is blocked while typing, in a menu, following, or
            # while ALNavigation is driving the base (exploring / heading to a spot).
            controllable = not typing and not menu_open and not following and not nav_busy
            head_mode = controllable and (keys[pygame.K_LCTRL] or keys[pygame.K_RCTRL])
            # SHIFT (without CTRL) = hip mode: WASD leans the hip, Q/E crouch/rise.
            hip_mode = (controllable and not head_mode
                        and (keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]))
            driving = controllable and not head_mode and not hip_mode
            key_vel = compute_velocity(keys, speed) if driving else (0.0, 0.0, 0.0)
            if controllable:
                update_targets(targets, keys, head_mode, hip_mode, dt)

            now = pygame.time.get_ticks()
            # Base velocity priority: keyboard > timed voice move > stop. Pressing a key
            # cancels an in-progress voice move.
            voice_moving = now < state["move_until"]
            if key_vel != (0.0, 0.0, 0.0):
                state["move_until"] = 0
                base_vel = key_vel
            elif voice_moving:
                base_vel = (state["vx"], state["vy"], state["vth"])
            else:
                base_vel = key_vel  # (0,0,0) when nothing is driving

            # Decide which joints WE assert this frame. An emote/posture (suspend) owns
            # head+arms; follow-me owns the head. When control returns, re-seed targets
            # from the robot's real pose so nothing snaps.
            emote_busy = now < max(suspend_until, state["suspend_until"])
            head_ours = not emote_busy and not following
            arms_ours = not emote_busy
            if (head_ours and not prev_head_ours) or (arms_ours and not prev_arms_ours):
                try:
                    targets.update(zip(MANAGED_JOINTS, motion.getAngles(MANAGED_JOINTS, True)))
                except Exception:
                    pass
            prev_head_ours, prev_arms_ours = head_ours, arms_ours
            # Hips share the arm suspend logic (emotes like the clown dance drive them):
            # assert them only when arms are ours.
            assert_targets(motion, targets,
                           (HEAD_JOINTS if head_ours else []) + (ARM_JOINTS if arms_ours else [])
                           + (HIP_JOINTS if arms_ours else []))

            # Base: keep re-sending (moveToward is "sticky" but a dropped command or a
            # focus-loss frame would stall it). Skip while following or navigating —
            # ALTracker / ALNavigation own the base then.
            if not following and not nav_busy:
                if base_vel != last_vel or (now - last_send) >= KEEPALIVE_MS:
                    motion.moveToward(*base_vel)
                    last_vel = base_vel
                    last_send = now
            vel = base_vel  # for the HUD readout below

            # Read the newest frame from the background grabber (never blocks here).
            frame = grabber.latest()
            if frame is not None:
                w, h, data = frame
                try:
                    if len(data) == w * h * 3:
                        last_surface = pygame.image.frombuffer(data, (w, h), "RGB")
                    else:
                        print("WARN: frame buffer {} != {}x{}x3, skipping".format(len(data), w, h))
                except Exception as exc:
                    print("WARN: frame decode failed: {}".format(exc))

            # Always render: keep the window alive whether or not a frame arrived.
            screen.fill((0, 0, 0))
            if last_surface is not None:
                if last_surface.get_size() != screen.get_size():
                    shown = pygame.transform.scale(last_surface, screen.get_size())
                else:
                    shown = last_surface
                screen.blit(shown, (0, 0))
            else:
                waiting = font.render("waiting for camera...", True, HUD_COLOR)
                screen.blit(waiting, (12, screen.get_height() // 2))
            cam_label = {CAM_TOP: "top", CAM_BOTTOM: "bottom"}.get(camera.cam_id, "cam")
            if menu_open:
                mode = "MENU"
            elif nav_busy:
                mode = "NAV"
            elif following:
                mode = "FOLLOW"
            elif typing:
                mode = "TYPE"
            elif head_mode:
                mode = "HEAD"
            elif hip_mode:
                mode = "HIP"
            else:
                mode = "BASE"
            hud_lines = [
                "cam:{} ({})  speed:{:.1f}  fps:{:.0f}  mode:{}".format(
                    camera.cam_id, cam_label, speed, clock.get_fps(), mode),
                "x:{:+.2f} y:{:+.2f} th:{:+.2f}".format(*vel),
            ]
            if voice is not None and voice.active:
                hud_lines.append("voice: ON  last: {}".format(voice.last_word or "-"))
            if nav is not None and (nav.busy or nav.status not in ("idle", "")):
                hud_lines.append("nav: {}  spots: {}".format(nav.status, len(nav.spots)))
            if typing:
                hud_lines.append("tablet> {}_  (ENTER=send, ESC=cancel)".format(text_input))
            draw_hud(screen, font, hud_lines)
            if menu_open:
                render_menu(screen, font, menu)
            pygame.display.flip()
            # (framerate is capped by clock.tick(60) at the top of the loop)
    finally:
        try:
            if follow is not None and follow.active:
                follow.stop()
            if touch is not None and touch.active:
                touch.stop()
            if nav is not None:
                nav.stop()
            if dialogue is not None and dialogue.active:
                dialogue.stop()
            if voice is not None and voice.active:
                voice.stop()
            motion.stopMove()
            motion.moveToward(0.0, 0.0, 0.0)
            set_security_distances(motion, DEFAULT_ORTHO_SECURITY, DEFAULT_TANGENT_SECURITY)
        except Exception:
            pass
        grabber.stop()
        gif_server.stop()
        camera.close()
        pygame.quit()
        print("Stopped, camera released.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Keyboard teleop with live camera for Pepper.")
    parser.add_argument("--ip", required=True, help="Pepper's IP address")
    parser.add_argument("--port", type=int, default=9559)
    parser.add_argument("--camera", type=int, default=CAM_TOP,
                        help="0=top (default), 1=bottom")
    parser.add_argument("--vga", action="store_true",
                        help="640x480 feed (default 320x240, faster/smoother)")
    parser.add_argument("--fps", type=int, default=15, help="requested camera fps")
    args = parser.parse_args()
    resolution = RES_VGA if args.vga else RES_QVGA
    main(args.ip, args.port, args.camera, resolution, args.fps)
