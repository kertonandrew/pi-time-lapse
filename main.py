
import time
import subprocess
import logging

from take_photo import take_photo

# Setup logging
logging.basicConfig(
    filename='/home/pi/timelapse.log',
    level=logging.INFO,
    format='%(asctime)s - %(message)s'
)

def main():
    logging.info("Starting timelapse script")

    # Take photo
    success = take_photo()

    # Allow time for file writing to complete
    time.sleep(1)

    # Return to deep sleep by shutting down
    logging.info("Preparing for shutdown")
    time.sleep(1)  # Give time for log to write

    # Execute shutdown command
    subprocess.call(['sudo', 'shutdown', '-h', 'now'])

if __name__ == "__main__":
    main()