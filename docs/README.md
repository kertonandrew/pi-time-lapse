# Public documentation

These guides describe reusable setup, configuration and supported behavior.

- [Deployment and private configuration](deployment.md)
- [Camera capture and transfer](camera-workflow.md)
- [Home Assistant](home-assistant.md)
- [Restricted SSH archive receiver](ssh-receiver.md)
- [Solar-aware transfer policy](solar-transfer-design.md)
- [Bounded battery discharge diagnostic](battery-discharge-test.md)
- [Provisional charging diagnostic](battery-charging-trial.md)
- [Hardware recorder and installation](../hardware/README.md)
- [Power measurement design](../hardware/power-measurement-plan.md)

Store device-specific test plans, iteration notes, deployment receipts, measurement
exports and review artifacts under the ignored `local/` workspace, for example
`local/docs/` and `local/reports/`. Sanitized real-device observations are still
private working material; removing names does not make them public documentation.
Publish selected results only as a deliberate, reviewed release artifact.

New public guides require an explicit entry in the root and docs ignore rules and
`PUBLIC_DOC_PATHS` in `ops/check_public_repo.py`. Include example configuration
and reproducible procedures rather than local endpoints, run logs, work-in-progress
status or claims that a particular bench result qualifies other hardware.
