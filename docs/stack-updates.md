# Stack pin updates

Project stack changes are explicit and dry-run first. The command resolves a local immutable version tag, verifies that the tagged runtime declares the same semantic version, checks contract compatibility, and shows the proposed project changes without writing:

```bash
./go stack update --to v0.3.3 --json
```

Apply only after reviewing that plan:

```bash
./go stack update --to v0.3.3 --apply --json
```

An applied update atomically replaces `.go/project.json` and writes `.go/updates/<update>.json` first. That record contains the before and after project objects, the resolved stack commit, and rollback status. Missing tags, moving branch names, tag/version mismatches, and runtimes older than the project's contract version fail before project state changes.

The command resolves tags from the local stack checkout by default. Fetch the intended release tag into that checkout first, or pass a different trusted checkout with `--stack-repo`.
