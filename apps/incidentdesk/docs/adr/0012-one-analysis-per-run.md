---
status: accepted
date: 2026-10-23
supersedes-in-part: 0005 (bullet "Agreement as an abstain signal"), 0008 (two calls per view)
---

# One analysis per officer Run; consistency is measured across repeated benchmark Runs

An officer Run sends each view to Gemini **once** and produces one Report. The analysis sees the whole clip at normal detail plus a high-detail window around the contact (and a zoomed crop for small, distant crashes) in the **same request**. The window is chosen by a fixed rule before the call: the middle of the clip, because officer uploads are usually trimmed around the crash (owner ruling 2026-10-23; local crash finders were measured and parked). If the model's contact time falls outside the window it was sent, a **second call** examines the right moment (the safety net).

Agreement is no longer a step inside a Run. It is a **benchmark measure**: the same clip is run 3 times as separate Runs, one analysis each, and the benchmark reports how often they reach the same finding.

**Why:**
- The officer and professor get one Report from one upload; three analyses per Run tripled cost and latency for a vote that did not work.
- The in-Run vote was keyed by free-text party names. It blocked correct results (night1: the same car named three ways) and let single-analysis decisions stand under a false "2 of 3 agree" label, because a party missing from two analyses counted those two absences as agreement (2026-10-18 paid gate: MODERATE_04 r1, MODERATE_05 r2).
- A closed checklist (P-codes, typed colour and type, split movement) plus code-side consistency checks gives the abstain signal the vote was meant to give: contradictory or `unknown` answers become Cannot Determine with a reason.
- Owner decision, spec 003 decisions 1, 2 and 11 (grill 2026-10-18; window rule updated 2026-10-23).

## Consequences
- ADR 0005's "3/3 trusted, 2/3 flagged" abstain signal no longer applies to officer Reports. Abstention now comes from `unknown` answers and consistency checks; the benchmark still grades abstention as its own outcome.
- ADR 0008's two passes per view become one request with a high-detail window, plus the fallback second call. Frame rates, window length and crop rules stay project assumptions to validate (ADR 0006).
- The in-Run vote code may remain for research only; wherever it remains, a missing item never counts as agreement.
- Consistency across Runs needs a party identity that does not depend on wording: P-codes, colour and type, then movement and role; two parties that still cannot be told apart count as a failure, never a guess.
- Stop condition: if the single request is less accurate than the two-call path on the free-key probe, keep two calls and record why.
