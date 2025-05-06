import os
import time
import datetime
from picamera2 import Picamera2
import logging

# Setup logging
logging.basicConfig(
    filename='/home/pi/timelapse.log',
    level=logging.INFO,
    format='%(asctime)s - %(message)s'
)

def take_photo():
    try:
        # Create photos directory if it doesn't exist
        photos_dir = "/home/pi/photos"
        if not os.path.exists(photos_dir):
            os.makedirs(photos_dir)

        # Generate filename with timestamp
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{photos_dir}/image_{timestamp}.jpg"

        # Initialize camera with proper configuration
        logging.info("Initializing camera...")
        picam2 = Picamera2()

        # Create a configuration for still capture
        config = picam2.create_still_configuration()
        picam2.configure(config)

        # Start camera
        picam2.start()

        # Wait for auto-exposure to complete
        logging.info("Waiting for auto exposure to stabilize...")
        picam2.set_controls({"AeEnable": True})  # Ensure auto-exposure is enabled

        # Use the built-in method for letting auto-exposure settle
        # This is equivalent to what libcamera-still does
        time.sleep(0.5)  # Short delay to initialize
        picam2.switch_mode_and_capture_array(config)  # Trigger AE convergence

        # Take the actual photo
        logging.info(f"Taking photo: {filename}")
        picam2.capture_file(filename)

        # Close camera
        picam2.close()

        logging.info("Photo captured successfully")
        return True

    except Exception as e:
        logging.error(f"Error taking photo: {str(e)}")
        return False