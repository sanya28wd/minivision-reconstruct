---
status: accepted
date: 2026-10-11
---

# Judge each view independently, then compare. Never fuse views into one judgement

All views uploaded in one report session are assumed to show the same incident. Each view is analysed on its own: only what that view shows. The per-view findings are then combined into one report.

Wherever two views disagree about the same fact, the report shows it as a Contradiction for the officer, not a silent merge.

We chose this over analysing all views together in one pass for three reasons:
- A joint pass lets one view's content leak into claims about another.
- It hides which camera a finding came from.
- It has no way to show disagreement.

Every finding cites its view and timestamp.

## Consequences
- **Same-incident assumption.** Holds until real multi-source footage shows otherwise; then a "views may not match" check can be added.
- **Synthetic test pairs.** Interior and exterior clips paired from different research datasets test this path. Their results are reported as "workflow tested", never as fusion accuracy.
- **Time alignment.** Views are not assumed to be synchronised. Comparing findings across views tolerates clock offsets.
