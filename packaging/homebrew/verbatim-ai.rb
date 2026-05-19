# Homebrew formula for verbatim-ai.
#
# To install once this is in a tap (see TAP_SETUP.md):
#   brew tap qatcod/verbatim
#   brew install verbatim-ai
#
# To install ad-hoc from this file in a checkout:
#   brew install --build-from-source ./packaging/homebrew/verbatim-ai.rb
#
# When cutting a new release, bump `version`, update `url`, and update `sha256`
# (the sdist SHA from PyPI). The dependency SHAs change rarely — bump if any
# dependency's version pin in pyproject.toml moves.

class VerbatimAi < Formula
  include Language::Python::Virtualenv

  desc "AI memory layer for engineering teams"
  homepage "https://github.com/qatcod/verbatim"
  url "https://files.pythonhosted.org/packages/source/v/verbatim-ai/verbatim_ai-0.10.0.tar.gz"
  # sha256 must be updated alongside `url` on every release. Get it via:
  #   curl -sL https://pypi.org/pypi/verbatim-ai/0.10.0/json \
  #     | jq -r '.urls[] | select(.packagetype=="sdist") | .digests.sha256'
  sha256 "REPLACE_WITH_SDIST_SHA256_FROM_PYPI"
  license "MIT"

  depends_on "python@3.12"

  # Runtime dependencies. Homebrew resolves these into the formula's isolated
  # virtualenv via `virtualenv_install_with_resources`. To regenerate this list
  # automatically (recommended whenever pyproject.toml deps change), run:
  #   brew install pipgrip
  #   pipgrip --pypi verbatim-ai --tree
  # then map each line into a `resource` block.
  resource "anthropic" do
    url "https://files.pythonhosted.org/packages/source/a/anthropic/anthropic-0.40.0.tar.gz"
    sha256 "REPLACE_ME"
  end

  resource "typer" do
    url "https://files.pythonhosted.org/packages/source/t/typer/typer-0.12.0.tar.gz"
    sha256 "REPLACE_ME"
  end

  resource "pydantic" do
    url "https://files.pythonhosted.org/packages/source/p/pydantic/pydantic-2.6.0.tar.gz"
    sha256 "REPLACE_ME"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/source/r/rich/rich-13.0.0.tar.gz"
    sha256 "REPLACE_ME"
  end

  resource "mcp" do
    url "https://files.pythonhosted.org/packages/source/m/mcp/mcp-1.0.0.tar.gz"
    sha256 "REPLACE_ME"
  end

  resource "slack-sdk" do
    url "https://files.pythonhosted.org/packages/source/s/slack-sdk/slack_sdk-3.27.0.tar.gz"
    sha256 "REPLACE_ME"
  end

  resource "websocket-client" do
    url "https://files.pythonhosted.org/packages/source/w/websocket-client/websocket-client-1.6.0.tar.gz"
    sha256 "REPLACE_ME"
  end

  resource "httpx" do
    url "https://files.pythonhosted.org/packages/source/h/httpx/httpx-0.27.0.tar.gz"
    sha256 "REPLACE_ME"
  end

  resource "rapidfuzz" do
    url "https://files.pythonhosted.org/packages/source/r/rapidfuzz/rapidfuzz-3.5.0.tar.gz"
    sha256 "REPLACE_ME"
  end

  resource "starlette" do
    url "https://files.pythonhosted.org/packages/source/s/starlette/starlette-0.36.0.tar.gz"
    sha256 "REPLACE_ME"
  end

  resource "uvicorn" do
    url "https://files.pythonhosted.org/packages/source/u/uvicorn/uvicorn-0.27.0.tar.gz"
    sha256 "REPLACE_ME"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match(/verbatim/i, shell_output("#{bin}/verbatim version"))
    assert_match(/extract|ingest|query/i, shell_output("#{bin}/verbatim --help"))
  end
end
