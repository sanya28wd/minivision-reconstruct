# MIG-04 cross-check browser smoke (Claude reviewer)

Use the MIG-03 development startup/attachment checklist in docs/migration/MIG-03_WORK_LOG.md.
No producer fixture edits or live model calls. Retain visible synthetic labels.

1. With INCIDENTDESK_RECONSTRUCTION=false, confirm no cross-check or notice appears.
2. Enable the flag. Attach a current export to its SHA-bound report view; open the review page.
   Verify one collapsed Reconstruction cross-check block per attached view, native view/time/frame,
   producer review badges, and explicit unavailable citations (never substituted replay times).
3. Record reviewer track-to-party mappings. Use labelled synthetic saved facts to exercise agreement,
   timing disagreement (signed seconds), conflicting parties, motion disagreement, tracker/model miss,
   rejected/unknown corrected time, and missing calibration/timestamp inputs. Show both source values.
4. Verify the notice near Fault Status only lists disagreements/misses and names the view.
   Verify existing status, colour, fines, storyline, summary and Q&A remain identical.
5. Unmap/cannot-determine a track. Verify Cannot compare. Attach a newer export; prior mappings must
   need re-confirmation, old comparisons must disappear. Cameras with unknown alignment stay separate.
6. Open print preview / save PDF. Verify cross-check and notice are excluded. Compare the resulting
   rendered pages to flag-off output. The app serves HTML at /print and has no PDF-byte endpoint;
   automated tests compare full print response bytes, not a claimed browser PDF byte comparison.
7. Confirm existing Gemini recovery trigger/recovery notes remain visible beside the view evidence.
   Record screenshots, incident/import identifiers, browser/codec and any unexercised step.

Claude owns manual browser smoke and independent critical acceptance. No merge/push/tag by Codex.

## Round 2 regression smoke (Claude)

- On the labelled scratch seed, retain the prior flag/mapping/fault/print checks.
- Check a fact in a saved lost/interpolated span, and a recovered fact whose prior
  movement interval crosses that span: Cannot compare, with the party's missing
  observation reason; neither becomes a motion disagreement notice.
- Check another saved contact outside the assessment interval (and two contacts
  within a broad interval): the out-of-interval producer peak is ambiguous, not a
  timing disagreement. Both times remain visible; no notice from that row.
- With no assessment interval, unequal frames display Times differ (no interval),
  both native times and signed seconds. No notice. Matching frame remains Agrees.
- Parties compare only when contact time agrees and an exact unique typed contact
  matches it. Unverified export provenance always abstains. Missing-input reasons
  are readable. Camera names are primary and hashes secondary metadata.

## Round 3 regression smoke (Claude)

- Interval6.0–6.2, saved contact6.1, reconstruction peak6.08: parties should agree
  for the mapped pair, or disagree with both descriptions for a different pair.
  Two eligible contacts6.05/6.15: Cannot compare, which contact cannot be determined.
  With no interval, same-source-frame timestamps should permit a unique party match.
- A single outside contact naming the same mapped pair should say video analysis
  contact times are inconsistent; no notice. Different pairs retain ambiguity.
- One matching producer candidate plus another valid unmatched candidate: show the
  latter as an additional reconstruction contact, no timing/party disagreement notice.
- Verify frame-less lost/interpolated evidence within a motion span abstains. These
  constructed-record cases are automated fixture tests, not claims about her exports.
- Retain all prior flag, mapping, source citation, fault/fine and print checks.
