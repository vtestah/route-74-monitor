# Local Workflow

## First Run

```bash
./bin/onboard
```

The script creates `.venv`, installs the package in editable mode, and creates
`.env` from `.env.example` if it does not exist yet.

## Changing Code

1. Find the relevant layer in the Architecture section of the root README.
2. Make a small change.
3. Run:

```bash
./bin/check
```

4. If the Yandex integration changed:

```bash
./bin/smoke-yandex
```

## Before GitHub

- Do not commit `.env`.
- Do not commit `data/*.sqlite`.
- Run `./bin/check`.
- Fill in the pull request template: checks, the risk section, and any remaining
  dirty scope.
- If needed, make a local commit first, then push or open a PR.
