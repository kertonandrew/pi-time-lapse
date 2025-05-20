import os
import datetime
import subprocess
from pathlib import Path
from logger import get_logger

# Setup logging
logger = get_logger()


def take_photo(photos_dir):
    try:
        # Create photos directory if it doesn't exist
        if not os.path.exists(photos_dir):
            os.makedirs(photos_dir)

        # Generate filename with timestamp
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{photos_dir}/image_{timestamp}.jpg"

        # Use libcamera-still CLI command to take photo
        logger.info(f"Taking photo with libcamera-still: {filename}")
        cmd = ["libcamera-still", "-o", filename, "--immediate"]

        # Execute the command
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            logger.info("Photo captured successfully")
        else:
            logger.error(f"Error taking photo: {result.stderr}")

    except Exception as e:
        logger.error(f"Error taking photo: {str(e)}")


def test_camera():
    from logger import get_logger

    logger = get_logger()

    logger.info("Starting timelapse script")

    PROJECT_ROOT = Path(os.getcwd())

    logger.info("photos_dir: %s", PROJECT_ROOT / "test_photos")

    take_photo(PROJECT_ROOT / "test_photos")

    logger.info("test_camera script completed")
