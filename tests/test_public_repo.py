from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ops import check_public_repo as guard


class ContentChecks(unittest.TestCase):
    def categories(self, content, path="README.md"):
        return {item.category for item in guard.scan_content(path, content.encode())}

    def test_private_keys_and_provider_tokens_are_detected_without_storing_values(self):
        samples = {
            "private_key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "github_token": "ghp_" + "Ab9" * 12,
            "aws_access_key": "AKIA" + "Q7" * 8,
            "slack_token": "xoxb-" + "Q7" * 12,
            "service_token": "sk-proj-" + "Q7" * 20,
            "jwt": "eyJ" + "a" * 15 + "." + "b" * 20 + "." + "c" * 20,
        }
        for category, value in samples.items():
            with self.subTest(category=category):
                found = guard.scan_content("settings.py", value.encode())
                self.assertIn(category, {item.category for item in found})
                self.assertNotIn(value, repr(found))

    def test_inline_credentials_and_authenticated_urls_are_detected(self):
        value = "unissued-" + "Z3" * 12
        self.assertIn(
            "inline_credential", self.categories(json.dumps({"password": value}))
        )
        url = "mqtts://operator:" + value + "@broker.example:8883"
        self.assertIn("authenticated_url", self.categories(url))

    def test_example_password_exemption_is_exact_and_other_tokens_still_fail(self):
        content = json.dumps({"password": "example-only-password"})
        self.assertNotIn("inline_credential", self.categories(content))
        value = "example-only-password" + "-changed"
        self.assertIn(
            "inline_credential", self.categories(json.dumps({"password": value}))
        )
        token = "ghp_" + "Z8" * 20
        self.assertIn("github_token", self.categories(content + "\n" + token))

    def test_documented_endpoints_and_personal_locations_are_distinguished(self):
        examples = "camera.local example.local homeassistant.local 192.0.2.10 2001:db8::1 /home/camera/config.json"
        self.assertEqual(self.categories(examples), set())
        cases = {
            "private_network_address": [
                "192" + ".168.4.21",
                "10" + ".2.3.4",
                "172" + ".16.3.4",
                "fd12" + ":3456::1",
                "fe80" + "::1%wlan0",
            ],
            "personal_home_path": [
                "/" + "Users/operator/project",
                "/" + "home/camera.person/config.json",
            ],
            "installation_hostname": ["field-unit" + ".local"],
            "personal_login_example": ["person" + ".name@camera.local"],
        }
        for category, values in cases.items():
            for value in values:
                with self.subTest(category=category):
                    self.assertIn(category, self.categories(value))

    def test_runtime_artifacts_and_local_configuration_are_rejected(self):
        paths = [
            "local/site.json",
            "deploy/config.json",
            "site.local.json",
            ".env.production",
            "secrets/password",
            "client.key",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertIn("private_configuration_file", self.categories("", path))
        for path in ["handover.zip", "photo.jpg", "samples.csv", "metrics.sqlite3"]:
            with self.subTest(path=path):
                self.assertIn("private_artifact", self.categories("", path))

    def test_example_filenames_do_not_exempt_embedded_credentials(self):
        path = "deploy/home-assistant/config.example.json"
        value = "unissued-" + "Z3" * 12
        content = json.dumps({"password": value})
        self.assertIn("inline_credential", self.categories(content, path))
        self.assertEqual(self.categories('{"password_file": null}', path), set())
        self.assertEqual(self.categories("", ".env.example"), set())

    def test_document_identity_placeholders_and_checksums_are_allowed(self):
        example = "00000000-0000-4000-8000-000000000001"
        self.assertEqual(self.categories(example, "hardware/example.json"), set())
        identifier = "abcdabcd-" + "1234-4321-8123-abcdefabcdef"
        self.assertIn(
            "device_or_boot_identifier",
            self.categories(identifier, "hardware/example.json"),
        )
        checksum = "abcdef0123456789" * 4
        self.assertEqual(self.categories(checksum, "hardware/example.json"), set())
        compact_id = "abc123" * 5 + "aa"
        self.assertIn(
            "device_or_capture_identifier",
            self.categories(compact_id, "hardware/example.json"),
        )
        mac = ":".join(["ab"] * 6)
        self.assertIn(
            "network_hardware_identifier", self.categories(mac, "hardware/README.md")
        )

    def test_only_curated_documentation_paths_are_public(self):
        for path in (
            "docs/README.md",
            "docs/deployment.md",
            "docs/camera-workflow.md",
            "docs/home-assistant.md",
            "docs/ssh-receiver.md",
            "docs/solar-transfer-design.md",
            "docs/battery-charging-trial.md",
            "docs/battery-discharge-test.md",
            "docs/.gitignore",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.categories("Public guide\n", path), set())
        for path in (
            "docs/session-2026-09-09.md",
            "docs/receipt.json",
            "docs/solar-field-session.md",
            "docs/archive/README.md",
            "docs/README.md.backup",
            "docs/Readme.md",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.categories("", path), {"private_documentation"})

    def test_permitted_guide_still_checks_for_credentials(self):
        value = "unissued-" + "Z3" * 12
        content = json.dumps({"password": value})
        self.assertEqual(
            self.categories(content, "docs/deployment.md"), {"inline_credential"}
        )

    def test_binary_and_oversized_inputs_fail_closed(self):
        found = guard.scan_content("unknown.data", b"\x00secret")
        self.assertEqual({item.category for item in found}, {"unreviewed_binary"})
        with patch.object(guard, "MAX_FILE_BYTES", 4):
            found = guard.scan_content("oversize.txt", b"12345")
        self.assertEqual({item.category for item in found}, {"file_exceeds_scan_limit"})

    def test_output_is_locations_only_even_when_filename_contains_a_token(self):
        token = "ghp_" + "Ab9" * 12
        found = guard.scan_content(token + ".txt", token.encode())
        output = StringIO()
        with patch.object(guard, "scan_repository", return_value=found):
            with redirect_stdout(output):
                self.assertEqual(guard.main([]), 1)
        self.assertNotIn(token, output.getvalue())
        for line in output.getvalue().splitlines():
            self.assertEqual(set(json.loads(line)), {"file", "line", "category"})


class RepositoryChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "--quiet")

    def git(self, *arguments):
        return subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=Example",
                "-c",
                "user.email=example@example.invalid",
                *arguments,
            ],
            check=True,
            capture_output=True,
        ).stdout

    def commit(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git("add", "--", path)
        self.git("commit", "--quiet", "-m", "Create synthetic fixture")

    def test_default_reads_head_while_working_tree_mode_reads_tracked_edits(self):
        self.commit("README.md", "Public example\n")
        token = "ghp_" + "C8" * 20
        (self.root / "README.md").write_text(token)
        self.assertEqual(guard.scan_repository(self.root), [])
        self.assertIn(
            "github_token",
            {f.category for f in guard.scan_repository(self.root, working_tree=True)},
        )

    def test_guard_checks_current_revision_without_demanding_history_rewrite(self):
        token = "ghp_" + "C8" * 20
        self.commit("README.md", token)
        bad_commit = self.git("rev-parse", "HEAD").decode().strip()
        self.commit("README.md", "Public example\n")
        self.assertEqual(guard.scan_repository(self.root), [])
        self.assertIn(
            "github_token",
            {f.category for f in guard.scan_repository(self.root, bad_commit)},
        )

    def test_untracked_private_file_is_excluded_until_explicitly_added(self):
        self.commit("README.md", "Public example\n")
        local = self.root / "local"
        local.mkdir()
        (local / "config.json").write_text("{}")
        self.assertEqual(guard.scan_repository(self.root, working_tree=True), [])
        self.git("add", "local/config.json")
        self.assertIn(
            "private_configuration_file",
            {f.category for f in guard.scan_repository(self.root, working_tree=True)},
        )

    def test_force_tracked_private_docs_fail_both_repository_scan_modes(self):
        self.commit("docs/.gitignore", "*\n!.gitignore\n!README.md\n")
        paths = (
            "docs/session-2026-09-09.md",
            "docs/receipt.json",
            "docs/solar-field-session.md",
        )
        for path in paths:
            (self.root / path).write_text("{}\n")
            self.git("check-ignore", "--", path)
        self.assertEqual(guard.scan_repository(self.root, working_tree=True), [])
        self.git("add", "--force", "--", *paths)
        expected = {guard.Finding(path, 0, "private_documentation") for path in paths}
        self.assertEqual(
            set(guard.scan_repository(self.root, working_tree=True)), expected
        )
        self.git("commit", "--quiet", "-m", "Track synthetic private documents")
        self.assertEqual(set(guard.scan_repository(self.root)), expected)

    def test_curated_guides_pass_repository_scan(self):
        self.commit("docs/deployment.md", "Public deployment guide\n")
        self.commit("docs/battery-charging-trial.md", "Public battery guide\n")
        self.assertEqual(guard.scan_repository(self.root), [])
        self.assertEqual(guard.scan_repository(self.root, working_tree=True), [])

    def test_private_documentation_category_survives_size_limit(self):
        path = "docs/receipt.json"
        self.commit(path, "12345")
        with patch.object(guard, "MAX_FILE_BYTES", 4):
            for working_tree in (False, True):
                with self.subTest(working_tree=working_tree):
                    self.assertEqual(
                        set(
                            guard.scan_repository(self.root, working_tree=working_tree)
                        ),
                        {
                            guard.Finding(path, 0, "private_documentation"),
                            guard.Finding(path, 0, "file_exceeds_scan_limit"),
                        },
                    )

    def test_symlinks_are_not_followed(self):
        self.commit("README.md", "Public example\n")
        (self.root / "link").symlink_to("README.md")
        self.git("add", "link")
        self.assertEqual(
            {f.category for f in guard.scan_repository(self.root, working_tree=True)},
            {"unreviewed_link_or_submodule"},
        )

    def test_git_failure_has_a_generic_redacted_error(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(guard.main(["--root", str(self.root)]), 2)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"file": "<repository>", "line": 0, "category": "scan_failed"},
        )


if __name__ == "__main__":
    unittest.main()
