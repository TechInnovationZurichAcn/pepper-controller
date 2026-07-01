"""
Pepper connection + diagnostic starter.

Run this FIRST. It validates your Python/pynaoqi setup, tells you the
robot's NAOqi version (classic 2.5 vs newer 2.9), and lists the services
you can script against.

Requires: Python 2.7 with pynaoqi on PYTHONPATH (the `qi` module).
Usage:
    python2 pepper_diag.py --ip <PEPPER_IP>
Find <PEPPER_IP> by pressing Pepper's chest button (it says its IP aloud).
"""

from __future__ import print_function
import argparse
import sys

try:
    import qi
except ImportError:
    print("ERROR: couldn't import `qi`. Your pynaoqi SDK isn't on PYTHONPATH.")
    print(
        "  export PYTHONPATH=/path/to/pynaoqi/lib/python2.7/site-packages:$PYTHONPATH"
    )
    print("  ...and run with python2, not python3.")
    sys.exit(1)


def main(ip, port):
    session = qi.Session()
    url = "tcp://{}:{}".format(ip, port)
    try:
        session.connect(url)
    except RuntimeError:
        print("ERROR: can't reach NAOqi at {}.".format(url))
        print("Checklist:")
        print("  - Same network? Try: ping {}".format(ip))
        print("  - Right IP? Press the chest button; robot speaks it.")
        print("  - Port 9559 open? School/corp Wi-Fi client-isolation blocks it")
        print("    -> put Pepper + laptop on your own router instead.")
        sys.exit(1)

    system = session.service("ALSystem")
    print("=" * 50)
    print("Connected to: {}".format(system.robotName()))
    print("NAOqi version: {}".format(system.systemVersion()))
    print("  -> starts with 2.5  = classic Pepper")
    print("  -> starts with 2.9  = newer Pepper")
    print("=" * 50)

    # Quick check that the pieces you need for 'follow me' are present.
    needed = [
        "ALMotion",
        "ALRobotPosture",
        "ALTracker",
        "ALPeoplePerception",
        "ALTextToSpeech",
    ]
    services = session.services()
    available = set(s["name"] for s in services)
    print("\nServices needed for follow-me + speech:")
    for name in needed:
        mark = "OK " if name in available else "MISSING"
        print("  [{}] {}".format(mark, name))

    print("\nTotal services available: {}".format(len(services)))

    # Friendly proof-of-life: make Pepper say hello.
    try:
        tts = session.service("ALTextToSpeech")
        tts.say("Connection successful. I am ready.")
    except Exception as e:
        print("(TTS test skipped: {})".format(e))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pepper connection diagnostic")
    parser.add_argument("--ip", required=True, help="Pepper IP (chest button says it)")
    parser.add_argument(
        "--port", type=int, default=9559, help="NAOqi port (default 9559)"
    )
    args = parser.parse_args()
    main(args.ip, args.port)
