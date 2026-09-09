"""Check one Git revision for accidentally published private configuration."""

import argparse
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

MAX_FILE_BYTES = 5 * 1024 * 1024
PATTERNS = {
    "private_key": re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
    ),
    "github_token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{30,255})\b"
    ),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "service_token": re.compile(r"\b(?:sk_live_|sk-proj-|AIza)[A-Za-z0-9_-]{20,}\b"),
    "jwt": re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    ),
    "authenticated_url": re.compile(
        r"\b(?:https?|mqtts?|sftp|ssh|postgres(?:ql)?|mysql)://[^\s/:@]+:[^\s/@]+@"
    ),
    "personal_home_path": re.compile(
        r"/(?:Users|home)/(?!(?:camera|example|user|pi)(?![A-Za-z0-9_.-]))"
        r"[A-Za-z0-9_.-]+"
    ),
}
PRIVATE_IPV4 = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)
PRIVATE_IPV6 = re.compile(
    r"(?<![A-Za-z0-9:])(?:f[cd][a-f0-9]{2}|fe[89ab][a-f0-9])"
    r"(?::[a-f0-9]{0,4}){2,7}(?:%[A-Za-z0-9_.-]+)?",
    re.I,
)
LOCAL_HOST = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9_-]*\.local(?![A-Za-z0-9_.-])")
EXAMPLE_LOCAL_HOSTS = {
    "camera.local",
    "example.local",
    "homeassistant.local",
    "rc.local",
    "threading.local",
}
INLINE_CREDENTIAL = re.compile(
    r"(?i)(?<![\w])(?:password|passwd|passphrase|psk|api_key|access_token|"
    r"refresh_token|client_secret|secret_key)[\"']?\s*[:=]\s*([\"'])([^\r\n\"']+)\1"
)
EXAMPLE_CREDENTIALS = {"example-only-password", "REPLACE_ME"}
DOTTED_LOGIN = re.compile(r"\b([a-z][a-z0-9_-]*\.[a-z][a-z0-9_.-]*)@", re.I)
EXAMPLE_DOTTED_LOGINS = {"example.user", "camera.user"}
DEVICE_UUID = re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I)
COMPACT_DEVICE_ID = re.compile(r"\b[0-9a-f]{32}\b", re.I)
MAC_ADDRESS = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.I)
ARTIFACT_SUFFIXES = {
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".7z",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
    ".mp4",
    ".mov",
    ".h264",
    ".raw",
    ".dng",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".csv",
    ".jsonl",
}
PRIVATE_NAMES = {
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "authorized_keys",
    "known_hosts",
    "credentials",
    "credentials.json",
    "secrets.json",
    "passwords",
    "config.json",
}
PRIVATE_DIRECTORIES = {"local", ".ssh", "secrets", "spool", "photos", "telemetry"}
PUBLIC_DOC_PATHS = {
    "docs/README.md",
    "docs/deployment.md",
    "docs/camera-workflow.md",
    "docs/home-assistant.md",
    "docs/ssh-receiver.md",
    "docs/solar-transfer-design.md",
    "docs/battery-discharge-test.md",
    "docs/.gitignore",
}


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    category: str


def safe_path(path: str) -> str:
    for pattern in [
        *PATTERNS.values(),
        PRIVATE_IPV4,
        PRIVATE_IPV6,
        DOTTED_LOGIN,
        LOCAL_HOST,
        DEVICE_UUID,
        COMPACT_DEVICE_ID,
        MAC_ADDRESS,
    ]:
        path = pattern.sub("<redacted>", path)
    return path


def is_private_documentation(path: str) -> bool:
    relative = PurePosixPath(path)
    return relative.is_relative_to("docs") and str(relative) not in PUBLIC_DOC_PATHS


def scan_content(path: str, content: bytes) -> list[Finding]:
    """Return locations and categories without retaining matched values."""
    relative = PurePosixPath(path)
    findings = set()

    def add(line, category):
        findings.add(Finding(safe_path(path), line, category))

    if is_private_documentation(path):
        add(0, "private_documentation")
    if (
        any(part.lower() in PRIVATE_DIRECTORIES for part in relative.parts)
        or relative.name.lower() in PRIVATE_NAMES
        or (relative.name.startswith(".env") and relative.name not in {".env.example"})
        or relative.suffix.lower() in {".key", ".pem", ".p12", ".pfx", ".jks"}
        or re.search(r"\.(?:local|private)\.(?:json|toml|ya?ml)$", relative.name)
    ):
        add(0, "private_configuration_file")
    if relative.suffix.lower() in ARTIFACT_SUFFIXES:
        add(0, "private_artifact")
    if len(content) > MAX_FILE_BYTES:
        add(0, "file_exceeds_scan_limit")
        return sorted(findings)
    if b"\0" in content:
        add(0, "unreviewed_binary")
        return sorted(findings)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        add(0, "unreviewed_binary")
        return sorted(findings)
    for number, line in enumerate(text.splitlines(), 1):
        for category, pattern in PATTERNS.items():
            if pattern.search(line):
                add(number, category)
        for match in PRIVATE_IPV4.finditer(line):
            try:
                ipaddress.IPv4Address(match.group())
            except ipaddress.AddressValueError:
                continue
            add(number, "private_network_address")
        for match in PRIVATE_IPV6.finditer(line):
            try:
                ipaddress.IPv6Address(match.group().split("%", 1)[0])
            except ipaddress.AddressValueError:
                continue
            add(number, "private_network_address")
        if any(
            match.group() not in EXAMPLE_LOCAL_HOSTS
            for match in LOCAL_HOST.finditer(line)
        ):
            add(number, "installation_hostname")
        if any(
            match.group(2) not in EXAMPLE_CREDENTIALS
            for match in INLINE_CREDENTIAL.finditer(line)
        ):
            add(number, "inline_credential")
        if any(
            match.group(1) not in EXAMPLE_DOTTED_LOGINS
            for match in DOTTED_LOGIN.finditer(line)
        ):
            add(number, "personal_login_example")
        if relative.parts[0] in {"docs", "hardware"} and relative.suffix in {
            ".md",
            ".json",
        }:
            if any(
                not match.group().startswith("00000000-0000-4000-8000-")
                for match in DEVICE_UUID.finditer(line)
            ):
                add(number, "device_or_boot_identifier")
            if any(
                re.search(r"[a-f]", match.group(), re.I)
                for match in COMPACT_DEVICE_ID.finditer(line)
            ):
                add(number, "device_or_capture_identifier")
            if any(
                match.group() != "00:00:00:00:00:00"
                for match in MAC_ADDRESS.finditer(line)
            ):
                add(number, "network_hardware_identifier")
    return sorted(findings)


def git_bytes(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Could not read repository")
    return result.stdout


def scan_repository(
    root: Path, revision: str = "HEAD", working_tree: bool = False
) -> list[Finding]:
    findings = []
    if working_tree:
        entries = git_bytes(root, "ls-files", "--stage", "-z")
    else:
        commit = (
            git_bytes(
                root,
                "rev-parse",
                "--verify",
                "--end-of-options",
                revision + "^{commit}",
            )
            .decode("ascii")
            .strip()
        )
        entries = git_bytes(root, "ls-tree", "-rlz", "--full-tree", commit)
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, encoded_path = entry.split(b"\t", 1)
        path = encoded_path.decode("utf-8", errors="replace")
        if is_private_documentation(path):
            findings.append(Finding(safe_path(path), 0, "private_documentation"))
        fields = metadata.decode("ascii").split()
        mode = fields[0]
        if mode != "100644" and mode != "100755":
            findings.append(Finding(safe_path(path), 0, "unreviewed_link_or_submodule"))
            continue
        if working_tree:
            if fields[2] != "0":
                findings.append(Finding(safe_path(path), 0, "unmerged_file"))
                continue
            source = root / path
            if source.is_symlink():
                findings.append(
                    Finding(safe_path(path), 0, "unreviewed_link_or_submodule")
                )
                continue
            if not source.exists():
                continue
            if source.stat().st_size > MAX_FILE_BYTES:
                findings.append(Finding(safe_path(path), 0, "file_exceeds_scan_limit"))
                continue
            content = source.read_bytes()
        else:
            if int(fields[3]) > MAX_FILE_BYTES:
                findings.append(Finding(safe_path(path), 0, "file_exceeds_scan_limit"))
                continue
            content = git_bytes(root, "cat-file", "blob", fields[2])
        findings.extend(scan_content(path, content))
    return sorted(set(findings))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--working-tree", action="store_true")
    args = parser.parse_args(argv)
    try:
        findings = scan_repository(args.root, args.revision, args.working_tree)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        print(
            json.dumps({"file": "<repository>", "line": 0, "category": "scan_failed"})
        )
        return 2
    for finding in findings:
        print(
            json.dumps(
                {
                    "file": finding.path,
                    "line": finding.line,
                    "category": finding.category,
                }
            )
        )
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
