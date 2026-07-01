"""
pepper_follow_me.py -- Pepper follows a person and talks.

The robot follows a detected person using ALTracker in "Move" mode (it walks
to hold a set distance from the target) and gives spoken feedback with
ALTextToSpeech, greeting people as they arrive and reacting when they leave.

Needs: a Python 3 venv with the `qi` package (`pip install qi`) and a Pepper
on the same network. Confirmed against NAOqi 2.5.10.7 (classic Pepper).

Usage:
    python pepper_follow_me.py --ip 192.168.1.2
    python pepper_follow_me.py --ip 192.168.1.2 --distance 0.5
    python pepper_follow_me.py --ip 192.168.1.2 --target Face --language English

Stop with Ctrl-C: Pepper stops moving, drops tracking, sits, and rests.
"""
from __future__ import print_function

import argparse
import sys
import threading
import time

import qi

DEFAULT_PORT = 9559

# --- Follow tuning (metres / radians) ------------------------------------
FOLLOW_DISTANCE_M = 0.6          # distance Pepper tries to keep from the person
DISTANCE_TOLERANCE_M = 0.1       # deadband so it doesn't jitter at the target
ANGLE_TOLERANCE_RAD = 0.1
FACE_WIDTH_M = 0.15              # assumed face width, only for the "Face" target
MAX_DETECTION_DISTANCE_M = 3.0   # ignore people further away than this
LOOP_PERIOD_S = 0.5              # how often we refresh who to follow


class FollowMe(object):
    """Drives Pepper to follow people and provide spoken feedback."""

    def __init__(self, session, target="People", distance=FOLLOW_DISTANCE_M,
                 language=None):
        self.target = target
        self.distance = distance
        self.motion = session.service("ALMotion")
        self.posture = session.service("ALRobotPosture")
        self.tracker = session.service("ALTracker")
        self.people = session.service("ALPeoplePerception")
        self.tts = session.service("ALTextToSpeech")
        self.memory = session.service("ALMemory")
        # Optional: pausing basic awareness stops Autonomous Life from fighting
        # the tracker for head/base control. Not fatal if it isn't available.
        self.awareness = self._optional(session, "ALBasicAwareness")

        self._tracking = False
        self._known_ids = set()
        self._present = set()    # who's currently visible (for greetings)

        if language:
            try:
                self.tts.setLanguage(language)
            except Exception as exc:
                print("(could not set language %r: %s)" % (language, exc))

    @staticmethod
    def _optional(session, name):
        try:
            return session.service(name)
        except Exception:
            return None

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self.tts.say("Starting follow me mode.")
        self.motion.wakeUp()
        self.posture.goToPosture("StandInit", 0.5)

        if self.awareness is not None:
            try:
                self.awareness.stopAwareness()
            except Exception:
                pass

        try:
            self.people.setFastModeEnabled(True)   # lower-latency detection
        except Exception:
            pass

        self.tracker.setMaximumDistanceDetection(MAX_DETECTION_DISTANCE_M)
        # Move mode + follow distance are (re)applied in _start_moving(), AFTER
        # the target is registered -- registering resets the tracker to
        # head-only, which looks like "Pepper stares but never walks".

        # Reset per-run state so restarts (and server reuse) begin clean.
        self._tracking = False
        self._known_ids = set()
        self._present = set()
        print("Follow-me active. target=%s distance=%.2fm -- Ctrl-C to stop."
              % (self.target, self.distance))

    def _greet_changes(self, ids):
        # Greet by diffing who's visible. Avoids ALMemory.subscriber(), which
        # raises "Invalid signature" on this NAOqi/qi build.
        current = set(ids)
        if current - self._present:
            self.tts.say("Hello. I will follow you.")
        elif self._present and not current:
            self.tts.say("Where did you go?")
        self._present = current

    # -- main loop ---------------------------------------------------------
    def run(self, stop_event):
        """Follow until stop_event is set. Blocks the calling thread.

        Safe to run from a worker thread (the web server does this).
        """
        while not stop_event.is_set():
            try:
                self._update_target()
            except Exception as exc:
                print("(update skipped: %s)" % exc)
            stop_event.wait(LOOP_PERIOD_S)

    def _update_target(self):
        if self.target == "Face":
            if not self._tracking:
                self.tracker.registerTarget("Face", FACE_WIDTH_M)
                self._start_moving("Face")
            return

        # "People": follow whoever ALPeoplePerception currently sees.
        try:
            ids = self.memory.getData("PeoplePerception/PeopleList") or []
        except Exception:
            ids = []                      # key doesn't exist until someone is seen
        ids = [int(i) for i in ids]

        self._greet_changes(ids)
        if not ids:
            return

        id_set = set(ids)
        if id_set != self._known_ids:
            self._known_ids = id_set
            print("seeing %d person(s): %s" % (len(ids), ids))
            self._register_people(ids)
        if not self._tracking:
            self._start_moving("People")

    def _start_moving(self, target):
        # Apply Move mode + follow distance AFTER registerTarget, then track.
        # registering a target resets the tracker to head-only, so setting Move
        # here is what actually makes Pepper drive its base toward you.
        self.tracker.setMode("Move")
        self.tracker.setRelativePosition(
            [-self.distance, 0.0, 0.0,
             DISTANCE_TOLERANCE_M, DISTANCE_TOLERANCE_M, ANGLE_TOLERANCE_RAD])
        self.tracker.track(target)
        self._tracking = True
        print("Move mode engaged -- step back and Pepper should follow.")

    def _register_people(self, ids):
        # Pass the ID list; some builds want a single ID, so fall back to that.
        try:
            self.tracker.registerTarget("People", ids)
        except Exception:
            self.tracker.registerTarget("People", ids[0])

    # -- shutdown ----------------------------------------------------------
    def stop(self):
        print("\nStopping follow-me...")
        try:
            self.tracker.stopTracker()
            self.tracker.unregisterAllTargets()
            self.tracker.setEffector("None")
        except Exception as exc:
            print("(tracker cleanup: %s)" % exc)
        try:
            self.tts.say("Follow me mode off. Goodbye.")
            self.posture.goToPosture("Sit", 0.5)
            self.motion.rest()
        except Exception as exc:
            print("(rest: %s)" % exc)


def connect(ip, port):
    session = qi.Session()
    url = "tcp://{}:{}".format(ip, port)
    try:
        session.connect(url)
    except RuntimeError:
        print("ERROR: can't reach NAOqi at {}.".format(url))
        print("  - same network? ping {}".format(ip))
        print("  - right IP? press the chest button; robot speaks it.")
        sys.exit(1)
    return session


def main():
    parser = argparse.ArgumentParser(description="Pepper follow-me + speech")
    parser.add_argument("--ip", required=True, help="Pepper IP (chest button says it)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="NAOqi port")
    parser.add_argument("--target", choices=["People", "Face"], default="People",
                        help="People = follow a whole person (default); "
                             "Face = follow a face (person must face the robot)")
    parser.add_argument("--distance", type=float, default=FOLLOW_DISTANCE_M,
                        help="metres to keep from the person (default 0.6)")
    parser.add_argument("--language", default=None,
                        help="ALTextToSpeech language, e.g. English, French")
    args = parser.parse_args()

    session = connect(args.ip, args.port)
    behavior = FollowMe(session, target=args.target, distance=args.distance,
                        language=args.language)
    behavior.start()
    stop_event = threading.Event()
    try:
        behavior.run(stop_event)
    except KeyboardInterrupt:
        pass
    finally:
        behavior.stop()


if __name__ == "__main__":
    main()
