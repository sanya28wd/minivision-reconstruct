---
status: accepted
date: 2026-10-11
supersedes: MASTER_DESIGN_DECISIONS ARCH-011 (refines it)
---

# Benchmark truth comes from Gemini dense-frame annotation that the owner reviews and overrides

We are rebuilding the benchmark from scratch. The new clips come from public research datasets. The old 29 YouTube-era clips are moved to `dataset/archive/` and used only for spot-checks.

Writing per-second timestamps by hand for 30 or more videos is not feasible. So for each clip, Gemini (run through Antigravity as the bulk vision worker) writes a dense annotation at 7–9 fps. The owner reviews it, and any correction the owner makes overrides Gemini.

Unreviewed annotations still count as truth, but every benchmark result must say how many of its scores rest on unreviewed truth.

## Consequences
- **Circularity risk.** If the runtime also uses Gemini, an unreviewed annotation grades Gemini against itself. Owner review is the only check on this. The share of reviewed clips is therefore part of every accuracy claim.
- **Cost of 7–9 fps.** It is far above Gemini's 1 fps default, so token cost per clip is several times higher. Long clips may need lower media resolution. Check the current fps and token limits before running.
- **Old hand truth.** Earlier owner rulings (VIDEO5, video6, video7) live with the archived clips and are not carried into the new benchmark.

## Benchmark size (owner, 2026-10-12)
Start with about 25 clips and grow toward 50 or more. Grow by adding officer-disputed clips, failure cases and thin slices. Every result states its smallest detectable effect: about 25 clips only detect drops of roughly 20 points (ADR 0005).

## Amendment 2026-10-16 (owner)

The benchmark uses the 29 archived clips (MAJOR 6, MODERATE 5, MINOR 5, OLD 13 in `dataset/archive/videos/`) instead of new clips from public research datasets. The RPT-10 acquisition is dropped. This supersedes "used only for spot-checks" above.
- The RPT-13 tool re-drafts their truth with Gemini Pro at dense frame rates, and the owner reviews it. The old hand truth (`ground_truth.py`, `PURE_GEMINI_GROUNDTRUTH.md`, earlier rulings) feeds only `dataset/truth/CONFLICTS.md` and is never copied in silently.
- The clips' source and licence are not recorded (YouTube era). They are used only for private academic testing: never redistributed, never published, and never shown outside the team without the professor's agreement.
