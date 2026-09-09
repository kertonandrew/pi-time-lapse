# Public repository guidance

This repository is public. Keep committed documentation reusable for external
users: supported behavior, setup guides, configuration examples and reproducible
procedures.

Keep private test plans, bench journals, iteration notes, deployment receipts,
measurements, screenshots, photographs and generated reports under ignored
`local/` directories. This includes sanitized records of a particular installation.
Do not add those files to `docs/`, commit them, or force-add ignored artifacts.
Promote a result into public documentation only after an explicit publication
review; label measurement limits and remove installation-specific details.

Preserve ignored local setup and test artifacts during cleanup. Keep historical
backups local and outside public Git refs. Never publish a backup branch containing
removed private material.

Before committing, run `python3 ops/check_public_repo.py --working-tree` and inspect
the staged filenames. New public documentation must be added to the curated docs
allowlist and ignore exceptions. Use `docs/README.md` as the documentation index.
