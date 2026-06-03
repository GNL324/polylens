# Contributing

Thanks for your interest in Polylens.

## Local Setup

```bash
git clone <repo-url>
cd polylens
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

## Development Guidelines

- Keep arbitrage logic covered by focused tests.
- Do not commit secrets, runtime databases, raw API payloads, reports, or logs.
- Prefer small, well-scoped pull requests.
- Document new CLI commands and user-visible behavior.

## Testing

```bash
pytest
```

Add tests for bug fixes and new features.

## Pull Request Process

1. Open an issue or describe the motivation clearly in the PR.
2. Include tests and docs for user-facing changes.
3. Run `pytest` before submitting.
4. Keep unrelated refactors out of feature PRs.
