# Verbatim logomark & identity

_Started 2026-05-18 23:46 UTC_

---

## User

Product: Verbatim is the AI memory layer for engineering teams. It ingests meetings, Slack threads, and PR discussions, extracts structured commitments/decisions/questions/blockers, and surfaces them everywhere a team works — CLI, web UI,
  Slack bot, email, MCP for AI agents.

  The contract behind the name: every extracted entity carries the exact verbatim quote from the source that supports it. No paraphrase, no hallucination — the quote is the evidence.

  Current logomark (inline SVG above): Two pairs of pill-shaped marks tilted -12°. The lower-left pair reads as an opening curly quote "; the upper-right pair as a closing curly quote ". The empty space between them is the captured
  statement. The product's contract, in one glyph.
  
  Color tokens:
  - Primary accent: #a78bfa (violet 400)
  - Accent depth: #8b5cf6 (violet 500)
  - Surface: #131316
  - Background: #0a0a0c
  - Text primary: #f4f4f5
  - Text secondary: #c4c4c9
  
  Typography: Inter for UI text, system-ui fallback. Lowercase wordmark ("verbatim") for the dev-tool aesthetic, like Linear/Stripe/Vercel.

## Assistant

_[tool: questions_v2]_

I have the brief — Verbatim, AI memory layer for engineering teams, the verbatim-quote contract, violet accent on near-black, lowercase Inter wordmark. Got it.

A couple of things before I start:

1. You mentioned "inline SVG above" for the current logomark — I don't see an attachment in this message. Could you paste it, or describe the proportions/spacing more precisely so I can match (or intentionally evolve) it?
2. **What would you like me to design?** The brief reads like a setup, but the ask is open. A few possibilities:

## User

Questions answered:
- deliverable: Web app UI (the dashboard where commitments/decisions/questions/blockers live)
- audience: Mixed — needs to speak to all of them
- tone: Linear-style: dense, fast, mostly monochrome, tiny pops of violet
- verbatim_hero_idea: It's the hero — every visual references the quote contract somehow
- surfaces_to_show: Web UI / dashboard, Slack bot
- variations: Just one — commit to a direction
- tweaks: Dark / light mode

## Assistant

_[tool: list_files]_

_[tool: update_todos]_

_[tool: write_file]_

_[tool: update_todos]_

Design system project is empty — I'll commit to the tokens in your brief and build a tight, Linear-flavored system around them.

Quick design decisions before I build:

**Layout:** Three-pane shell — left nav (220px), entity list (~ 480px), detail pane (rest). Linear-style density: 13px body, 11px metadata, generous use of monochrome with violet reserved for the *quote evidence* and unread/needs-attention states.

**Verbatim-as-hero approach:** Every list row carries a truncated italic quote underneath the summary — the summary is metadata, the quote is the truth. In the detail pane, the quote becomes the page-defining element: large, violet left rail, surrounded by reconstructed source context (the actual Slack thread or meeting transcript with the quoted line highlighted in place). This is where the Slack bot mock lives — not as a separate screen, but as the *source context* that proves the extraction.

**Logomark:** I'll render your curly-quote-pair-with-captured-space mark inline as SVG in the sidebar.

Let me build.

_[tool: write_file]_

_[tool: write_file]_

_[tool: write_file]_

_[tool: done]_

_[tool: list_files]_

_[tool: show_html]_

_[tool: show_html]_

_[tool: get_webview_logs]_

_[tool: run_script]_

The two larger JSX files are failing to fetch. Let me consolidate everything into a single JSX file to dodge whatever's happening with multi-file loading.

_[tool: str_replace_edit]_

_[tool: delete_file]_

_[tool: done]_

_[tool: update_todos]_

_[tool: fork_verifier_agent]_

Built. Verbatim is open in your tab.

**What's in it:**
- **Three-pane shell** — Linear-style nav (workspace, sources, teams, integrations), entity list with type filters, detail pane.
- **Entity list** — every row shows summary + the verbatim quote underneath in a violet-railed italic. Type-dot color codes commitment/decision/question/blocker. Mono-style ID, owner avatar, due, source channel, confirmed status, unread indicator.
- **Detail pane = quote-as-hero** — extracted summary is the H1, but the *evidence card* sits right under it: violet-tinted, "verbatim" lock-tag, large italic quote, source attribution. The summary is metadata; the quote is the contract.
- **Source context** — the reconstructed Slack thread with the quoted line highlighted in place, then an inline dashed "Verbatim extracted commitment from this line → VRB-417" trace, then the **Slack bot reply** showing the extraction card with confirm/edit/reassign actions (the same one users would see in-channel).
- **Right rail** — properties, evidence (confidence bar, model, confirmation reactions), linked PRs/decisions/meetings, related verbatim quotes from elsewhere.
- **Dark/light tweak** — toggle in the Tweaks panel (toolbar). Light mode rebalances violet to #7c3aed for AA contrast.

**Caveat:** the list filter tabs and entity selection work, but the rest of the UI (nav clicks, action buttons) is non-interactive — this is a hi-fi static mock, not a working prototype.

**Possible next steps if you want them:** a CLI mock, an email digest layout, a marketing landing page, or wire selection so clicking other entities swaps in their (currently shared) Slack thread for realism.

