#!/usr/bin/python3
import os
from pathlib import Path
from pijuice import PiJuice
from datetime import datetime
from utils.logger import get_logger

logger = get_logger()

def configure_pijuice(test_mode=False):
    # Initialize PiJuice
    pijuice = PiJuice(1, 0x14)

    # Configure script to run on wake up
    PROJECT_ROOT = Path(__file__).resolve().parent
    script_path = PROJECT_ROOT / "main.py"
    pijuice.power.SetWakeUpOnCharge(0)  # Disable wake on charge

    # Configure the system event
    system_events = pijuice.config.GetSystemEvents()
    system_events['wake_up'] = {
        'enabled': True,
        'command': f'python3 {script_path}'
    }
    pijuice.config.SetSystemEvents(system_events)

    if test_mode:
        # In testing mode, use periodic wakeup every minute
        logger.info("Testing mode enabled, setting wake time for every minute")
        pijuice.rtcAlarm.SetPeriodicWakeup(1, "MINUTE")
        pijuice.rtcAlarm.SetPeriodicWakeupEnabled(True)
        return

    # Get current hour
    current_hour = datetime.now().hour
    logger.info(f"Current hour: {current_hour}")

    # Disable any periodic wakeup that might be set
    pijuice.rtcAlarm.SetPeriodicWakeupEnabled(False)

    # Only set wake alarm if current time is night (so it wakes in day)
    if current_hour < 6 or current_hour > 18:
        # Calculate next wake time (e.g., 8:00 AM)
        logger.info("Nighttime detected, setting wake time for 8:00 AM")
        next_wake = {
            'day': 'EVERY_DAY',
            'hour': 8,
            'minute': 0
        }
    else:
        # During day, set next wake for current hour + 30 minutes
        current_minute = datetime.now().minute
        next_hour = current_hour
        next_minute = current_minute + 30

        # Handle minute overflow
        if next_minute >= 60:
            next_minute = next_minute % 60
            next_hour = (current_hour + 1) % 24

        logger.info(f"Daytime detected, setting wake time for {next_hour}:{next_minute:02d}")
        next_wake = {
            'day': 'EVERY_DAY',
            'hour': next_hour,
            'minute': next_minute
        }

    pijuice.rtcAlarm.SetAlarm(next_wake)
    pijuice.rtcAlarm.SetWakeupEnabled(True)
