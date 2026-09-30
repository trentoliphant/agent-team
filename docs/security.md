# Authentication and execution boundaries

Agent Team runs trusted projects on the operator's machine. Separate clones
prevent accidental shared working-tree edits; they are **not** containers or a
complete operating-system security boundary.

## Subscription access

The coordinator checks `codex login status` for ChatGPT login and
`claude auth status --json` for first-party subscription login before each call.
It filters environment variables using an allowlist, excluding API keys,
GitHub tokens, cloud-provider switches, and API base URL overrides. Codex uses
the OpenAI provider and forced ChatGPT login. The coordinator never copies
authentication files and never switches to API billing.

Claude uses safe/restricted mode, explicit tools, no persistent session, and an
empty MCP configuration. It intentionally does not use `--bare`, which requires
API/provider credentials instead of the subscription keychain. Claude authors
get file read/edit tools; reviewers get read tools only. The coordinator runs
validation commands for both families so Claude needs no shell permission.
Codex authors use workspace-write sandboxing, reviewers use read-only sandboxing,
and neither uses approval bypass. The installed CLI versions and policies still
determine their exact behavior; unsupported flags fail rather than being removed
automatically. The initial adapters were exercised with Codex 0.157.1 and Claude
Code 2.1.283.

Authentication status checks cannot measure remaining subscription capacity or
override account entitlements. Quota failures queue a bounded later retry and set a shared cooldown for that
agent family in the local state directory. Calls within a family are serialized;
other families and non-agent stages can continue. A personal
subscription is not a guarantee of unlimited unattended throughput. Provider
policies, administrative configuration, and CLI interfaces may change.

Official references:

- [Codex authentication](https://developers.openai.com/codex/auth)
- [Codex non-interactive execution](https://developers.openai.com/codex/noninteractive)
- [Claude programmatic execution](https://code.claude.com/docs/en/headless)
- [Claude CLI options](https://code.claude.com/docs/en/cli-reference)

## GitHub and local execution

The coordinator uses `gh`'s existing identity or a dedicated GitHub App token
provided to the coordinator process. GitHub credentials are not forwarded in the
worker environment. Git push destinations are constructed from the registered
repository. Coordinator Git calls ignore global/system configuration and disable
filesystem monitors and hooks. Local config, excludes, and attributes are
fingerprinted before workers run; changes block later coordinator Git operations.
Worker Git configuration does not inherit global credential helpers. An empty
GitHub CLI configuration directory discourages accidental worker authentication.
These measures do not make credentials on the same OS account inaccessible to
arbitrary code. Use a dedicated OS account or VM for stronger isolation.

Only register repositories you trust. Validation commands and project code execute
in fresh candidate clones with the operator's filesystem access. Issue text is untrusted input, and
prompts tell workers it cannot expand permissions. Prompts are not a security
boundary. Issue approval records a content fingerprint, and subsequent edits
invalidate approval. Protect the coordinator's GitHub identity; use repository rulesets and a
separate automation identity to protect branches. Do not grant that identity
branch-rule bypass or administration privileges.

The software has no merge operation. To enforce human-only merging independently
of the software, configure GitHub permissions as described in
[GitHub setup](github.md). Default personal `gh` authentication makes initial
setup convenient but does not separate the operator from automation at GitHub's
permission layer.

Local prompts, logs, and checkouts may contain repository data. The coordinator
uses a private file-creation mask and keeps raw logs out of GitHub comments. Review
generated summaries before enabling automation on sensitive projects. State and
artifacts must never be committed to this public repository.
