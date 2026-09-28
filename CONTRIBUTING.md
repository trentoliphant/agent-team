# Contributing

Discuss substantial changes in a GitHub issue. Keep fixes focused and link the
issue from the PR. Describe behavior, validation, and material limitations.

Runtime code uses Python 3.11+ and no third-party Python packages. Run:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/check_boundary.py
```

Ordinary tests must not call model providers or GitHub. Use temporary local Git
repositories and fake provider responses. Verify external CLI changes against the
official documentation and installed CLI help. Subscription logins, tokens, and
run artifacts belong outside this repository.

Agent-authored PRs must identify the actual model family. Reviewer independence
requires another family and a fresh session. The maintainer is the final merge
authority. Source changes to orchestration policy deserve particular scrutiny.
