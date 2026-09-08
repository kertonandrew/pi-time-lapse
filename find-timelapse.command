#!/usr/bin/env python3

"""Discover SSH listeners on one bounded IPv4 subnet and save a private report."""

import argparse
import concurrent.futures
import datetime
import errno
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile


MAX_ADDRESSES = 1024
INTERFACE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
HOSTNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}\Z")
RESOLVE_CODE = (
    "import json,socket,sys; "
    "print(json.dumps(sorted({item[4][0] for item in "
    "socket.getaddrinfo(sys.argv[1],None,socket.AF_INET,socket.SOCK_STREAM)})))"
)


class DiscoveryError(RuntimeError):
    pass


def subnet_argument(value):
    try:
        network = ipaddress.IPv4Network(value, strict=False)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError) as error:
        raise argparse.ArgumentTypeError(
            "Use an IPv4 subnet such as 192.0.2.0/24"
        ) from error
    if network.num_addresses > MAX_ADDRESSES:
        raise argparse.ArgumentTypeError(
            "Scan range must contain at most 1024 addresses"
        )
    return network


def interface_argument(value):
    if not INTERFACE_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("Invalid network interface name")
    return value


def hostname_argument(value):
    if not HOSTNAME_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("Invalid optional hostname")
    return value


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--interface",
        type=interface_argument,
        help="Interface to inspect; otherwise use the default route",
    )
    source.add_argument(
        "--subnet",
        type=subnet_argument,
        help="Explicit IPv4 scan range, independent of interface discovery",
    )
    parser.add_argument(
        "--hostname",
        type=hostname_argument,
        help="Optional remembered hostname to resolve",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Create a new mode-0600 report at this path; existing paths are refused",
    )
    return parser.parse_args(argv)


def command_output(arguments, timeout=5):
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        raise DiscoveryError("Discovery command timed out") from error
    except OSError as error:
        raise DiscoveryError("Discovery command could not start") from error
    if result.returncode:
        raise DiscoveryError("Discovery command failed")
    return result.stdout.strip()


def _executable(name):
    executable = shutil.which(name)
    if executable is None:
        raise DiscoveryError(
            f"{name} is unavailable; use --subnet to specify the scan range"
        )
    return executable


def discover_subnet(interface=None, system=None):
    system = platform.system() if system is None else system
    if system == "Darwin":
        if interface is None:
            route = command_output([_executable("route"), "-n", "get", "default"])
            match = re.search(r"^\s*interface:\s*(\S+)\s*$", route, re.M)
            if match is None:
                raise DiscoveryError(
                    "No default interface found; use --interface or --subnet"
                )
            interface = interface_argument(match.group(1))
        output = command_output([_executable("ifconfig"), interface])
        match = re.search(
            r"\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+|[0-9.]+)", output
        )
        if match is None:
            raise DiscoveryError("No IPv4 address found on the selected interface")
        own_ip, mask = match.groups()
        if mask.startswith("0x"):
            mask = str(ipaddress.IPv4Address(int(mask, 16)))
        network = subnet_argument(f"{own_ip}/{mask}")
    elif system == "Linux":
        executable = _executable("ip")
        if interface is None:
            routes = json.loads(
                command_output([executable, "-j", "-4", "route", "show", "default"])
            )
            if (
                not isinstance(routes, list)
                or not routes
                or not isinstance(routes[0], dict)
                or not isinstance(routes[0].get("dev"), str)
            ):
                raise DiscoveryError(
                    "No default interface found; use --interface or --subnet"
                )
            interface = interface_argument(routes[0]["dev"])
        interfaces = json.loads(
            command_output(
                [executable, "-j", "-4", "address", "show", "dev", interface]
            )
        )
        addresses = [
            address
            for item in interfaces
            for address in item.get("addr_info", [])
            if address.get("family") == "inet"
        ]
        if not addresses:
            raise DiscoveryError("No IPv4 address found on the selected interface")
        own_ip = addresses[0]["local"]
        network = subnet_argument(f"{own_ip}/{addresses[0]['prefixlen']}")
    else:
        raise DiscoveryError(
            "Automatic interface discovery supports macOS/Linux; use --subnet on this platform"
        )
    return network, str(ipaddress.IPv4Address(own_ip)), interface


def ping_command(system=None):
    executable = shutil.which("ping")
    if executable is None:
        return None
    system = platform.system() if system is None else system
    if system == "Darwin":
        return [executable, "-n", "-c", "1", "-W", "300"]
    if system == "Linux":
        return [executable, "-n", "-c", "1", "-W", "1"]
    if system == "Windows":
        return [executable, "-n", "1", "-w", "300"]
    return None


def _safe_banner(value):
    return "".join(character if " " <= character <= "~" else "?" for character in value)


def inspect_host(address, ping_arguments=None):
    ip = str(address)
    ping_replied = None
    if ping_arguments is not None:
        try:
            ping = subprocess.run(
                [*ping_arguments, ip], capture_output=True, text=True, timeout=1.5
            )
            ping_replied = ping.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ping_replied = False
    banner = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.6)
            result = connection.connect_ex((ip, 22))
            if result == 0:
                try:
                    banner = _safe_banner(
                        connection.recv(256).decode(errors="replace").strip()
                    )
                except OSError:
                    pass
    except OSError as error:
        result = error.errno or errno.EIO
    return ip, ping_replied, result, banner


def create_report(path=None):
    """Create a private new report without following or replacing existing files."""
    if path is None:
        descriptor, name = tempfile.mkstemp(prefix="pi-network-scan-", suffix=".txt")
        path = Path(name)
    else:
        path = Path(path).absolute()
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise OSError(
                errno.ENOTSUP, "Explicit reports require safe no-follow creation"
            )
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow, 0o600
        )
    try:
        return path, os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise


def neighbor_cache(system):
    if system == "Linux" and shutil.which("ip"):
        return command_output([shutil.which("ip"), "-4", "neighbour", "show"])
    executable = shutil.which("arp")
    if executable:
        return command_output([executable, "-a" if system == "Windows" else "-an"])
    return "Neighbor-cache command is unavailable."


def main(argv=None):
    arguments = parse_arguments(argv)
    try:
        path, report = create_report(arguments.report)
    except (OSError, ValueError):
        print(
            "Cannot create report: use a new path in a writable directory.",
            file=sys.stderr,
        )
        return 1
    with report:

        def emit(message):
            print(message, flush=True)
            report.write(message + "\n")
            report.flush()

        emit("Pi discovery: " + datetime.datetime.now().isoformat(timespec="seconds"))
        emit("Report: " + str(path))
        try:
            system = platform.system()
            if arguments.subnet is not None:
                network, own_ip = arguments.subnet, None
                emit("Explicit subnet: " + str(network))
            else:
                network, own_ip, interface = discover_subnet(
                    arguments.interface, system
                )
                emit(f"Interface {interface} subnet: {network}")
            if arguments.hostname:
                emit("Checking remembered hostname " + arguments.hostname + ":")
                try:
                    emit(
                        command_output(
                            [
                                sys.executable,
                                "-I",
                                "-c",
                                RESOLVE_CODE,
                                arguments.hostname,
                            ],
                            timeout=6,
                        )
                        or "No hostname result."
                    )
                except DiscoveryError as error:
                    emit("Hostname lookup unavailable: " + str(error))
            ping_arguments = ping_command(system)
            if ping_arguments is None:
                emit(
                    "Ping is unavailable on this platform; checking SSH listeners only."
                )
            emit("Scanning for ping replies and SSH listeners.")
            addresses = [
                address for address in network.hosts() if str(address) != own_ip
            ]
            ssh_hosts = 0
            denied = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
                for ip, ping_replied, result, banner in executor.map(
                    lambda address: inspect_host(address, ping_arguments), addresses
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
            try:
                emit(neighbor_cache(system))
            except DiscoveryError as error:
                emit(str(error))
            emit("Results saved to " + str(path))
            return 0
        except (
            DiscoveryError,
            ValueError,
            TypeError,
            KeyError,
            argparse.ArgumentTypeError,
        ) as error:
            emit("Discovery stopped: " + str(error))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
