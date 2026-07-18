# Stack pin updates

Project stack changes are explicit and dry-run first. The command resolves a local immutable version tag, verifies that the tagged runtime declares the same semantic version, checks contract compatibility, and shows the proposed project changes without writing:

```bash
./go stack update --to v0.3.5 --json
```

Apply only after reviewing that plan:

```bash
./go stack update --to v0.3.5 --apply --json
```

An applied update atomically replaces `.go/project.json` and writes `.go/updates/<update>.json` first. That record contains the before and after project objects, the resolved stack commit, and rollback status. Missing tags, moving branch names, tag/version mismatches, and runtimes older than the project's contract version fail before project state changes.

The command resolves tags from the local stack checkout by default. Fetch the intended release tag into that checkout first, or pass a different trusted checkout with `--stack-repo`.

`go-workflow doctor` verifies source checkouts against the local annotated tag. For a standalone VCS package installation, it instead requires PEP 610 `direct_url.json` metadata bound to the executing package root and recording Git, the authenticated official repository URL, the exact `v<package-version>` ref, and its resolved 40-character commit. Missing, malformed, archive-based, plaintext-HTTP, unrelated, or mismatched provenance remains unverified; `GO_STACK_ALLOW_DEV=1` is still the only explicit development escape hatch.
