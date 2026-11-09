# MIG-04B display-only interior driver timeline

Interior attachments refuse before any reconstruction-import write. The exact inspected
snapshot is validated then persisted without rereading files. Interior views never show
the road reconstruction cross-check. No producer change, dependency, table or migration.

Both flags are required: `INCIDENTDESK_RECONSTRUCTION=true` and
`INCIDENTDESK_DRIVER_CONTEXT=true`; both default off. The original Driver observations
assessment and raw driver timeline remain independent of these flags.

Every interior observation retains native start/end seconds, subject and view metadata.
The existing saved driver assessment/reason is reused. Aligned context is expressed on
each road view's native clock: direct offset, negated reverse offset, or difference of
two offsets to a common reference. Shared uncertainty is the sum of the stated bounds;
both reviewers are shown. Latest alignments only, unknown excluded, no inferred chains.
No alignment means native panel only with an explicit no-timing-relation message.

Video contact interval endpoints, typed contact timestamps (including overturns), and
non-rejected reconstruction candidate peaks are separately labelled. Obscured video
intervals keep their qualifier. Following Round 2, candidate peaks needing review,
quarantined or with unresolved order are excluded from driver timing; rejected or unknown
corrected times cannot provide numeric context. Signed differences are aligned observation start/end
minus each cited road contact time. For uncertainty u, definite temporal overlap requires
start+u ≤ t ≤ end−u; possible overlap requires start−u ≤ t ≤ end+u. No new window,
metric inference, causation or classification is introduced.

Fault, fines, driver rules, summary, storyline, Q&A and print inputs are unchanged.
New context is print-excluded. Tests use labelled synthetic saved facts and unchanged
MIG-02 exports, with native citations, hostile text, offline rendering and all flag
combinations. Full `/print` response bytes are compared; browser PDF bytes are not
deterministic and no PDF-byte guarantee is claimed. Independent Claude browser/PDF
smoke remains required: `integration/smoke/MIG-04B-INTERIOR-TIMELINE.md`.

## Tell the collaborator

No interior capability or export change is requested from reconstruction. Road exports
remain proposals. Reviewer alignment is a display aid and never enters driver/fault rules.

## Round 2 — legacy links and validation coverage

Pre-existing interior imports are retained in immutable history but ignored before any
road comparison, event, tool, mapping or combined-timeline projection. A readable notice
explains the ignored export; native video observations remain. The flag-matrix invariant
test now exercises both unaligned and reviewer-aligned actual saved road exports.
