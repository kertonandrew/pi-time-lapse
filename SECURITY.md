# Security policy

This project controls a camera, network transfers and battery-powered hardware. Use the current maintained branch and rerun the documented checks after changing configuration. Historical trial results describe one installation; they are not a hardware certification or a charging specification for another battery.

## Report a vulnerability privately

Use the repository's **Security → Advisories → Report a vulnerability** option when GitHub private vulnerability reporting is enabled. Include the affected commit, a minimal reproduction and the impact. If that option is unavailable, open an issue requesting a private reporting channel without including exploit details or secrets. Do not post credentials, private photographs, local network configuration or unredacted telemetry in public issues.

No disclosure response time or independent security certification is currently promised.

## Keep installation data private

Store actual installation configuration and exports under the ignored `local/` directory, or outside the repository. Keep only explicit example configuration in version control. Confirm a file is ignored before placing credentials or photographs there; Git ignore rules do not protect files that are already tracked or deliberately added with `git add -f`.

Passwords belong in restrictive credential files, not configuration literals, shell arguments, Git commits or logs. Use directory mode 0700 and file mode 0600 for local private material. Protect backups to the same standard. Configure your own device ID, endpoints, SSH account and data directories through the setup flow rather than copying installation details from historical records.

MQTT should use verified TLS, a dedicated identity per camera and exact topic ACLs. Give telemetry publishers, photo publishers and command publishers only the permissions their role requires. Discovery access is configuration access: restrict who can publish under the discovery prefix. Treat control-topic write permission as permission to change the camera's behavior. Keep the broker on a trusted network or authenticated private connection; do not expose an anonymous listener to the internet. See the [Home Assistant setup](docs/home-assistant.md) and the supplied [broker ACL example](deploy/home-assistant/mosquitto.acl.example).

Use strict SSH host-key verification and a dedicated restricted upload identity for the archive server. Follow the [SSH receiver guide](docs/ssh-receiver.md) to restrict the key to the gateway and disable unrelated shell, forwarding and PTY access. Do not grant the camera a general-purpose administrative SSH key. The receiver validates uploads, but filesystem ownership and the SSH account boundary still matter.

Do not publish live camera images or device telemetry through an unauthenticated dashboard. Archive and broker retention are separate copies of the data; deleting the camera's local copy does not delete those copies. Do not enable hardware charging or power controls based solely on a software test result. Battery chemistry, protection, wiring, temperature sensing and charge-current limits require their own qualification.

## Public repository checks

Run the stdlib release guard from a clone:

```sh
python3 ops/check_public_repo.py
```

It checks the tracked files at `HEAD`, including binary/artifact filenames, common private-key and service-token patterns, inline credentials, private network defaults, personal home paths and unsanitized device identifiers in documentation. It prints only file, line and category, and returns a nonzero status for findings or scan failures. It never verifies suspected credentials against external services.

To check tracked local edits before committing:

```sh
python3 ops/check_public_repo.py --working-tree
```

Untracked files are outside that check until added to Git. The guard deliberately rejects private runtime directories, local configuration, key containers, archives, photographs and telemetry exports. It does not broadly exempt test directories or example files: embedded credentials are still checked. The exact non-credential literals `example-only-password` and `REPLACE_ME`, documented generic accounts/endpoints and the zero-prefixed example UUID format are narrow exceptions. New intentional public fixtures or binary assets require an explicit reviewed rule change.

CI runs the guard, application and hardware unit tests, a locked dependency installation and a package build on Python 3.11 and 3.13. Actions are pinned to commit IDs and the workflow has read-only repository permissions. Unit tests do not contact a real Pi or qualify battery safety. The opt-in Home Assistant and MQTT security acceptance scripts use isolated synthetic services and must be run separately when changing those interfaces.

The guard is a focused prevention check, not a complete secret scanner or a proof that a repository is safe to publish. It checks one revision, not historical commits, untracked local files, external archives, release attachments or forks. Review new data files and configuration manually, and use a redacted history-capable scanner when assessing prior exposure. Keep dependencies and pinned CI tools updated through reviewed changes.

## If something private was published

Revoke or rotate any exposed credential first. Removing a file, ignoring its path or making a repository private does not invalidate a credential already copied by someone else.

A normal cleanup commit removes data from the current tree while leaving earlier commits reachable. History rewriting requires a separate reviewed plan, private backups and coordination with collaborators. Even after a rewrite, forks, caches, old clones and hosting-provider artifacts may retain copies. Do not claim complete removal without checking those locations, and do not rewrite history merely to make the current-revision guard pass.
