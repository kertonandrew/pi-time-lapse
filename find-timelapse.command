#!/usr/bin/env python3

import concurrent.futures
import datetime
import errno
import ipaddress
import re
import socket
import subprocess
from pathlib import Path


REPORT_PATH = Path("/private/tmp/pi-network-scan.txt")


def command_output(arguments, timeout=5):
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=timeout
        )
        return (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return "Command timed out: " + " ".join(arguments)


def inspect_host(address):
    ip = str(address)
    try:
        ping = subprocess.run(
            ["/sbin/ping", "-n", "-c", "1", "-W", "300", ip],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        ping_replied = ping.returncode == 0
    except subprocess.TimeoutExpired:
        ping_replied = False
    banner = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.6)
            result = connection.connect_ex((ip, 22))
            if result == 0:
                try:
                    banner = connection.recv(256).decode(errors="replace").strip()
                except OSError:
                    pass
    except OSError as error:
        result = error.errno
    return ip, ping_replied, result, banner


def main():
    with REPORT_PATH.open("w") as report:

        def emit(message):
            print(message, flush=True)
            report.write(message + "\n")
            report.flush()

        emit("Pi discovery: " + datetime.datetime.now().isoformat(timespec="seconds"))
        interface = command_output(["/sbin/ifconfig", "en0"])
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+)", interface)
        if not match:
            emit("No IPv4 address found on Wi-Fi interface en0.")
            return
        own_ip = match.group(1)
        netmask = str(ipaddress.IPv4Address(int(match.group(2), 16)))
        network = ipaddress.ip_network(own_ip + "/" + netmask, strict=False)
        if network.num_addresses > 1024:
            emit(
                "Network is larger than 1024 addresses; stopping for a narrower scan range."
            )
            return
        emit("Wi-Fi subnet: " + str(network))
        emit("Checking remembered hostname camera.local:")
        emit(
            command_output(
                ["/usr/bin/dscacheutil", "-q", "host", "-a", "name", "camera.local"],
                6,
            )
            or "No hostname result."
        )
        emit("Scanning for ping replies and SSH listeners; usually under one minute.")
        addresses = [address for address in network.hosts() if str(address) != own_ip]
        ssh_hosts = 0
        denied = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
            for ip, ping_replied, result, banner in executor.map(
                inspect_host, addresses
            ):
                if ping_replied or result == 0:
                    status = "SSH OPEN" if result == 0 else "SSH not open"
                    emit(
                        f"{ip}: ping={ping_replied}, {status}"
                        + (f", {banner}" if banner else "")
                    )
                ssh_hosts += result == 0
                denied += result in (errno.EPERM, errno.EACCES)
        emit("SSH hosts found: " + str(ssh_hosts))
        if denied:
            emit("Network permission errors: " + str(denied))
        emit("Neighbor cache after scan:")
        emit(command_output(["/usr/sbin/arp", "-an"]))
        emit("Results saved to " + str(REPORT_PATH))


if __name__ == "__main__":
    main()
