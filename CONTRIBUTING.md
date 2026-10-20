# Contributing

Thanks for helping. This project is alpha; small, focused changes with tests are
the easiest to review.

## Setup

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), and Node 24 if you
touch `clients/typescript` or `studio-web`.

```bash
git clone https://github.com/Liceron/nexura && cd nexura
uv sync --extra studio          # Python SDK, service and dev tools
uv run python examples/oss/o07.py   # quick end-to-end smoke check
```

No API keys are needed for anything in the test suite. Never add tests or CI steps
that require paid provider credentials; use `FakeModel`, `StreamingFakeProvider` or
`httpx.MockTransport` instead.

## Test profiles

Run the profile that matches what you changed. CI runs every profile except the
gVisor sandbox (it needs a privileged runtime) and the local release gate.

| Profile | Command (from repo root unless noted) |
|---|---|
| Python tests | `uv run pytest --deselect tests/backend/b22` |
| Python lint / format | `uv run ruff check src tests examples` and `uv run ruff format --check <changed files>` |
| Python types | `uv run mypy src packages/*/src` |
| Licence headers (new source files need the MIT SPDX header) | `uv run python scripts/add_spdx_headers.py --check`; run it without `--check` to add missing headers |
| Vector stores (needs Postgres+pgvector and Qdrant) | set `NEXURA_TEST_POSTGRES_URI` and `NEXURA_TEST_QDRANT_URL`, then `uv run pytest tests/backend/b22 tests/postgres` |
| Sandbox isolation (needs gVisor `runsc` and passwordless sudo) | `NEXURA_REAL_GVISOR=1 uv run pytest tests/backend/b30` (the test helper `tests/backend/b30/conftest.py` reads `runsc_path`, `rootfs_path` and `runsc_command_prefix` from `.agy/review-infrastructure.json` at the repo root and passes them in an explicit operator `SandboxConfig`; the SDK itself never reads that file) |
| Local release gate (all profiles, writes receipts) | `uv run python experiments/release-gate/run_gate.py` |
| TypeScript client | `cd clients/typescript && npm ci && npm test && npm run build` |
| Studio web unit | `cd studio-web && npm ci && npm run typecheck && npm run lint && npm test && npm run build` |
| Studio browser | `cd studio-web && npx playwright install chromium && npm run test:e2e` |
| Package | `uv build`, then install the wheel into a fresh Python 3.12 venv |

The browser tests start their own local service and Vite server on random ports and
stop them afterwards. The vector-store tests are excluded by default because they
need running databases; a skipped required profile means that scope is unverified.

## Style

- Python: ruff (line length 100), strict mypy, type hints on public functions.
- Match the surrounding code; avoid drive-by reformatting of files you didn't change.
- Errors should be specific and actionable. Unknown usage, pricing or outcomes stay
  unknown; never default them to zero or success.
- Workflow JSON must never carry executable code; behavior lives in registered handlers.

## Contributor License Agreement

Contributions require signing the Liceron Contributor License Agreement (CLA) before
they can be merged. The agreement is in [CLA.md](CLA.md). In short, you keep the
copyright in your work and grant Liceron a perpetual, worldwide, royalty-free licence
to use it, including the right to relicense it, plus a patent licence for your
contribution. Read the full text; this summary is not legal advice.

The CLA text is currently a **draft that has not been reviewed by a lawyer**. Until the
reviewed version is published, pull requests from outside contributors cannot be merged.

How to sign:

1. **Planned:** a CLA check will run on every pull request. It will ask you to sign once,
   through a link in the pull request, and will then pass for your later pull requests.
2. **Until that check exists:** fill in the signature block at the end of `CLA.md` and
   email it to info@liceron.in with the subject "Nexura CLA", from the address you use
   for your commits. A maintainer will confirm by reply, and will record that you have
   signed before merging your first pull request.

If you contribute on behalf of your employer, ask at the same address first; an entity
agreement does not exist yet.

## Pull requests

- Sign the CLA first (see above).
- One logical change per PR, with tests that fail without it.
- Describe what you verified and how (commands and results), and what you did not.
- Update `CHANGELOG.md` under **Unreleased** for user-visible changes.
- Do not add publishing, deployment or secret-dependent steps to CI.

## Extending the SDK

Custom handlers, modules and providers do not need Studio; see
[Extending](docs/extensions.md#extending). A new module should declare its optional extra in `ModuleManifest` so a
missing dependency is reported instead of failing at run time.

## Support and deprecation policy

- Supported: the latest `0.x` minor release on Python 3.12 and Node 24 on Linux.
- Until 1.0, minor releases may change APIs. Breaking changes are listed in
  `CHANGELOG.md`; where practical the old name keeps working for one minor release
  with a `DeprecationWarning` before removal.
- Workflow `spec_version` 1 is the only supported spec version. Older formats are
  migrated explicitly with `WorkflowMigrator`; unknown versions are rejected.

## Conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
