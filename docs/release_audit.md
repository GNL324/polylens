# Release Audit

## Summary

Polylens has a substantial working feature set and a strong test suite. The repository is close to public-release shape, but it still needs final decisions on licensing, packaging metadata, and public data hygiene.

## Missing Documentation

- A public README was needed and has been added.
- Getting started, architecture, roadmap, security, and examples docs were needed and have been added.
- Existing feature docs are useful but should be reviewed for consistency before release.

## Missing Metadata

Recommended before public release:

- Choose and add a `LICENSE` file.
- Add `pyproject.toml` or package metadata if Polylens should be installable.
- Add pinned or generated dependency metadata if `requirements.txt` is incomplete.
- Add repository topics and description on GitHub.

## CI/CD

A GitHub Actions pytest workflow has been added in `.github/workflows/tests.yml`.

## Developer Onboarding

`CONTRIBUTING.md`, setup docs, and test commands have been added. Future improvement: add a small fixture-based local demo requiring no external API key.

## Security Concerns

- Runtime env file `deploy/systemd/polylens-live-arb.env` is untracked and should remain untracked.
- Raw API payloads, logs, reports, and SQLite databases should not be committed.
- The repository should include `.gitignore` coverage for runtime data if not already present.
- Secret scan findings are documented in `docs/security_audit.md`.

## Public Release Blockers

- License not selected.
- Confirm dependency files are complete.
- Confirm no runtime databases, raw API dumps, or logs are tracked.
- Confirm screenshots/examples are sanitized.
- Add release notes and tag only after CI passes.

## Recommendations

1. Pick a license and add `LICENSE`.
2. Add/verify `.gitignore` for data, logs, env files, and SQLite files.
3. Run CI on a clean clone.
4. Review docs for claims and legal disclaimers.
5. Publish an initial `v0.1.0` release after a final secret scan.
