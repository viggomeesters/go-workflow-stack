# Go Workflow Stack

![Go Workflow Stack hero](assets/hero.svg)

Reusable tooling for repo-local agentic engineering.

The stack contains schemas, validators, fixtures, and a small CLI for projects that keep their own `.go/` JSON/JSONL state next to the code.

## Why this exists

Agent work should be clone-readable. A future agent should be able to inspect a repository and understand its project state without needing a central vault task database.

## Repository roles

- **This repo (`go-workflow-stack`)**: reusable workflow tooling.
- **Template repo ([`go-project-template`](https://github.com/viggomeesters/go-project-template))**: starter `.go/` project-state structure.
- **Project repos**: own their `.go/` state and evidence.

For the full practical architecture and application flow, see [`docs/practical-architecture.md`](docs/practical-architecture.md).

## Practical architecture in one minute

Use this stack when you want reusable commands and validation. Use [`go-project-template`](https://github.com/viggomeesters/go-project-template) when you want to start a new repo that already carries its own `.go/` state. A real project should copy/adapt the template and then keep tasks, evidence, decisions, and architecture principles in its own repository.

```text
go-workflow-stack  -> validates/operates -> project repo with .go/
go-project-template -> seeds/copies ------^
```

## Install / usage

Clone this repository next to a project repository:

```bash
git clone https://github.com/viggomeesters/go-workflow-stack.git
git clone https://github.com/viggomeesters/go-project-template.git
cd go-workflow-stack
make check
```

Run the CLI against a repo-local `.go/` project:

```bash
python3 cli/go.py validate ../go-project-template
python3 cli/go.py readback ../go-project-template
python3 cli/go.py next ../go-project-template
```

Initialize a fresh repo with the current minimal fixture:

```bash
python3 cli/go.py init ../my-project --force
```

## CLI commands

- `init <repo>`: create a minimal `.go/` fixture.
- `validate <repo>`: validate `.go/` JSON and JSONL files.
- `next <repo>`: show the first open task.
- `claim <task-id> --repo <repo> --agent <name>`: move an open task to active.
- `finish <task-id> --repo <repo> --agent <name> --evidence <text>`: move an active task to done and append evidence.
- `dirty-check <repo>`: classify dirty Git state against owned paths.
- `readback <repo>`: summarize the project from `.go/` only.

## Contract

```text
.go/
  project.json
  architecture-principles.json
  vision.json
  hierarchy.json
  tasks/open/*.json
  tasks/active/*.json
  tasks/done/*.json
  tasks/blocked/*.json
  runs/*.jsonl
  evidence/*.jsonl
  decisions/*.jsonl
  locks/
```

JSON is canonical for current state. JSONL is canonical for lifecycle, evidence, and decision streams. Markdown is a human view only.

## Development

Use local validation before committing or publishing changes. The check compiles the Python CLI where applicable and validates the template repository contract.

```bash
make check
bash scripts/check.sh
```

## Privacy and security

The repository should contain only synthetic public fixtures. Do not commit private vault data, credentials, customer data, local runtime DBs, or generated machine state.

## Status

Early public spike. The goal is to prove the repo-local contract before broad migration.

## License

This project is released under the MIT License. See [`LICENSE`](LICENSE) for the full license text.
