#!/usr/bin/env python

import sys

from timelapse.cli import main


if __name__ == "__main__":
    arguments = ["capture", "--dry-run"] if sys.argv[1:] == ["--test"] else sys.argv[1:]
    raise SystemExit(main(arguments))
