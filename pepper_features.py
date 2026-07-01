"""Robot-side actions for the Pepper teleop menu, kept separate from the pygame UI
so each piece stays small: follow-me (ALTracker), face characteristics
(ALFaceCharacteristics + TTS), the 'hi' emote, and LED control (ALLeds).

All calls are wrapped defensively — a missing service or an unreadable memory key
degrades gracefully instead of crashing the teleop loop.
"""
from __future__ import print_function
import math
import os
import socket
import subprocess
import tempfile
import threading
import time

try:
    from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler  # Python 3
except ImportError:  # pragma: no cover
    ThreadingHTTPServer = None
    SimpleHTTPRequestHandler = None


def _run_async(fn):
    threading.Thread(target=fn, daemon=True).start()


def _safe_tablet(fn, *args):
    try:
        fn(*args)
    except Exception as exc:
        print("WARN: tablet call failed: {}".format(exc))


def local_ip(target_ip):
    """The laptop's LAN IP that can reach `target_ip` (so the robot tablet can load
    assets we host). Uses a UDP socket — no packets are actually sent."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target_ip, 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


ROBOT_APP = "followme"
ROBOT_HTML_DIR = ".local/share/PackageManager/apps/{}/html".format(ROBOT_APP)


def robot_gif_url(ip):
    """URL of the 67 page once hosted on the robot's own web server (same-origin,
    served offline just like the deployed tablet dashboards)."""
    return "http://{}/apps/{}/67.html".format(ip, ROBOT_APP)


def deploy_gif_to_robot(ip, assets_dir, user="nao", password="pepper"):
    """scp 67.gif + 67.html onto the robot so its tablet can load them locally,
    offline, over the same web path as the other dashboards. Non-interactive via
    SSH_ASKPASS (this qi build has no sshpass). Best-effort: returns True on success."""
    gif = os.path.join(assets_dir, "67.gif")
    html = os.path.join(assets_dir, "67.html")
    if not (os.path.exists(gif) and os.path.exists(html)):
        print("WARN: 67 assets missing in {}, skipping deploy".format(assets_dir))
        return False

    askpass = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    askpass.write("#!/bin/sh\necho '{}'\n".format(password))
    askpass.close()
    os.chmod(askpass.name, 0o700)
    env = dict(os.environ)
    env["SSH_ASKPASS"] = askpass.name
    env["SSH_ASKPASS_REQUIRE"] = "force"
    env.setdefault("DISPLAY", ":0")
    opts = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    target = "{}@{}".format(user, ip)

    def run(cmd, timeout):
        subprocess.run(["setsid", "-w"] + cmd, env=env, check=True, timeout=timeout,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        run(["ssh"] + opts + [target, "mkdir -p ~/{}".format(ROBOT_HTML_DIR)], 30)
        run(["scp"] + opts + [gif, html, "{}:{}/".format(target, ROBOT_HTML_DIR)], 60)
        print("67 GIF deployed to robot -> {}".format(robot_gif_url(ip)))
        return True
    except Exception as exc:
        print("WARN: 67 auto-deploy failed ({}). Run  ./deploy_67.sh {}  once.".format(exc, ip))
        return False
    finally:
        try:
            os.unlink(askpass.name)
        except Exception:
            pass


class GifServer(object):
    """Tiny HTTP server (on the laptop) that serves the assets/ dir so Pepper's
    tablet WebView can load the 67 GIF over the LAN — no SSH, no internet needed."""

    def __init__(self, directory, target_ip, port=8088):
        self.directory = directory
        self.port = port
        self.base_url = None
        self._httpd = None
        self._thread = None
        self._target_ip = target_ip

    def start(self):
        if ThreadingHTTPServer is None:
            print("WARN: no http.server; GIF hosting disabled")
            return None
        directory = self.directory

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                SimpleHTTPRequestHandler.__init__(self, *a, directory=directory, **kw)

            def log_message(self, *a):
                pass  # keep the console quiet

        try:
            self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
            self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
            self._thread.start()
            self.base_url = "http://{}:{}".format(local_ip(self._target_ip), self.port)
            print("GIF server: {} (serving {})".format(self.base_url, directory))
        except Exception as exc:
            print("WARN: GIF server failed to start: {}".format(exc))
            self.base_url = None
        return self.base_url

    def url(self, filename):
        return "{}/{}".format(self.base_url, filename) if self.base_url else None

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass


def _say(tts, text):
    if tts is None:
        print("(no TTS) {}".format(text))
        return
    try:
        tts.say(text)
    except Exception as exc:
        print("WARN: TTS failed: {}".format(exc))


def _mem(memory, key):
    try:
        return memory.getData(key)
    except Exception:
        return None


class FollowMe(object):
    """Follow a person with ALTracker in Move mode (ports the tested pepper_follow_me
    logic). A background poll loop reads PeoplePerception/PeopleList every LOOP_S and
    registers the *actual* person IDs (an empty target never drives). Correct order
    matters: registerTarget(ids) FIRST, then setMode('Move')/setRelativePosition/track
    — registering resets the tracker to head-only, so Move must be (re)applied after.
    While active, ALTracker owns the base AND head, so the teleop loop stops sending
    moveToward and stops asserting head angles.
    """

    FOLLOW_DISTANCE = 0.6
    DIST_TOL = 0.1
    ANG_TOL = 0.1
    MAX_DETECT = 3.0
    LOOP_S = 0.5

    def __init__(self, session):
        self.motion = session.service("ALMotion")
        self.tracker = session.service("ALTracker")
        self.memory = session.service("ALMemory")
        try:
            self.people = session.service("ALPeoplePerception")
        except Exception:
            self.people = None
        try:
            self.tts = session.service("ALTextToSpeech")
        except Exception:
            self.tts = None
        try:
            self.awareness = session.service("ALBasicAwareness")
        except Exception:
            self.awareness = None
        self.active = False
        self._stop = None
        self._thread = None
        self._tracking = False
        self._known = set()

    def start(self):
        try:
            self.motion.wakeUp()
            if self.awareness is not None:
                try:
                    self.awareness.stopAwareness()
                except Exception:
                    pass
            if self.people is not None:
                try:
                    self.people.subscribe("teleop_follow")
                except Exception:
                    pass
                try:
                    self.people.setFastModeEnabled(True)
                except Exception:
                    pass
            try:
                self.tracker.setMaximumDistanceDetection(self.MAX_DETECT)
            except Exception:
                pass
            self._tracking = False
            self._known = set()
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            self.active = True
            print("follow-me: ON")
            _say(self.tts, "Follow me mode on. Step back and I will follow you.")
        except Exception as exc:
            print("WARN: follow start failed: {}".format(exc))
            self.active = False
        return self.active

    def _run(self):
        while not self._stop.is_set():
            try:
                self._update()
            except Exception as exc:
                print("(follow update skipped: {})".format(exc))
            self._stop.wait(self.LOOP_S)

    def _update(self):
        try:
            ids = self.memory.getData("PeoplePerception/PeopleList") or []
        except Exception:
            ids = []  # key doesn't exist until someone is seen
        ids = [int(i) for i in ids]
        if not ids:
            return
        id_set = set(ids)
        if id_set != self._known:
            self._known = id_set
            print("follow-me: seeing {} person(s): {}".format(len(ids), ids))
            try:
                self.tracker.registerTarget("People", ids)
            except Exception:
                self.tracker.registerTarget("People", ids[0])
        if not self._tracking:
            self.tracker.setMode("Move")
            self.tracker.setRelativePosition(
                [-self.FOLLOW_DISTANCE, 0.0, 0.0, self.DIST_TOL, self.DIST_TOL, self.ANG_TOL])
            self.tracker.track("People")
            self._tracking = True
            print("follow-me: Move mode engaged — step back and Pepper follows")

    def stop(self):
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            self.tracker.stopTracker()
            self.tracker.unregisterAllTargets()
            self.tracker.setEffector("None")
        except Exception as exc:
            print("WARN: follow stop failed: {}".format(exc))
        if self.people is not None:
            try:
                self.people.unsubscribe("teleop_follow")
            except Exception:
                pass
        self._tracking = False
        self.active = False
        print("follow-me: OFF")
        _say(self.tts, "Follow me off.")
        return self.active

    def toggle(self):
        return self.stop() if self.active else self.start()


class VoiceRecognizer(object):
    """Offline voice commands via ALSpeechRecognition. Recognizes a fixed vocabulary
    of command phrases (word-spotting, so they can be said inside a sentence) and
    calls the matching handler from `commands` (a {phrase: callable} dict built by
    the caller). WordRecognized is POLLED from ALMemory — this qi build raises
    'Invalid signature' on ALMemory.subscriber, so we read + clear it instead.

    Note: ALSpeechRecognition and ALDialog share the recognizer, so run only one at
    a time (the menu toggles enforce this)."""

    CONF_THRESHOLD = 0.38
    POLL_S = 0.15

    def __init__(self, session, commands):
        self.asr = session.service("ALSpeechRecognition")
        self.memory = session.service("ALMemory")
        self.commands = commands
        self.active = False
        self.last_word = ""
        self._stop = None
        self._thread = None

    def _set_vocabulary(self, vocab):
        try:
            self.asr.setVocabulary(vocab, True)  # True = enable word spotting
        except Exception:
            # Some builds require pausing recognition before changing vocabulary.
            self.asr.pause(True)
            self.asr.setVocabulary(vocab, True)
            self.asr.pause(False)

    def start(self):
        try:
            try:
                self.asr.setLanguage("English")
            except Exception:
                pass
            self._set_vocabulary(sorted(self.commands.keys()))
            self.asr.subscribe("teleop_voice")
            try:
                self.memory.insertData("WordRecognized", ["", 0.0])
            except Exception:
                pass
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            self.active = True
            print("voice: ON — say a command")
        except Exception as exc:
            print("WARN: voice start failed: {}".format(exc))
            self.active = False
        return self.active

    def _run(self):
        while not self._stop.is_set():
            try:
                data = self.memory.getData("WordRecognized")
            except Exception:
                data = None
            if data and len(data) >= 2 and data[0]:
                word = str(data[0]).strip().lower()
                conf = float(data[1])
                if conf >= self.CONF_THRESHOLD:
                    # Consume the event so a stale value isn't re-fired next poll.
                    try:
                        self.memory.insertData("WordRecognized", ["", 0.0])
                    except Exception:
                        pass
                    self._dispatch(word, conf)
            self._stop.wait(self.POLL_S)

    def _dispatch(self, word, conf):
        fn = self.commands.get(word)
        if fn is None:
            for phrase, handler in self.commands.items():
                if phrase in word or word in phrase:
                    fn = handler
                    break
        if fn is None:
            print("voice: heard '{}' ({:.2f}) — no match".format(word, conf))
            return
        self.last_word = word
        print("voice> {} ({:.2f})".format(word, conf))
        try:
            fn()
        except Exception as exc:
            print("WARN: voice command '{}' failed: {}".format(word, exc))

    def stop(self):
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            self.asr.unsubscribe("teleop_voice")
        except Exception as exc:
            print("WARN: voice unsubscribe failed: {}".format(exc))
        self.active = False
        print("voice: OFF")
        return self.active

    def toggle(self):
        return self.stop() if self.active else self.start()


class Dialogue(object):
    """Offline chat via ALDialog: loads a small QiChat topic so you can talk to
    Pepper (it listens on the mic and replies). No internet / no LLM needed."""

    TOPIC = u"""topic: ~teleop_chat()
language: enu

concept:(greet) [hello hi hey "good morning" "good evening"]
u:(~greet) Hello there! Nice to meet you.
u:(how are you) I am doing great, thank you for asking!
u:(what is your name) My name is Pepper.
u:(my name is _*) Nice to meet you, %1 !
u:({can} you {please} follow me) Sure, open the menu and turn on follow me.
u:(what can you do) I can follow you, read your face, wave, and chat with you.
u:(tell me a joke) Why was the robot angry? Someone kept pushing its buttons!
u:(do you like humans) I like humans very much.
u:([bye goodbye "see you"]) Goodbye! Talk to you later.
"""

    def __init__(self, session):
        self.dialog = session.service("ALDialog")
        self.topic = None
        self.active = False

    def start(self):
        try:
            try:
                self.dialog.setLanguage("English")
            except Exception:
                pass
            self.topic = self.dialog.loadTopicContent(self.TOPIC)
            self.dialog.activateTopic(self.topic)
            self.dialog.subscribe("teleop_dialog")
            self.active = True
            print("dialogue: ON — talk to Pepper")
        except Exception as exc:
            print("WARN: dialogue start failed: {}".format(exc))
            self.active = False
        return self.active

    def stop(self):
        try:
            self.dialog.unsubscribe("teleop_dialog")
        except Exception:
            pass
        if self.topic:
            try:
                self.dialog.deactivateTopic(self.topic)
                self.dialog.unloadTopic(self.topic)
            except Exception as exc:
                print("WARN: dialogue unload failed: {}".format(exc))
            self.topic = None
        self.active = False
        print("dialogue: OFF")
        return self.active

    def toggle(self):
        return self.stop() if self.active else self.start()


EXPRESSIONS = ["neutral", "happy", "surprised", "angry", "sad"]


def say_face_characteristics(session, tts):
    """Analyze the face in front and speak rough age/gender/expression/smile."""

    def _work():
        try:
            memory = session.service("ALMemory")
            face = session.service("ALFaceCharacteristics")
        except Exception as exc:
            print("WARN: face services missing: {}".format(exc))
            _say(tts, "Sorry, face analysis is not available.")
            return
        try:
            people = session.service("ALPeoplePerception")
            people.subscribe("teleop_facechar")
        except Exception:
            pass

        ids = _mem(memory, "PeoplePerception/PeopleList")
        if not ids:
            _say(tts, "I do not see anyone in front of me.")
            return
        pid = ids[0]
        try:
            face.analyzeFaceCharacteristics(pid)
        except Exception as exc:
            print("WARN: analyzeFaceCharacteristics failed: {}".format(exc))

        parts = []
        age = _mem(memory, "PeoplePerception/Person/{}/AgeProperties".format(pid))
        if age:
            parts.append("about {} years old".format(int(round(age[0]))))
        gender = _mem(memory, "PeoplePerception/Person/{}/GenderProperties".format(pid))
        if gender:
            parts.append("and appear {}".format("male" if gender[0] >= 0.5 else "female"))
        expr = _mem(memory, "PeoplePerception/Person/{}/ExpressionProperties".format(pid))
        if expr:
            best = EXPRESSIONS[expr.index(max(expr))]
            parts.append("Your expression looks {}".format(best))
        smile = _mem(memory, "PeoplePerception/Person/{}/SmileProperties".format(pid))
        if smile:
            parts.append("and you are {}".format("smiling" if smile[0] > 0.5 else "not smiling"))

        if not parts:
            _say(tts, "I can see you, but I could not read your face. Please look at me.")
        else:
            _say(tts, "You are " + ", ".join(parts) + ".")

    _run_async(_work)


def do_hi_emote(session):
    """Play Pepper's standard 'Hey' wave animation. Returns approx duration (s)."""

    def _work():
        try:
            player = session.service("ALAnimationPlayer")
            player.run("animations/Stand/Gestures/Hey_1")
        except Exception as exc:
            print("WARN: hi emote failed: {}".format(exc))

    _run_async(_work)
    return 4.0  # caller suspends joint assertion for roughly this long


# 67 meme emote: hands out front (palms up, elbows bent), arms CONTINUOUSLY rocking
# up and down like a "weighing" see-saw while "six seven" is repeated back-to-back.
# Arms and speech run in parallel threads for the whole duration (not stepped).
EMOTE_67_DURATION_S = 6.0        # how long the arms rock and "six seven" repeats
EMOTE_67_RESET_S = 3.0           # extra suspend for the StandInit reset (outlasts it)
_67_lock = threading.Lock()      # ensures only one 67 emote runs at a time
EMOTE_67_FREQ_HZ = 1.3           # arm up/down cycles per second
EMOTE_67_TICK_S = 0.05           # arm update period (smooth = frequent small updates)

# Static half of the pose held for the whole emote (everything but ShoulderPitch).
_67_STATIC_NAMES = ["LShoulderRoll", "LElbowYaw", "LElbowRoll", "LWristYaw", "LHand",
                    "RShoulderRoll", "RElbowYaw", "RElbowRoll", "RWristYaw", "RHand"]
_67_STATIC_VALS = [0.15, -1.0, -1.0, -1.2, 1.0,
                   -0.15, 1.0, 1.0, 1.2, 1.0]
_67_MID, _67_AMP = 0.5, 0.4      # ShoulderPitch sweeps _67_MID +/- _67_AMP (smaller = up)


def emote_67_duration():
    """How long the caller should suspend joint assertion (incl. the reset)."""
    return EMOTE_67_DURATION_S + EMOTE_67_RESET_S


def do_67_emote(session, tts, duration=EMOTE_67_DURATION_S, tablet=None, gif_url=None):
    """Rock the arms up/down continuously AND repeat 'six seven' non-stop for
    `duration` seconds (two parallel threads), showing the 67 GIF on the tablet,
    then hide the GIF and reset to a clean stance.

    Hardened so the arms ALWAYS move and are never interrupted:
      - a lock ignores a re-trigger while one emote is already running;
      - wakeUp + full arm stiffness so the joints are actually driveable;
      - external collision protection is disabled for the arms (it otherwise blocks
        arm moves when a person/obstacle is in front — exactly the 67 situation)
        and restored afterwards;
      - the caller's suspend (emote_67_duration) outlasts the whole thing so the
        teleop assertion loop can't fight the motion mid-emote.
    """
    if not _67_lock.acquire(False):
        return emote_67_duration()  # one already running — ignore this trigger

    def _speak_loop(end_t):
        if tts is None:
            return
        while time.time() < end_t:
            _say(tts, "six seven")  # blocking ~0.7s, so it loops back-to-back

    def _work():
        motion = None
        try:
            try:
                motion = session.service("ALMotion")
            except Exception as exc:
                print("WARN: 67 emote — no ALMotion: {}".format(exc))
                return
            try:
                posture = session.service("ALRobotPosture")
            except Exception:
                posture = None

            # Tablet in its own thread — showWebview can block for seconds.
            if tablet is not None and gif_url:
                threading.Thread(
                    target=lambda: _safe_tablet(tablet.showWebview, gif_url), daemon=True).start()

            # Make the arms actually driveable and unblockable.
            try:
                motion.wakeUp()
            except Exception:
                pass
            try:
                motion.setExternalCollisionProtectionEnabled("Arms", False)
            except Exception as exc:
                print("WARN: 67 could not disable arm collision protection: {}".format(exc))
            try:
                motion.setStiffnesses("LArm", 1.0)
                motion.setStiffnesses("RArm", 1.0)
                motion.setAngles(_67_STATIC_NAMES, _67_STATIC_VALS, 0.3)
            except Exception as exc:
                print("WARN: 67 emote pose failed: {}".format(exc))

            end_t = time.time() + duration
            speaker = threading.Thread(target=_speak_loop, args=(end_t,), daemon=True)
            speaker.start()
            # Smooth continuous rocking: opposite sine waves on the two shoulders.
            start = time.time()
            while time.time() < end_t:
                phase = 2.0 * math.pi * EMOTE_67_FREQ_HZ * (time.time() - start)
                offset = _67_AMP * math.sin(phase)
                try:
                    motion.setAngles(["LShoulderPitch", "RShoulderPitch"],
                                     [_67_MID - offset, _67_MID + offset], 0.8)
                except Exception:
                    pass
                time.sleep(EMOTE_67_TICK_S)
            speaker.join(timeout=2.0)

            if tablet is not None and gif_url:
                threading.Thread(
                    target=lambda: _safe_tablet(tablet.hideWebview), daemon=True).start()
            # Reset to a clean stance so the arms don't stay in the 67 pose afterwards.
            if posture is not None:
                try:
                    posture.goToPosture("StandInit", 0.5)
                except Exception:
                    pass
        finally:
            if motion is not None:
                try:
                    motion.setExternalCollisionProtectionEnabled("Arms", True)
                except Exception:
                    pass
            _67_lock.release()

    _run_async(_work)
    return emote_67_duration()


class Leds(object):
    """Cycle Pepper's eye (FaceLeds) and chest (ChestLeds) RGB groups, and toggle
    the blue-only ear LEDs (EarLeds)."""

    COLORS = [("white", 1, 1, 1), ("red", 1, 0, 0), ("green", 0, 1, 0),
              ("blue", 0, 0, 1), ("yellow", 1, 1, 0), ("magenta", 1, 0, 1),
              ("off", 0, 0, 0)]

    def __init__(self, session):
        self.leds = session.service("ALLeds")
        self.eye_idx = 0
        self.chest_idx = 0
        self.ears_on = True

    def _cycle(self, group, idx):
        idx = (idx + 1) % len(self.COLORS)
        name, r, g, b = self.COLORS[idx]
        try:
            self.leds.fadeRGB(group, float(r), float(g), float(b), 0.2)
        except Exception as exc:
            print("WARN: LED {} failed: {}".format(group, exc))
        return idx, name

    def cycle_eyes(self):
        self.eye_idx, name = self._cycle("FaceLeds", self.eye_idx)
        return name

    def cycle_chest(self):
        self.chest_idx, name = self._cycle("ChestLeds", self.chest_idx)
        return name

    def toggle_ears(self):
        self.ears_on = not self.ears_on
        try:
            self.leds.setIntensity("EarLeds", 1.0 if self.ears_on else 0.0)
        except Exception as exc:
            print("WARN: ear LED failed: {}".format(exc))
        return self.ears_on

    def set(self, group, r, g, b):
        """Set an RGB LED group to an explicit color (used by voice commands)."""
        try:
            self.leds.fadeRGB(group, float(r), float(g), float(b), 0.2)
        except Exception as exc:
            print("WARN: LED set {} failed: {}".format(group, exc))

    def all_off(self):
        try:
            self.leds.off("AllLeds")
        except Exception as exc:
            print("WARN: LED off failed: {}".format(exc))
        self.ears_on = False

    @property
    def eye_color(self):
        return self.COLORS[self.eye_idx][0]

    @property
    def chest_color(self):
        return self.COLORS[self.chest_idx][0]
