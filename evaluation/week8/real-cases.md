# Week 8 real-footage runs (frozen config `incident-week8-002`)

Both clips are from the ACCIDENT real set. Neither was used for tuning; every threshold was frozen on the synthetic
fixture before these runs. Results are qualitative: two clips, one label each.

## accident-day-intersection-004 (rear-end, labelled impact 7.80 s, frame 117, 15 fps)

- Calibration `revision-001`: metric, assumption-based (12 ft lanes, 40 ft dash cycle), fitted on a median background
  image. Held-out dash validation RMSE 0.95 m against a 1.5 m tolerance fixed in advance; the stop line's far end lands
  0.06 m from the assumed lane width. See `cases/accident-day-intersection-004/analysis/calibration/ASSUMPTIONS-revision-001.md`.
- Chain: tracks `bytetrack-baseline-004` → reconstruction `real-metric-001` → movement `movement-003`
  (`movement-week7-002`) → incident `incident-002`.
- **Impact time error: −0.27 s** (proposed peak 7.53 s, frame 113, the frame with the visible impact flash), inside the
  ±0.5 s tolerance. The pair proposed (tracks 62 and 64) was checked against the frame and is the colliding pair.
- **Event type wrong: proposed as `near_miss_candidate`, not a collision.** Track 62 ends at the impact frame and track
  64 twelve frames later, so the stop and obstruction afterwards are not observed under the same track IDs, and the
  rest position (x ≈ 13–15 m) lies outside the three calibrated lanes.
- Assessment: `UNDETERMINED` (decisive lost frames for both road users). No contributor was attributed.
- False alarms: 18 other near-miss proposals and 1 collision proposal (tracks 206 and 314 at 24.6 s, queueing at the
  stop line), mostly with track loss or interpolation; all but two assessments are `UNDETERMINED`.

## accident-day-highway-002 (single vehicle, labelled 3.58 s, frame 53)

- Existing relative calibration `revision-001` (across: 1 unit = one lane width; along: 1 unit = bridge-joint spacing,
  ratio to lane width unknown), reconstruction `real-relative-001`, movement `movement-003`, incident `incident-002`.
- **Missed.** No single-vehicle candidate; 39 near-miss proposals, all `UNDETERMINED`.
- Cause: distance thresholds are metres applied to relative units, and the along-road unit is likely tens of metres,
  so contact-point proximity is not meaningful on this calibration. A diagnostic run with thresholds converted to lane
  widths still gave 24 near misses (not a result: this clip's outcome had already been seen).

## Footprint (SAM 2) decision

Deferred. On both real clips the dominant failures are tracking continuity through and after the impact (004) and
calibration (002), not the contact-point approximation. Footprints would address the synthetic single-vehicle limit
(vehicle rotation instead of smoothed path heading), so they stay a candidate once tracking through impact is improved.
