# Pull request

## Summary

- What changed and why:

## Change type

- [ ] Fix
- [ ] Feature
- [ ] Docs only
- [ ] Refactor or chore

## Scope

- Paths and tools touched:
- Out of scope (if near the change):

## Tests and verification

Commands run (keep the ones you ran):

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
git diff --check
```

- Result for each command:
- Evidence (diff refs, failure output):

## Docs impact

- [ ] No docs change needed
- [ ] Docs updated (`README.md`, `docs/`, or `CHANGELOG.md`)

## Security and privacy review

- [ ] No tokens, passwords, private URLs, or personal data in the diff
- [ ] No change to auth, `exec_run` gate, or endpoint access (or described above)
- [ ] Token rotation not needed (or rotation steps verified)

## Deployment impact

- [ ] No deployment impact
- [ ] Deployment impact described (configuration, reverse proxy, service file):

## Code of Conduct

- [ ] I obeyed the Code of Conduct (`CODE_OF_CONDUCT.md`)
