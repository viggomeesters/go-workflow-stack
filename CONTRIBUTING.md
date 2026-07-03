# Contributing

Thanks for improving this repo.

## Local checks

Run the documented check command before opening a PR.

```bash
make check
```

## Boundaries

- Keep durable workflow state JSON/JSONL-first.
- Do not add private vault data, credentials, or machine-local runtime artifacts.
- Do not turn Markdown into the canonical workflow state.
- Keep examples synthetic and public-safe.
