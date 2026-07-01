"""
pepper_settings.py -- open Pepper's on-tablet Settings menu.

No nao/SSH password needed: ALTabletService is a NAOqi service on the same
port (9559) you already connect to, and it can pop the Settings UI directly on
the chest tablet. Use it to change Wi-Fi, volume, language, etc. by hand --
most of what the (locked) robot web page offered, without any login.

Usage:
    python pepper_settings.py --ip <PEPPER_IP>
"""
from __future__ import print_function

import argparse
import sys

import qi

NAOQI_PORT = 9559


def main():
    parser = argparse.ArgumentParser(description="Open Pepper's tablet Settings")
    parser.add_argument("--ip", required=True, help="Pepper IP (chest button says it)")
    parser.add_argument("--port", type=int, default=NAOQI_PORT, help="NAOqi port")
    args = parser.parse_args()

    session = qi.Session()
    try:
        session.connect("tcp://{}:{}".format(args.ip, args.port))
    except RuntimeError:
        print("ERROR: can't reach NAOqi at {}:{}".format(args.ip, args.port))
        sys.exit(1)

    try:
        tablet = session.service("ALTabletService")
    except Exception as exc:
        print("ERROR: ALTabletService not available: {}".format(exc))
        sys.exit(1)

    # Wake the screen so the menu is actually visible.
    try:
        tablet.turnScreenOn(True)
    except Exception:
        pass

    try:
        tablet._openSettings()
    except Exception as exc:
        print("ERROR: could not open Settings: {}".format(exc))
        print("  Some firmware hides _openSettings(). Tell me and I'll fall back")
        print("  to showWebview on the local settings page instead.")
        sys.exit(1)

    print("Settings opened on Pepper's tablet.")
    print("Change Wi-Fi / volume / language there by hand -- no login needed.")


if __name__ == "__main__":
    main()
