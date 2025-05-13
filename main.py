#!/usr/bin/python3
import time
import os
import subprocess
from pathlib import Path
from utils.logger import get_logger

from camera import take_photo
from pijuice import configure_pijuice

PROJECT_ROOT =  Path(os.getcwd())
logger = get_logger()

def main(test_mode=False):
    logger.info("Starting timelapse script")

    configure_pijuice(test_mode)
    take_photo(path=PROJECT_ROOT / "photos")

    # Allow time for file writing to complete
    time.sleep(1)

    # Return to deep sleep by shutting down
    logger.info("Preparing for shutdown")
    time.sleep(1)  # Give time for log to write

    # Execute shutdown command
    subprocess.call(['sudo', 'shutdown', '-h', 'now'])

if __name__ == "__main__":
    import sys
    test_mode = "--test" in sys.argv
    main(test_mode)