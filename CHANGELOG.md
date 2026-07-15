# Changelog

## 0.3.0 - 2026-07-15

- Replace hosted automation with a local Linux/WSL verification command.
- Add immutable `stack_ref` pins and explicit template lifecycle status.
- Add a local-only release preflight that never publishes.
- Split version, migration, and adapter-protocol rules into importable modules.
- Add transactional `.go` contract migrations and the shared versioned Codex/Hermes/custom adapter protocol.

## 0.2.0 - 2026-07-14

- Added bounded autonomous build, verification, deep-critic, repair, transactional ship, and goal-audit loops.
- Added portable cross-machine resume state, Hermes-first executor configuration, WSL doctor checks, and a local Linux verification contract.
- Added project/stack version compatibility plus a live opt-in Hermes acceptance campaign.
- Hardened scope enforcement, dirty-state handling, template pairing, and project-specific template application.

## 0.1.0 - 2026-07-03

- Initial public scaffold for Go Workflow Stack.
- Added public README, MIT license, security/support/contributing docs, issue templates, and local validation.
