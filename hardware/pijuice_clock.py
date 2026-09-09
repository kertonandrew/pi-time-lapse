#!/usr/bin/python3

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


NTP_SYNCHRONIZED = Path("/run/systemd/timesync/synchronized")
RTC_DEVICE = Path("/sys/bus/i2c/devices/1-0068")
RTC_DRIVER = Path("/sys/bus/i2c/drivers/rtc-ds1307")
MINIMUM_YEAR = 2024
MAXIMUM_SKEW_SECONDS = 2


class ClockError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def read_rtc(pijuice):
    result = pijuice.rtcAlarm.GetTime()
    if result.get("error") != "NO_ERROR":
        raise ClockError(f"Read RTC failed: {result.get('error', 'missing status')}")
    try:
        values = result["data"]
        hour = values["hour"]
        if isinstance(hour, str):
            hour, period = hour.split()
            hour = int(hour)
            if not 1 <= hour <= 12 or period not in ("AM", "PM"):
                return None
            hour = hour % 12 + (12 if period == "PM" else 0)
        value = datetime(
            values["year"],
            values["month"],
            values["day"],
            hour,
            values["minute"],
            values["second"],
            tzinfo=timezone.utc,
        )
        return value if MINIMUM_YEAR <= value.year <= 2099 else None
    except (KeyError, TypeError, ValueError):
        return None


def bind_rtc(device=RTC_DEVICE, driver=RTC_DRIVER):
    if not device.exists():
        raise ClockError("RTC device 1-0068 is missing; configure the ds1307 overlay")
    if (device / "name").read_text().strip() != "ds1307":
        raise ClockError("RTC device 1-0068 must be configured as ds1307")
    binding = device / "driver"
    if binding.exists():
        if binding.resolve() != driver.resolve():
            raise ClockError("RTC device 1-0068 is bound to an unexpected driver")
        return
    if not driver.exists():
        subprocess.run(
            ["/sbin/modprobe", "rtc_ds1307"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    if not binding.exists():
        (driver / "bind").write_text(device.name + "\n")
    if not binding.exists() or binding.resolve() != driver.resolve():
        raise ClockError("RTC driver binding did not complete")
    print("Bound PiJuice RTC to rtc-ds1307")


def synchronize_clock(pijuice, synchronized, now=utc_now, bind=bind_rtc):
    rtc_time = read_rtc(pijuice)
    system_time = now().astimezone(timezone.utc)
    if synchronized:
        if not MINIMUM_YEAR <= system_time.year <= 2099:
            raise ClockError(
                "Synchronized system date is outside the supported RTC range"
            )
        if (
            rtc_time is None
            or abs((rtc_time - system_time).total_seconds()) > MAXIMUM_SKEW_SECONDS
        ):
            result = pijuice.rtcAlarm.SetTime(
                {
                    "year": system_time.year,
                    "month": system_time.month,
                    "day": system_time.day,
                    "weekday": system_time.isoweekday() % 7 + 1,
                    "hour": system_time.hour,
                    "minute": system_time.minute,
                    "second": system_time.second,
                }
            )
            if result.get("error") != "NO_ERROR":
                raise ClockError(
                    f"Set RTC failed: {result.get('error', 'missing status')}"
                )
            rtc_time = read_rtc(pijuice)
            if (
                rtc_time is None
                or abs((rtc_time - now().astimezone(timezone.utc)).total_seconds())
                > MAXIMUM_SKEW_SECONDS
            ):
                raise ClockError("RTC readback differs from system time after repair")
            print(f"Synchronized PiJuice RTC to {rtc_time.isoformat()}")
    if rtc_time is None:
        raise ClockError(
            "RTC time is invalid; waiting for network time synchronization"
        )
    bind()


def main():
    try:
        if os.geteuid() != 0:
            raise ClockError("PiJuice clock recovery must run as root")
        from pijuice import PiJuice

        synchronize_clock(PiJuice(1, 0x14), NTP_SYNCHRONIZED.exists())
    except (ClockError, OSError, ImportError, subprocess.SubprocessError) as error:
        print(f"PiJuice clock recovery failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
