# Homebrew tap setup for verbatim-ai

To make `brew install verbatim-ai` work, the `verbatim-ai.rb` formula has
to live in a Homebrew **tap repository** — a GitHub repo named
`homebrew-<something>` containing a `Formula/` directory.

## Option A — personal tap (recommended for v0.10.0)

1. Create a new GitHub repo named `qatcod/homebrew-verbatim` (note the
   `homebrew-` prefix — this is what makes it a tap).
2. Add a `Formula/` directory at the repo root.
3. Copy `packaging/homebrew/verbatim-ai.rb` into `Formula/verbatim-ai.rb`
   in the tap repo.
4. Users install with:

   ```bash
   brew tap qatcod/verbatim
   brew install verbatim-ai
   ```

After step 4, future formula updates are pulled with `brew update && brew
upgrade verbatim-ai`.

## Option B — Homebrew core

For inclusion in `homebrew-core` (so users can `brew install verbatim-ai`
with no tap step), the project has to meet [Acceptable Formulae][af]:
3 month track record, > 30 GitHub stars, > 30 forks-or-watchers, and an
audited dependency tree. v0.10.0 is too early — revisit once the project
has some adoption.

[af]: https://docs.brew.sh/Acceptable-Formulae

## Filling in the SHAs

The formula ships with `REPLACE_ME` placeholders for the resource sha256s.
For a clean release flow:

```bash
# Get the sdist sha for verbatim-ai itself
curl -sL https://pypi.org/pypi/verbatim-ai/0.10.0/json \
  | jq -r '.urls[] | select(.packagetype=="sdist") | .digests.sha256'

# Auto-generate resource blocks for all transitive deps
pip install pipgrip homebrew-pypi-poet
poet -f verbatim-ai > /tmp/formula.rb
```

`homebrew-pypi-poet` outputs the full `resource "name" do ... end` blocks
with correct URLs and SHAs — paste them in over the `REPLACE_ME` stubs.

## Why aren't the SHAs filled in already?

The first release tagged `v0.10.0` is what generates the sdist on PyPI;
the SHA isn't knowable until that tag is published and the GitHub Actions
release workflow uploads to PyPI. The dependency SHAs change with each
dependency version bump, so they're cheapest to regenerate via poet at
formula-publish time rather than hand-maintain.
