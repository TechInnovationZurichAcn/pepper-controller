"""Standalone audio-playback test for the clown music.

Isolates the two possible reasons "the music didn't play":
  1. a code bug (fixed: the dance used a legacy `.post` proxy the qi bindings lack), or
  2. ALAudioPlayer can't decode the mp3 on this NAOqi build.

It uploads assets/clown-circus-music.mp3 to the robot, raises the speaker volume,
and plays it ONCE with the BLOCKING call so any decode error is printed here.

    python pepper_audio_test.py --ip <PEPPER_IP>

If you hear nothing and see a playback error, the file is the problem — re-encode
it to 44.1 kHz WAV (see the README) and pass --file clown-circus-music.wav.
"""
from __future__ import print_function

import argparse
import os
import sys

import qi

import pepper_features as feat

NAOQI_PORT = 9559


def main():
    parser = argparse.ArgumentParser(description="Test clown-music playback on Pepper")
    parser.add_argument("--ip", required=True, help="Pepper IP")
    parser.add_argument("--port", type=int, default=NAOQI_PORT)
    parser.add_argument("--file", default=feat.CLOWN_MP3, help="audio filename in assets/")
    parser.add_argument("--volume", type=int, default=80, help="output volume 0-100")
    args = parser.parse_args()

    session = qi.Session()
    try:
        session.connect("tcp://{}:{}".format(args.ip, args.port))
    except RuntimeError:
        print("ERROR: can't reach NAOqi at {}:{}".format(args.ip, args.port))
        sys.exit(1)

    assets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    remote = feat.deploy_audio_to_robot(args.ip, assets_dir, filename=args.file)
    if not remote:
        print("ERROR: upload failed — see the WARN above (SSH/scp issue).")
        sys.exit(1)

    try:
        audio = session.service("ALAudioPlayer")
    except Exception as exc:
        print("ERROR: ALAudioPlayer unavailable: {}".format(exc))
        sys.exit(1)

    try:
        dev = session.service("ALAudioDevice")
        print("output volume was {}, setting {}".format(dev.getOutputVolume(), args.volume))
        dev.setOutputVolume(args.volume)
    except Exception as exc:
        print("WARN: could not set volume: {}".format(exc))

    print("playing (blocking) {} ...".format(remote))
    try:
        audio.playFile(remote)   # blocking; returns when the track finishes
        print("OK: playback finished — audio works. The dance will now have sound.")
    except Exception as exc:
        print("FAIL: ALAudioPlayer could not play it: {}".format(exc))
        print("  -> the mp3 is likely the problem. Re-encode to 44.1 kHz WAV:")
        print("     ffmpeg -i assets/{f} -ar 44100 -ac 2 assets/{stem}.wav".format(
            f=args.file, stem=os.path.splitext(args.file)[0]))
        print("     then set CLOWN_MP3 = \"{stem}.wav\" in pepper_features.py".format(
            stem=os.path.splitext(args.file)[0]))
        sys.exit(1)


if __name__ == "__main__":
    main()
