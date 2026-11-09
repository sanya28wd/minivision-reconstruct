# MIG-04B interior timeline — independent Claude browser smoke

Use disposable local development data only. No model/network request, genuine footage,
producer edits, or Gemini-output claim. Record screenshots and unexercised steps.
Automated fixture source: `apps/incidentdesk/tests/test_reconstruction_driver_context.py`
(`driver_report`), explicitly labelled **Synthetic test fixture, not Gemini output**.
Road export: unchanged `mig02-multi-review`; native candidate 6.08 s, source frame 152.
Interior has a different SHA; never copy road bytes under that SHA or claim it plays.
This fixture tests display; playback requires a real correctly hash-bound interior file.

1. Use a valid local share session. Seed the labelled synthetic report with its two
   views using the existing RunLog development workflow; attach the unchanged road
   fixture only. Keep report provenance/summary synthetic labels visible. See the
   automated test for exact saved-facts construction; do not present it as AI output.
2. Start app with `INCIDENTDESK_RECONSTRUCTION=true` and
   `INCIDENTDESK_DRIVER_CONTEXT=true`. Open report and review. Confirm interior own
   clock panel: phone in hand 4–8 s, looking at phone 1–3 s, mounted phone 0–9 s,
   seatbelt worn 0–9 s. Subject and hostile test text must display as text.
   Check view label/hash metadata, source label, both interval endpoints, and existing
   Entertainment assessment/reason. Exterior alone has the reconstruction cross-check.
3. With no alignment, confirm “Clocks not aligned; no timing relation shown”. Save
   interior→road alignment +1 s, reviewer_estimated ±0.5 s, reviewer and reason.
   Reload: phone interval aligns to 5–9 s on road clock; contact interval endpoints
   6.0/6.2 s and candidate 6.08 s/frame152 remain separately cited. Differences to
   6.0 s read −1 s / +3 s. Reviewer and ±0.5 s visible. No cause/fault claim.
4. Change alignment to unknown: native intervals remain; no aligned timing relation.
   Repeat with both views aligned to a third reference: interior +3 ±0.5 s, road
   +2 ±0.25 s. Confirm relative +1 s, total ±0.75 s and both reviewers. Do not invent
   a third-view reference without an actual third view on the report.
5. Boundary cases: at contact 6.0 s and ±0.5 s, native observation [5.5,6.5] with
   zero offset says “overlaps contact time”; [5.51,6.5] says “possible overlap
   (within ±0.5 s)”; [6.51,7] shows signed numbers with no overlap wording. Use
   separately labelled synthetic saved reports, never edit immutable stored reports.
6. Forge attachment of a road export bound to a synthetic interior-role view. Confirm
   readable “reconstruction applies to road views only”; no new import/link appears.
   Existing pure exterior attachment still works. Do not alter the producer fixture.
7. Restart for all four combinations of the two flags. Interior context appears only
   when both are true; the existing Driver observations assessment and raw observation
   section remain visible throughout. Fault Status, sourced fines, What happened,
   summary and Q&A stay identical. Check print preview and saved PDF manually; the
   automated test compares full `/print` HTML bytes, not browser-generated PDF bytes.
8. Check narrow viewport, keyboard access to timing details and alignment form, readable
   text wrapping, and accessible labels. No confidence meaning should rely on colour.

Claude records independent gate, browser, and PDF checks before accepting. Codex has
prepared this checklist; preparation is not a browser-smoke pass.

## Round 2 follow-up

- With an existing legacy interior link from before attachment refusal, confirm the
  ignored-export notice and no interior reconstruction comparison, disagreement notice,
  tool/mapping controls or reconstruction event in native/combined timelines. Keep the
  immutable history; exercise this in disposable development data only. A valid exterior
  attachment must continue to render. No producer export edit is needed.
- With the saved road export and +1 s ±0.5 s reviewer-estimated alignment present,
  repeat the four-flag matrix and print preview; verify aligned timing is actually visible
  when both flags are enabled and existing driver/Fault Status/print content remains.
- A candidate needing review, quarantined or with unresolved order may remain in the road
  proposal timeline, but must not be listed as a contact in interior timing context. Valid
  resolved candidates and video contact evidence remain; no new tolerance is introduced.
