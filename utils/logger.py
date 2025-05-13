import os
import logging
from pathlib import Path

PROJECT_ROOT =  Path(os.getcwd())
LOG_DIR = PROJECT_ROOT / "logs"

# Ensure log directory exists
os.makedirs(LOG_DIR, exist_ok=True)

# Configure logger
def get_logger(name="pijuice_controller"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    # Create handlers
    file_handler = logging.FileHandler(LOG_DIR / "pijuice.log")
    console_handler = logging.StreamHandler()

    # Create formatters
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # Add handlers to logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger