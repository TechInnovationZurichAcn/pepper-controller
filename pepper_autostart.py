"""
pepper_autostart.py -- make an installed behavior run automatically at boot.

Once set, Pepper runs the follow-me app on its own tablet at startup, with no
laptop and no router -- the tablet talks to NAOqi internally. Uses
ALBehaviorManager over NAOqi 9559 (no SSH password needed).

The behavior must already be INSTALLED on the robot (Choregraphe: Package &
Install). Live-Play behaviors are not installed, so install once first.

Usage:
    python pepper_autostart.py --ip <IP> --list
    python pepper_autostart.py --ip <IP> --enable  followme/behavior_1
    python pepper_autostart.py --ip <IP> --disable followme/behavior_1
"""
from __future__ import print_function

import argparse
import sys

import qi

NAOQI_PORT = 9559


def main():
    parser = argparse.ArgumentParser(description="Run a behavior at boot")
    parser.add_argument("--ip", required=True, help="Pepper IP")
    parser.add_argument("--port", type=int, default=NAOQI_PORT)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list installed behaviors")
    group.add_argument("--enable", metavar="BEHAVIOR", help="run this behavior at boot")
    group.add_argument("--disable", metavar="BEHAVIOR", help="stop running it at boot")
    args = parser.parse_args()

    session = qi.Session()
    try:
        session.connect("tcp://{}:{}".format(args.ip, args.port))
    except RuntimeError:
        print("ERROR: can't reach NAOqi at {}:{}".format(args.ip, args.port))
        sys.exit(1)

    bm = session.service("ALBehaviorManager")

    if args.list:
        defaults = set(bm.getDefaultBehaviors())
        print("Installed behaviors (* = runs at boot):")
        for name in bm.getInstalledBehaviors():
            print("  [{}] {}".format("*" if name in defaults else " ", name))
        return

    if args.enable:
        if not bm.isBehaviorInstalled(args.enable):
            print("ERROR: behavior not installed: {}".format(args.enable))
            print("Package & Install it from Choregraphe first, then --list.")
            sys.exit(1)
        bm.addDefaultBehavior(args.enable)
        print("Will run at boot: {}".format(args.enable))
    else:  # --disable
        bm.removeDefaultBehavior(args.disable)
        print("No longer runs at boot: {}".format(args.disable))


if __name__ == "__main__":
    main()
