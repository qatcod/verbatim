# Security policy

## Supported versions

Verbatim follows semantic versioning. Security fixes land in the latest minor release; older minor lines do not receive backports.

## Reporting a vulnerability

If you find a security issue, **do not open a public GitHub issue.** Instead, contact the maintainer directly:

- Email: `zakrstech@gmail.com` (subject line: `[verbatim security]`)
- Or open a [GitHub security advisory](https://github.com/qatcod/verbatim/security/advisories/new)

Please include:

1. A description of the issue and its impact.
2. Steps to reproduce, or a minimal proof of concept.
3. The Verbatim version (`verbatim version`) and your Python version.
4. Whether you've shared this with anyone else.

You can expect:

- An acknowledgment within 72 hours.
- A status update within a week, including whether we've reproduced the issue and an estimated timeline for a fix.
- Coordinated disclosure once a fix is shipped, including credit (if you'd like it).

## Scope

In scope:

- Code execution, privilege escalation, or sandbox escape via Verbatim CLI or web UI.
- Data exfiltration from the Verbatim SQLite store or from in-flight API calls.
- Authentication bypass on the web UI (when run with bind addresses other than `127.0.0.1`).
- Supply-chain issues in pinned dependencies that materially affect Verbatim.

Out of scope:

- Issues that require physical access to the user's machine.
- Vulnerabilities in upstream dependencies that we cannot mitigate at our layer (we will still want to know about them — please report upstream).
- Self-XSS or other attacks that require the user to perform unusual actions.

## Trust model

Verbatim is designed to be self-hosted by a single user or small team. By default:

- The web UI binds to `127.0.0.1` only.
- API keys (Anthropic, Slack, GitHub, Linear) are read from environment variables; the application never writes them to disk.
- The SQLite store contains source quotes and metadata; if your transcripts contain sensitive data, treat the store as sensitive.

If you deploy Verbatim on a shared host or expose it on a network, you take on the responsibility of adding authentication, transport encryption, and access controls in front of it.
