# Security

## Secrets

These must never land in git:

- `.env` and `.env.*`;
- `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`;
- real SQLite/JSON data from `data/`;
- manual Yandex dumps;
- exact personal addresses and work locations.

Only `.env.example` with empty or safe placeholder values is allowed in the repo.

## Local Data

The web runtime uses local data:

- `data/route74.sqlite`
- `data/web_watches.json`

These files stay out of git.

## Before Publishing

```bash
./bin/check
git status --short --untracked-files=all
```

If the status shows `.env`, real `data/*`, or manual dump files, do not publish.

## If a Secret Leaks

1. Rotate the key on the service side.
2. Update the local `.env`.
3. Make sure the secret is not in tracked files.
4. Run `./bin/check` again.
