# MIG-04 display-only reconstruction cross-check

The review page projects comparisons from each current immutable import and the saved
OverviewFacts for the SHA-bound view. Nothing is persisted, no migration or dependency
is added, and no network/model request is made. Only the web presentation reads this
projection. INCIDENTDESK_RECONSTRUCTION remains default off. Print excludes both the
collapsed block and neutral disagreement notice.

## State mapping

| Producer field | Saved video-analysis field | Result |
| --- | --- | --- |
| Effective collision_candidate source_time/frame, with current same-view track mappings | contact_observed interval | inclusive containment: agree; outside: disagree_time with signed difference to nearest boundary |
| Same effective candidate | typed contact timestamp, when no interval | saved adjacent presentation timestamps define half-open source-frame intervals; frame equality agrees; no frame mapping cannot compare |
| No collision candidate | contact_observed / typed contact | tracker_miss, unless assessment says obscured/cannot assess |
| Any missing_inputs | saved facts retained | reconstruction_unavailable, never tracker_miss |
| Candidate and same-view mappings | no_contact_observed | model_miss |
| Rejected, quarantined, needs-review or unknown corrected time | any | cannot_compare |
| Two distinct reviewer-mapped tracks | one unambiguous matching typed contact's subject/other_party codes | unordered equality: agree/disagree_parties; missing/ambiguous codes: cannot_compare |
| Cited first/last seen and latest stop/start | motion_state_before_contact at its own timestamp | moving/moving, stopped/(stopped_in_lane or parked_at_kerb); reversing/unknown cannot_compare |
| Unresolved movement transition | any motion fact | cannot_compare; never bridge the unknown interval |
| First seen at/after its own cited contact; calibration/timestamp missing flags | same-party contact citations; recovery notes unchanged | lacks pre-contact context; never use another pair's contact |

Track associations come only from the latest mapping on the current attached import,
same view and still-valid party code. Prior associations need re-confirmation. Unknown
clocks are irrelevant to comparison: no cross-view comparison occurs. Source times
stay native and never borrow reviewer alignment offsets.

The existing importer timestamp/frame projection is reused. Modified events resolve
only through MIG-03 effective citations; no old frame substitutes for unknown corrected
time. Autoescaping stays enabled; no imported string becomes markup.

## Verification and review

New tests construct labelled synthetic saved OverviewFacts, never purported Gemini output.
All six unchanged MIG-02 exports are exercised. Normal-crossing's missing-input state is
kept separate from a negative or tracker miss. Tests cover timing, parties, motion,
context, unknown/rejected/quarantined evidence, camera isolation, current/stale mappings,
source-frame containment, socket-denied rendering, escaping and byte-identical full
print HTML with nonempty fault findings and a sourced fine row.

Native Standards and Spec reviewers found false video citation ownership, quarantine
leakage, uncertainty precedence, missing context and unknown movement barriers; red
regressions were added and fixes made. Final party-specific contact regression also
prevents unrelated earlier collisions triggering missing-context warnings. These
checks are preparation, not Claude acceptance. Gate logs and full result are in the
donor .agent_bridge/results/MIG-04-CROSS-CHECK.md. Claude browser checklist:
integration/smoke/MIG-04-CROSS-CHECK.md.

## Alternative found / limitations

There is no server PDF endpoint or byte-renderer in this app: /print returns HTML for
the browser's print/save-PDF workflow. Automated evidence is byte-identical print HTML,
not actual browser PDF-byte equality. Claude must verify the saved PDF/pages manually;
no PDF-byte claim is made. A dedicated deterministic PDF renderer would be a separate
scope/dependency decision, not a silent addition to this card.

A typed citation structure could replace the compact display-only video_times tuple
in a later refactor. This card keeps the existing native citation style and regression
tests enforce source ownership. Unknown transitions conservatively block the whole
track's motion comparison rather than invent their corrected time bounds.

## Tell the collaborator

No producer change required. Existing event, movement, source timestamp and review
exports are read as written. Reconstruction stays a proposal and a reviewer cross-check;
producer confirmation never changes Fault Status, fines, storyline, summary, Q&A or PDF.
No merge, push or tag by Codex. Independent Claude critical review remains required.

## Round 2 — reviewer-requested safeguards

H1: motion comparison now requires an observed/recovered observation at the saved
fact's source frame and no lost/interpolated observation in the inclusive transition
to fact frame span. Recovery at the fact frame does not bridge an earlier gap.
Without this coverage the row abstains with the named-party observation reason.
Tests use unchanged occluded-impact observations at 10.0 s (both moving/stopped),
10.72 s recovery and 9.28 s clear-span control. Relative interpolation is tested
with a labelled synthetic earlier stop; no producer fixture bytes are edited.

M1: an isolated fresh Python subprocess imports the entire requested fault-input
module closure and rejects reconstruction, evidence_import or web modules in
sys.modules. Three deliberate contamination cases demonstrate the guard trips.
The original text scan stays in place; no donor test or guard script changed.

M2/M3 supersede the state table above where applicable. An out-of-interval peak
with another same-view contact or assessment interval is cannot_compare, with the
additional cited time(s). Multiple distinct contacts inside one broad interval are
also ambiguous. With no interval, matching saved source frames still agree; unequal
frames become differs_no_interval, display both times and their signed delta, and
never enter the disagreement notice. No tolerance is invented.

Low fixes: unverified exports abstain with 'export provenance incomplete'; missing
inputs use existing readable labels; uncertain candidates cannot supply pre-contact
context. Party comparisons require time agreement plus an exact unique saved
contact match; the single-contact fallback is removed. Rows show the camera display
label and retain the hash as secondary metadata. The non-print Fault Status section
is tested byte-identically with a disagreement present, in addition to the full
print HTML invariant. All previous assertions are retained.

Focused Round 2 suite: 52 passed in 52.37s; final 13-check gate evidence is in the
donor result's Round 2 section. Native scoped Standards/Spec checks prepare the
handoff only. Claude independently reruns the gate and browser smoke before merge.

## Round 3 — unique contact matching and conservative extra evidence

MEDIUM-1 supersedes R2's exact-timestamp party match: once contact time agrees,
match the unique same-view typed contact within the agreed assessment interval, or
(no interval) resolving to the same saved source frame. Multiple eligible contacts
abstain with 'which contact cannot be determined'. Interval and frame tests cover
nonidentical timestamps, conflicting party pairs (both descriptions shown), and
ambiguous contacts. The prior negative test moves its contact outside the interval;
its cannot_compare assertion remains, while new positive cases cover the R3 rule.

LOW-1: when the only other typed contact has uniquely resolved codes matching the
reviewer's mapped pair (and no other assessment), the row says 'video analysis
contact times are inconsistent'. State remains cannot_compare, times preserved.
Unknown/different pair evidence retains the separate-contact ambiguity wording.

LOW-2: if one candidate agrees, another valid mapped non-agreeing candidate becomes
cannot_compare 'reconstruction reports an additional contact'. It keeps its source
citation, clears the disagreement delta, and cannot raise the notice through the
party comparison. Rejected, uncertain or unmapped candidates are not reinterpreted;
without an agreeing candidate, the original timing disagreement is preserved.

LOW-3: a frame-less lost/interpolated record in the explicit transition-to-fact time
span blocks motion comparison, inclusive of both endpoints. Unplaceable times block
conservatively; known outside-span times do not. No frame or time is fabricated.
Existing native frame checks remain. Constructed test records leave fixture bytes
unchanged. Focused suite:74 passed in71.81s; full gate evidence is in donor Round3.

Four item commits, no producer path/dependency/fault input changes. Native read-only
Standards/Spec review is preparation only; Claude owns independent critical review,
full gate rerun and browser smoke before merge. No merge/push/tag by Codex.
