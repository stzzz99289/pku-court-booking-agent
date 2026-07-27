# AGENTS.md

This repository was originally developed with Claude Code. Before making
changes, read and follow [`CLAUDE.md`](CLAUDE.md); it is the detailed,
authoritative project guide for architecture, configuration, workflows, and
operational behavior. Keep that file and this one aligned when project-wide
guidance changes.

## Codex Working Notes

- This project automates the live Peking University court reservation site
  with Playwright. Treat booking runs as real external actions: use the
  requested config, do not expose credentials or captcha service secrets, and
  clearly report whether a reservation was actually confirmed.
- Keep shared production selectors in `src/booking/site_constants.py`.
  Environment-specific or one-off selector overrides may live in the
  appropriate `config/**/site_config.yaml`; never put credentials in tracked
  files.
- Preserve the two-file deep-merge configuration model and the distinction
  between CLI configs and webapp split configs described in `CLAUDE.md`.
- Keep `main.py` and the CLI flow readable. Reuse the booking modules from
  `src/booking/` when adding web or scheduling behavior.
- The persistent profiles under `.browser_profile/`, local configs, `data/`,
  and `debugging/` may contain sensitive or run-specific state and must remain
  uncommitted.
- Validate changes with the narrowest relevant command first. For a live
  booking smoke test, use:

  ```bash
  python main.py -c config/cli/user_config.yaml \
    --site-config config/cli/site_config.yaml
  ```

- Follow the existing commit convention: a single-line subject under roughly
  70 characters with a tag such as `[feature]`, `[fix]`, or `[chore]`.
