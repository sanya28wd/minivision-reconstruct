# Week 8 ACCIDENT synthetic (CARLA) benchmark, frozen config `incident-week8-002`

40 clips from the ACCIDENT synthetic set (`data/accident/synthetic/manifests/week8-subset.csv`): 8 per collision
type, 3 dev and 5 held-out, every weather condition in each split, no camera view in both splits. The incident
config was frozen on the scripted fixture before any of these clips was run; nothing here was tuned on them.

## How the clips were run

`scripts/evaluate_accident.py prepare` takes each clip through the existing chain, with the same detector and
tracker configs as the real cases (`yolov8n_tracking.json`, `bytetrack.json`), movement config
`movement-week7-002`, and incident config `incident-week8-002`.

Calibration is metric and automatic, from the camera metadata only:
- Each annotation stores the camera's world position and rotation but no field of view. CARLA's default of 90
  degrees reproduces the parked cars of dev clip `Town03_head-on_rain_30` within 0.3–6.4 px of their labelled
  boxes (80 or 100 degrees give 40–160 px).
- The road is assumed flat at world height 0. No lanes are defined, because the annotations carry no road
  geometry; the lane-obstruction consequence signal and lane-based contribution rules therefore cannot fire.
- Every clip's assumptions are written next to its calibration (`ASSUMPTIONS-revision-001.md`).

Moving vehicles have no world positions in the annotations, so scoring is in time and image space:
- **Primary candidate:** the most confident collision or single-vehicle proposal, earliest on ties. It is chosen
  without looking at the label.
- **Detected:** its peak is within ±0.5 s of the labelled impact.
- **Location:** its point projected back into the image, against the labelled impact point (normalised
  coordinates) and box.
- **Pair:** both proposed tracks overlap the labelled colliding vehicles' boxes (IoU ≥ 0.3).
- **Diagnostics (labels used for scoring only):** whether the colliding vehicles have a track at the impact frame,
  and whether those tracks keep their ID through the 2 s post-impact window in which the consequence evidence
  (stop, heading jump) must be seen.

Per-clip rows: `accident-dev-002/`, `accident-heldout-002/`, `accident-heldout-ablation-proximity-only-002/`
(`results.csv`, `summary.json`, `summary.md`).

## Results

| Split | Clips | Detected (±0.5 s) | False alarms / clip | Right-pair near miss at impact | Both vehicles tracked at impact | Both keep track ID through post-window | Chain failures |
|---|---|---|---|---|---|---|---|
| Dev | 15 | 1 (type wrong) | 0.00 | 0 | 1 | 0 | 1 (no detections) |
| **Held-out** | **25** | **0** | **0.04** | **4** | **3** | **0** | **0** |
| Held-out, proximity-only ablation | 25 | 4 (type 0 / 4) | 7.24 | – | 3 | 0 | 0 |

**The frozen config detects no held-out accident.** It stays conservative: one false collision in 25 clips, and
6.2 low-confidence near-miss proposals per clip.

**Detection and tracking are the cause, not the incident rules.**
- In 12 of 25 held-out clips no track overlaps either colliding vehicle at the impact frame; in 10 only one does.
  Vehicles are small, often at night or in rain, and seen from above; YOLOv8n at 640 px misses them. In dev clip
  `Town06_sideswipe_rain_21` it detects nothing in the whole clip.
- In no clip do both colliding vehicles keep their track ID for the 2 s after impact. A collision needs two
  consequence signals on the same tracks after proximity, so with IDs switching or lost at impact, the best the
  rules can propose is a near miss. This is the same failure as real case `accident-day-intersection-004`.
- Where both vehicles were tracked, the rules found them: 4 held-out clips have a near miss with the right pair
  within ±0.5 s of the impact (`Town04_rear-end_sunset_40`, `Town04_sideswipe_sunset_02`,
  `Town04_sideswipe_wet_19`, `Town07_t-bone_clear_02`).

**Proximity alone is not a detector.** Without the consequence rule the system proposes 7 collisions per clip,
and the 4 "detections" are what chance overlap with that many proposals would give; all 4 have the wrong type.

## Changes made during this run (none affects the frozen evaluation)

- Two crash fixes for duplicate IDs, both found on these clips: a near-parallel path pair crossing twice around
  one passage (`incident/pet.py`), and a second shared-area conflict inside one proximity run
  (`incident/collision.py`). Re-running every scripted fixture run (50 with `incident-week8-002`, 30 with the
  ablation config) gave byte-identical conflicts, events, and assessments, so the fixture results stand.
- A clip whose chain fails is kept in the table as a miss, with its reason.
- The "near miss at impact" column first counted any near-miss time window overlapping the impact. With about six
  near misses per clip that is close to chance, so before writing this report it was tightened to "near-miss peak
  within ±0.5 s", plus the right-pair check. Only the scoring changed; no pipeline output changed.

## What would change the result

1. Detection of small, top-down vehicles: a larger inference size or a stronger detector (a change to the
   detector config, evaluated on the Week 2–3 sets first).
2. Track continuity through impact: link a track that ends at the impact to one that starts at the same spot,
   then look for consequences on the linked pair. This and the occluded-impact consequence window are the planned
   `incident-week8-003`. It needs new held-out data (fresh fixture seeds and a fresh, scene-disjoint ACCIDENT
   selection), not these 25 clips.
3. Lane geometry for CARLA towns, so the obstruction signal can count.
