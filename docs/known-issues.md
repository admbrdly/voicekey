# Known issues

## Headed Playwright MCP work steals compositor focus

**Status:** open on the Playwright side (observed 2026-09-04). Following
the 2026-09-05 audit, voicekey no longer treats a new activation in the same
window as proof of the original field. It preserves/copies the final transcript
and leaves potentially retained provisional text alone. Emacs uses an
acknowledged buffer pin and an expiring insertion operation. See
[audit-2026-09-05.md](audit-2026-09-05.md) and
[persistent-mode-architecture.md](persistent-mode-architecture.md).

Background LLM tasks using Playwright MCP regularly move compositor focus to
the headed browser even though the task does not need keyboard focus. This is
itself undesirable: automation should be able to manipulate a visible,
interactive browser without disrupting the application the user is currently
using. Headless mode is not an acceptable workaround because the user still
needs to log in and manipulate the browser manually.

One visible consequence is damaged Voicekey dictation when focus is stolen
between key-down and key-up. Applications handle the resulting deactivated
preedit differently: some appear to commit the provisional transcript, while
others discard it entirely. The final transcript then cannot reliably replace
the provisional text in its original field.

Preferred fix: stop Playwright MCP (or its headed-browser integration) from
requesting compositor focus during background operations while leaving the
browser visible and manually usable. Investigate whether the focus request
comes from browser creation, page or popup creation, or an explicit
`bringToFront`-style operation, and whether Playwright MCP can reuse an
existing visible browser without activating its windows. A compositor rule
that declines activation from this browser/profile may be a fallback.

Voicekey's defensive behavior is now implemented: an acknowledged Emacs buffer
pin survives compositor focus changes; generic delivery stays bound to its
original Wayland activation. A later activation cannot prove field identity,
so generic final text is preserved/copied instead of automatically rebound.
Emacs still uses shared Wayland previews, which can deactivate independently
of its pinned final insertion. Preventing the original focus theft remains
the outstanding Playwright/compositor work.
