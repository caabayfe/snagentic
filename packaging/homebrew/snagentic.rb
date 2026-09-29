# Homebrew cask template for the native snagentic bundle.
# Do not edit tokens by hand: `packaging/homebrew/render_cask.py` fills them from the
# release's SHA256SUMS and the release workflow publishes the result to the tap.
# The cask has no install scripts: Homebrew 7 sandboxes them away from ~/.copilot, so
# users run `snagentic copilot install` once themselves (see caveats).
cask "snagentic" do
{{ARCH}}
  version "{{VERSION}}"
{{SHA256}}
  url "{{URL}}"
  name "snagentic"
  desc "ServiceNow metadata tooling with GitHub Copilot CLI agents and tools"
  homepage "{{HOMEPAGE}}"
{{LIVECHECK}}
{{DEPENDS_ON}}

  binary "snagentic/snagentic"

  zap trash: [
    "~/.copilot/extensions/snagentic",
    "~/.copilot/snagentic-marketplace",
    "~/Library/Application Support/snagentic",
    "~/Library/Caches/snagentic",
  ]

  caveats <<~EOS
    Set up the GitHub Copilot CLI extension and the ServiceNow agents, skills and
    apply gate (run again after every upgrade):
      snagentic copilot install

    Store ServiceNow credentials in the macOS Keychain:
      snagentic auth login -i <instance>
    Install the Playwright browser used by UI recipes:
      snagentic ui install

    Before `brew uninstall`, remove the Copilot integration:
      snagentic copilot uninstall
  EOS
end
