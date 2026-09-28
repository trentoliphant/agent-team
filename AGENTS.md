# Agent Team contributor instructions

This repository is self-contained. Never depend on sibling checkouts, private
notes, ignored artifacts, copied credentials, or machine-specific paths.

Use Python 3.11+ and the standard library for runtime code. The official Codex,
Claude Code, Git, and GitHub CLIs are external prerequisites, not bundled code.

Preserve subscription-only execution, family separation, exact-commit review,
bounded retries, and the absence of PR merge operations. Never weaken these controls
to make a smoke test pass. No model calls in ordinary tests or CI. Live adapter
smokes are opt-in and consume subscription capacity.

Run `python3 -m unittest discover -s tests -v` and
`python3 scripts/check_boundary.py` before publishing changes. Add regression
coverage for changes to the state machine, adapters, and GitHub writes.
