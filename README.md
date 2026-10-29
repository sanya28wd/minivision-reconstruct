# MiniVision-Reconstruct

MiniVision-Reconstruct is being built for Dubai government incident review. Its
goal is to reconstruct what happened in fixed-camera footage and help a human
reviewer assess which road-user actions may have contributed to an incident.
The output should identify the involved track, the action, its timing, the
supporting frames and road context, and any uncertainty. It should allow
"multiple contributors" and "undetermined" when the evidence warrants them.
The reviewer confirms or corrects the assessment; the system does not issue a
legal fault or liability decision.

Speed calculation is outside this project's scope because the government
already has speed metrics. If authorized speed measurements are supplied,
the reconstruction may display them with their source, units, timestamp, and
matched road user. It must not estimate speed from video or infer a speed
violation from an OCR reading of a speed-limit sign.

Week 1 preserves an input video as immutable evidence, hashes it, extracts
`ffprobe` metadata, decodes it once for validation, and writes portable JSON
records under `cases/<case-id>/`.

```bash
python scripts/process_case.py \
  --input path/to/video.mp4 \
  --case-id incident-001 \
  --cases-root cases \
  --ffprobe ffprobe
```

Frame numbers are sequential and zero-based. The frame reader uses OpenCV's
reported decoder presentation position when it is available and strictly
monotonic. OpenCV exposes this as a floating-point time value rather than a
bit-exact container PTS for every codec. If it is unavailable or non-monotonic,
the reader explicitly falls back to `frame_number / ffprobe_frame_rate`.

## Week 2 detection

Download the explicitly configured official YOLO weights to the Git-ignored
`models/` directory, then process an existing Week 1 case:

```bash
python scripts/detect_case.py \
  --case-id incident-001 \
  --cases-root cases \
  --config configs/yolov8n.json \
  --run-id yolov8n-baseline-001
```

The detector exports only `person`, `bicycle`, `car`, `motorcycle`, `bus`, and
`truck` as independent frame detections. It writes JSONL records, an MJPEG AVI
annotated video, and a run manifest under
`cases/<case-id>/analysis/detections/<run-id>/`. It does not create track IDs.

## Week 3 tracking

Tracking runs every decoded frame with explicit ByteTrack settings. IDs are
stable only within one case/run; they are not cross-video identities.

```bash
python scripts/track_case.py \
  --case-id accident-night-highway-001 \
  --cases-root cases \
  --detector-config configs/yolov8n_tracking.json \
  --tracker-config configs/bytetrack.json \
  --run-id bytetrack-baseline-001
```

The run writes `tracks.jsonl`, `tracking.avi`, and an integrity manifest.
Records distinguish observed, lost, recovered, and short-gap interpolated
states. Lost and interpolated records must be excluded from identity metrics.

## Week 4 human track review

From the project environment, install the project and launch the local review UI:

```bash
python -m pip install -e .
streamlit run scripts/review_tracks.py -- --cases-root cases
```

Select a case, automatic tracking run, and reviewer name. Navigate by frame
number or the previous/next buttons. The canvas supports box correction and
manual observations; the review controls also support class correction,
observation/track deletion, merge, split, gap interpolation (up to five
frames), and undo. Merge keeps the selected primary ID and rejects overlapping
boxed observations.

Events are appended to
`cases/<case-id>/analysis/reviews/<tracking-run>/events.jsonl` as hash-linked
records. Each export creates a numbered `revision-NNN/` directory containing
reviewed `tracks.jsonl`, `reviewed.avi`, and a manifest linked to the source
automatic tracks and review log. Automatic tracking artifacts are never
rewritten.

Official sequential UA-DETRAC frames and XML annotations are not downloaded
implicitly. After obtaining them under the dataset terms, prepare the fixed
Week 3 subset with:

```bash
python scripts/prepare_ua_detrac_tracking.py \
  --frames-root /path/to/DETRAC-train-data \
  --annotations-root /path/to/DETRAC-Train-Annotations-XML \
  --output-root data/ua_detrac/tracking_subset \
  --registry data/registry.csv
```

Export only observed and recovered records to MOT text before running TrackEval:

```bash
python scripts/evaluate_tracking.py \
  --tracks-jsonl cases/incident-001/analysis/tracks/bytetrack-baseline-001/tracks.jsonl \
  --sequence MVI_39851 \
  --output-root evaluation/week3/bytetrack-baseline-001 \
  --iou-threshold 0.50
```

## Week 5 road calibration

Launch the local calibration UI for an ingested case:

```bash
streamlit run scripts/calibrate_case.py -- --cases-root cases
```

Enter at least four control correspondences as `image_x,image_y,world_x,world_y`
and at least one separate held-out validation correspondence. Image coordinates
are pixels from the source frame's top-left corner. Metric mode accepts metres
or feet; non-metric mode stores relative coordinates and does not permit
physical distance claims. Neither mode calculates speed. Saving requires every
validation point to be within the entered tolerance.

Road features use image pixel vertices: lanes, crossings, and monitored zones
are polygons; stop lines use two endpoints. Each save creates a numbered
revision under `cases/<case-id>/analysis/calibration/` with
`calibration.json` and `road_overlay.png`. The calibration references the hash
of the preserved case video and can be applied later to automatic or reviewed
tracks from that same video.

## Week 6 bird’s-eye reconstruction

Install the locked project environment and the project-local Chromium runtime:

```bash
UV_PROJECT_ENVIRONMENT=.week6-venv uv sync --python 3.11 --extra dev
PLAYWRIGHT_BROWSERS_PATH="$PWD/.week6-venv/browsers" .week6-venv/bin/python -m playwright install chromium
```

Prepare a replay from an explicitly selected reviewed tracking export and a
validated Week 5 calibration:

```bash
.week6-venv/bin/python scripts/prepare_reconstruction.py \
  --cases-root cases \
  --case-id incident-001 \
  --tracking-run bytetrack-baseline-001 \
  --review-revision revision-001 \
  --calibration-revision revision-001 \
  --run-id week6-001 \
  --ffmpeg ffmpeg
```

Use `--automatic` instead of `--review-revision` to select automatic tracks
explicitly. Preparation verifies source integrity, projects road-contact points,
and creates a 30 FPS H.264 display proxy with a mapping to every displayed source
frame. Raw coordinates, lost states, and review provenance remain available.
Metric feet are converted to metres; relative calibration has no distance scale.

Launch the local workspace:

```bash
PLAYWRIGHT_BROWSERS_PATH="$PWD/.week6-venv/browsers" .week6-venv/bin/python -m streamlit run scripts/reconstruct_case.py -- \
  --cases-root cases --media-port 8768 --ffmpeg ffmpeg
```

Select the case, reconstruction run, scene revision, marker revision, and
reviewer name. Playback, scrubbing, linked road-user selection, pan/zoom, and
trajectory controls run in the browser. Frame stepping loads the exact original
frame. Solid paths show observations; dashed paths show interpolation; `R`
identifies reviewer observations; unavailable intervals break the path.
Vehicle silhouettes are symbolic and cannot establish physical contact.

The road editor changes visual surfaces, markings, labels, and display rotation.
Calibrated references stay locked. Save scenes and reviewer markers as numbered
revisions; a saved revision becomes selected for replay and export. Markers can
link to road users, source frames, and an optional map location. The PNG button
exports the saved scene at the currently inspected source frame.

Export the selected saved revisions through the same Canvas renderer:

```bash
PLAYWRIGHT_BROWSERS_PATH="$PWD/.week6-venv/browsers" .week6-venv/bin/python scripts/export_reconstruction.py \
  --cases-root cases --case-id incident-001 --run-id week6-001 \
  --scene-revision initial --marker-revision initial \
  --export-id presentation-001 --snapshot-frame 0 --media-port 8769 --ffmpeg ffmpeg
```

Runs live under `cases/<case-id>/analysis/reconstruction/<run-id>/`. Exports
include two 1920×1080 MP4s, PNGs, projected observations, source/replay frame
indices, selected scene/marker documents, and integrity manifests. Export IDs
must be new. FFmpeg with `libx264` and the configured Playwright Chromium runtime
are required; missing tools raise errors.

A controlled, visibly synthetic demonstration can be created without detector
weights or external footage:

```bash
.week6-venv/bin/python scripts/create_reconstruction_demo.py \
  --cases-root cases --case-id week6-controlled-demo \
  --duration-seconds 40 --road-users 20 --ffmpeg ffmpeg --ffprobe ffprobe
```

To rebuild the packaged TypeScript assets after frontend changes:

```bash
npm ci --prefix src/minivision_reconstruct/reconstruction/component/frontend
npm run build --prefix src/minivision_reconstruct/reconstruction/component/frontend
```

Run the smoke suite, including browser and real encoder workflows:

```bash
PLAYWRIGHT_BROWSERS_PATH="$PWD/.week6-venv/browsers" .week6-venv/bin/python -m pytest tests/smoke -q
```

## Week 7 movement sequence and uncertainty

Derive movement records from a Week 6 reconstruction run. Every threshold is in
the explicit configuration `configs/movement.json`; nothing has a code default.

The commands below set `PYTHONPATH=src`. On this Mac, the `.week6-venv`
editable-install `.pth` files can receive the macOS `hidden` flag, and
Python 3.11.15 then skips them, so `minivision_reconstruct` stops importing
(including inside Streamlit subprocesses). `chflags nohidden
.week6-venv/lib/python3.11/site-packages/*.pth` also restores the install. The
browser smoke tests pass `PYTHONPATH` explicitly.

```bash
PYTHONPATH=src .week6-venv/bin/python scripts/analyze_movement.py \
  --cases-root cases --case-id incident-001 --reconstruction-run week6-001 \
  --config configs/movement.json --run-id movement-001
```

The run is written once to `cases/<case-id>/analysis/movement/<run-id>/` and
hash-linked to the reconstruction manifest, calibration, and configuration:

- `smoothed.jsonl`: the raw projected contact point is kept beside a
  segment-local Savitzky–Golay position. Smoothing never crosses `lost` frames.
- `movement_observations.jsonl`: the README §6.5 records, holding heading,
  movement state (`moving`, `stopped`, `turning`, `unknown`, `unavailable`),
  lane, and position uncertainty.
- `events.jsonl` and `sequence.json`: ordered system proposals, each with a
  time window and evidence signals. Event types are stop, start, left/right
  turn, lane change or entry, stop-line crossing, zone entry/exit, closest
  approach, and first/last observed. When two windows overlap, their order is
  marked unresolved instead of being asserted.
- `separation.jsonl`: sampled distances between contact points, with combined
  uncertainty. These are not gaps between vehicle bodies.
- `lane_sequences.json`, and `trajectories.csv`, which is rounded to each
  value's uncertainty. The JSONL files keep full precision.

**No speed is calculated.** "Stopped" means the contact point showed no
displacement beyond its own uncertainty for at least the configured duration.
Heading is reported only when displacement exceeds uncertainty.

Uncertainty is built in three parts:
- *Relative* uncertainty comes from contact-point pixel error. Its size depends
  on box height and detection confidence, and it is increased for interpolated
  and reviewer points. It is propagated through the homography Jacobian at that
  pixel.
- *Absolute* uncertainty adds the calibration's held-out RMSE in quadrature.
- Displayed values use the precision of their uncertainty's leading digit (for
  example, `12.3 ± 0.7 m`), and times are never finer than one source frame.

In non-metric calibrations, everything is in `relative_units` and separations
are flagged as non-physical. Pedestrians receive no lane assignment.

The Week 6 workspace (`scripts/reconstruct_case.py`) now has a **Movement
sequence and uncertainty** panel. It contains an events timeline and a ranked
table, raw and smoothed coordinates with uncertainty bands, heading,
movement-state and lane-occupancy timelines, and pairwise separation. Choosing
an event (by clicking its point, or with **Go to source frame**) seeks the
replay to that exact source frame and selects the road user.

### Validation against staged motion

Public CCTV has no maneuver timing labels. Validation therefore uses visibly
synthetic staged-motion cases whose kinematics are scripted exactly. Each case
has eight road users and covers:
- stop and go at a stop line;
- a left turn and a right turn;
- a lane change;
- a crossing near-approach;
- a 1 s occlusion and an interpolated span;
- a pedestrian who waits and then crosses;
- an abrupt bus stop.

Boxes carry seeded pixel noise.

```bash
PYTHONPATH=src .week6-venv/bin/python scripts/create_staged_motion_fixture.py \
  --cases-root cases --case-prefix week7-staged-heldout --seeds 101 102 103 \
  --pixel-noise-sigma 0.7 --duration-seconds 45 --ffmpeg ffmpeg --ffprobe ffprobe
PYTHONPATH=src .week6-venv/bin/python scripts/evaluate_movement.py \
  --cases-root cases --case-ids week7-staged-heldout-101 week7-staged-heldout-102 week7-staged-heldout-103 \
  --movement-run movement-003 --evaluation-root evaluation/week7 --evaluation-id heldout-movement-003
```

Ground truth lives in `cases/<case-id>/annotations/movement_ground_truth.json`.
Physical staged toy-vehicle videos (implementation README §11.5) can be
annotated by hand in the same schema and evaluated without code changes. Set
`source` to `staged_physical` and list only the exhaustively labelled types in
`annotated_event_types`. Events are matched within each event type and set of
road users. A match is accepted within 3× the declared tolerance, so errors
beyond tolerance are still measured. Precision and recall count only matches
within tolerance.

Configuration `movement-week7-002` was tuned on seeds 1–2 only, then frozen
before the held-out seeds were generated. Results are in `evaluation/week7/`.

| Split (σ = pixel noise) | Maneuver + stop peak error, mean / max | All annotated events within tolerance | Truth inside reported time window | Truth inside reported position uncertainty | Lane sequences exact | Position RMSE, raw → smoothed |
|---|---|---|---|---|---|---|
| Tuning, seeds 1–2, σ 0.7 px | 0.18 s / 0.45 s | 62 / 62 | 100% | 100% | 100% | 0.18 → 0.09 m |
| **Held-out, seeds 101–103, σ 0.7 px** | **0.17 s / 0.43 s** | **93 / 93** (1 extra closest-approach proposal) | **100%** | **100%** | **100%** | **0.19 → 0.10 m** |
| Stress, seeds 201–202, σ 1.5 px | 0.33 s / 1.17 s | 58 / 63 | 95% | 99% | 100% | 0.40 → 0.20 m |

Tolerances: stop/start ±0.5 s, turns ±1.0 s, lane change ±0.75 s, stop-line
crossing ±0.25 s, zone entry/exit ±0.5 s, closest approach ±0.5 s.

Known limitations:
- Stops are detected about 0.25 s early and starts about 0.25 s late. The
  final creep of a smooth stop falls below the displacement resolution, so the
  reported windows extend half the stationary window past the estimate.
- At twice the tuned pixel noise, one bus stop was split into two stationary
  intervals, and several stop/start times fell outside ±0.5 s.
- All of this ground truth is synthetic and rendered from rectangles, so it is
  not a detector benchmark and not real CCTV. Real-footage validation needs
  hand-annotated staged physical videos.

## Week 8 collision, near-miss, and contribution analysis

Week 8 adds an incident layer on top of a movement run. It proposes, for
review:
- post-encroachment time (PET) where two paths cross;
- collision, near-miss, and single-vehicle candidates;
- for each collision or near miss, an assessment of which road users'
  observed actions may have contributed.

Every output is a system proposal. Only time and position are used; no speed,
velocity, or time-to-collision is calculated.

```bash
PYTHONPATH=src .week6-venv/bin/python scripts/analyze_incident.py \
  --cases-root cases --case-id <case> --movement-run movement-003 \
  --config configs/incident.json --run-id incident-002
```

Outputs go to `cases/<case>/analysis/incident/<run-id>/`: `conflicts.jsonl`
(PET), `events.jsonl` (candidates), `assessments.jsonl`, `config.json`, and a
`manifest.json` hash-linked to the movement run. Runs are immutable.

How candidates are proposed:
- **Proximity is required:** contact points within `contact_distance` plus
  their combined uncertainty, or both road users inside one conflict area at
  once (PET ≤ 0).
- **A collision also needs two kinds of consequence** soon after: a visible
  stop, a heading jump, or a stop inside a lane long enough to obstruct it.
  With proximity but fewer consequences, it is a near miss.
- **Image box overlap is supporting evidence only.** It never creates or
  upgrades a candidate.
- Every candidate lists its signals, source frames, and the movement events
  it cites, with a time window that covers the uncertain impact time.

Contribution rules each cite movement events and frames:
- R1: did not stop behind a stopped road user;
- R2: lane change into an occupied lane;
- R3: entered an occupied conflict area;
- R4: no visible stop at a stop line;
- R5: turned across the other road user's path.

R4 is an observation, never a violation finding: `rule_source` is always null
until an approved agency rule source exists. Similar actions that did not meet
a rule are listed as counterevidence. Lost, interpolated, or reviewer-made
positions in the window just before a candidate force `UNDETERMINED`; a
missing observation is never used against anyone.

### Reviewing candidates

The workspace (`scripts/reconstruct_case.py`) has an **Incident analysis**
panel under the movement panel. It shows:
- the candidates and each one's signal checklist;
- **Go to impact frame**, which seeks the replay;
- PET conflicts;
- the contribution evidence for and against, and unresolved facts.

A reviewer can **confirm, reject, or modify** a candidate (impact time or
location) or its assessment (outcome and contributors). A note is required to
reject or modify.

Each save is an immutable revision under `<run>/reviews/`, sealed with
SHA-256 and chained to the previous revision (the first is chained to the run
manifest). `events.jsonl` and `assessments.jsonl` are never rewritten.
**Export reviewed view** writes the reviewed records, each embedding its
original proposal, under `<run>/reviewed/<revision>/`.

### Validation against scripted incidents

Ten scripted scenarios render one case each:
- rear-end, t-bone, lane-change sideswipe, and both road users changing lane;
- a near-miss crossing, a normal crossing, and a pedestrian near miss;
- an occluded impact;
- a single-vehicle swerve;
- a pass where boxes overlap but road users stay a lane apart.

Truth includes impact time and location, collision type, path-crossing PET,
and the expected contribution outcome and rules.

```bash
PYTHONPATH=src .week6-venv/bin/python scripts/create_incident_fixture.py \
  --cases-root cases --case-prefix week8-incident-heldout --seeds 101 102 103 \
  --scenarios rear_end t_bone lane_change_sideswipe both_lane_change near_miss_crossing \
              normal_crossing occluded_impact single_vehicle box_overlap_only pedestrian_near_miss \
  --pixel-noise-sigma 0.7 --ffmpeg ffmpeg --ffprobe ffprobe
PYTHONPATH=src .week6-venv/bin/python scripts/evaluate_incident.py \
  --cases-root cases --case-ids <the 30 held-out case IDs> \
  --incident-run incident-002 --evaluation-root evaluation/week8 --evaluation-id heldout-incident-002
```

Configuration `incident-week8-002` was tuned on seeds 1–2 (plus Week 7 staged
traffic as a negative check), then frozen before held-out seeds 101–103 were
generated. Results are in `evaluation/week8/`.

| Split | Detection P / R / F1 | False alarms per clip | Impact time error, median (within ±0.5 s) | Location within 3 m | Collision type | PET error at matched radius | Contribution agreement (given detection) |
|---|---|---|---|---|---|---|---|
| Tuning, seeds 1–2 | 1.00 / 0.88 / 0.93 | 0.00 | 0.06 s (86%) | 100% | 100% | ≤ 0.04 s, 6 of 6 | 14 / 14 |
| **Held-out, seeds 101–103** | **0.95 / 0.88 / 0.91** | **0.03** | **0.06 s (90%)** | **100%** | **93%** | **≤ 0.04 s, 9 of 9** | **20 / 21 (21 / 21)** |
| Ablation: proximity only | 0.85 / 0.92 / 0.88 | 0.13 | 0.15 s (86%) | 100% | 94% | ≤ 0.04 s | 21 / 21 |

The ablation shows the consequence rule is worth keeping: without it, every
near miss is also proposed as a collision, and box overlap scenarios produce
collisions.

On two real ACCIDENT clips (`evaluation/week8/real-cases.md`):
- **Rear-end (`accident-day-intersection-004`):** found 0.27 s before the
  labelled impact, at the right pair, but as a near miss. Both tracks are lost
  at impact, so the stop afterwards is never observed. The assessment is
  `UNDETERMINED`.
- **Single-vehicle (`accident-day-highway-002`):** missed. Its relative
  calibration makes contact-point distances meaningless.

Real traffic also produces many low-confidence near-miss proposals, nearly
all `UNDETERMINED`.

Known limitations:
- Single-vehicle detection relies on heading jumps. Smoothed contact-point
  headings cannot separate a post-impact swerve from a lane change or braking
  noise: 1 of 3 held-out swerves was found.
- One held-out occluded impact became a near miss. The consequence search
  starts after the hidden frames, missing the other road user's stop. A fix
  needs a new configuration version and fresh held-out seeds, not retuning on
  101–103.
- Distance thresholds are in metres and are only meaningful with metric
  calibration.
- Tracking loss through an impact turns collisions into near misses on real
  footage. This, not the contact-point approximation, is the next bottleneck;
  SAM 2 footprints are deferred until tracking through impact improves.
- R3 and R5 never fire in the fixture, so they are untested on data.
- On CARLA CCTV clips the frozen config finds no held-out accident: the
  colliding vehicles are rarely detected and tracked through the impact.
- The fixture is rendered rectangles, so it is not a detector benchmark.

### ACCIDENT synthetic subset

`scripts/prepare_accident_subset.py select` chooses 40 CARLA clips:
- 8 per collision type (3 dev, 5 held-out);
- every weather condition in each split;
- no camera view (`map` + `camera_position`) in both splits, and one clip per
  view.

The selection is saved to
`data/accident/synthetic/manifests/week8-subset.csv`. After the clips are
downloaded from Kaggle with their dataset paths,
`scripts/prepare_accident_subset.py register` hashes them into
`data/registry.csv`. Held-out rows are marked `development_allowed=false`, and
no row is used for tuning.

`scripts/evaluate_accident.py` runs a split through the whole chain and scores
it (§11.2):

```bash
PYTHONPATH=src .week6-venv/bin/python scripts/evaluate_accident.py prepare \
  --selection data/accident/synthetic/manifests/week8-subset.csv --split week8_heldout \
  --data-root data/accident/synthetic --cases-root cases \
  --detector-config configs/yolov8n_tracking.json --tracker-config configs/bytetrack.json \
  --tracking-run bytetrack-baseline --reconstruction-run carla-metric-001 \
  --movement-config configs/movement.json --movement-run movement-003 \
  --incident-config configs/incident.json --incident-run incident-002
PYTHONPATH=src .week6-venv/bin/python scripts/evaluate_accident.py evaluate \
  --selection data/accident/synthetic/manifests/week8-subset.csv --split week8_heldout \
  --data-root data/accident/synthetic --cases-root cases \
  --incident-config configs/incident.json --incident-run incident-002 \
  --output evaluation/week8/accident-heldout-002
```

Calibration is metric and automatic, from each clip's stored camera pose,
CARLA's default 90° field of view (checked on one dev clip to within 6.4 px),
and a flat road at height 0. The annotations hold no lanes and no world
positions for moving vehicles, so scoring is in time and image space.

| Split | Detected (±0.5 s) | False alarms per clip | Right-pair near miss at impact | Both vehicles tracked at impact | Both keep track ID after impact |
|---|---|---|---|---|---|
| Dev (15) | 1 | 0.00 | 0 | 1 | 0 |
| **Held-out (25)** | **0** | **0.04** | **4** | **3** | **0** |
| Held-out, proximity only | 4 (type 0 / 4) | 7.24 | – | 3 | 0 |

The frozen config detects none of the held-out accidents. Detection and
tracking are the cause: YOLOv8n misses the small top-down vehicles at impact
in most clips, and no clip keeps both colliding vehicles' track IDs through
the post-impact window, so consequences are never seen on the same pair.
Where both were tracked, the right pair was proposed as a near miss at the
impact time. Details: `evaluation/week8/accident-synthetic.md`.

## Next steps

The core pipeline through Week 8 is implemented and validated on controlled
data. On real and simulated CCTV, results are limited by perception: vehicles
are missed, or their tracks break at the moment of impact. The next phase
makes the core pipeline defendable on real footage before adding MVP
features. Work is time-boxed, and every result comes from data that was
locked before tuning.

### Phase A: real-footage hardening (about 3 weeks)

1. **Build the real evaluation set first.**
   - Done (2026-10-28): 100 real ACCIDENT clips, 40 development and 60
     test, selected and locked before any was processed. Development clips
     come from the benchmark's geo-aware train split (World, UAE) and test
     clips from its test split (US states), so the cameras differ; one clip
     per YouTube source; already inspected clips excluded. Test clips are
     registered with `development_allowed=false`. Rules and the selection
     are in `evaluation/real-footage/PROTOCOL.md`.
   - Hand-label the two colliding road users for about 2 s either side of
     impact, to measure track continuity through impact. The labelling app
     (`scripts/label_impact_app.py`) and box proposals for 20 development
     clips are ready; the labels themselves must be made by a person.
   - Calibration decided (`evaluation/real-footage/CALIBRATION.md`): an
     image-space mode with thresholds in vehicle-size units, cross-checked
     against manual metric calibration on about 5 development clips.
2. **Measure the current pipeline** on the development clips, stage by
   stage: detection of the colliding vehicles, track survival through
   impact, and incident detection (found, impact-time error, image
   location, collision type, false alarms per clip). This is the baseline.
3. **Improve, in order of expected impact**, checking each change on the
   development clips only:
   - link track fragments that end and restart at the same place near an
     impact, and compare ByteTrack with a tracker that uses appearance;
   - raise the detector input size or use image tiling for small and
     distant vehicles;
   - fine-tune the detector on traffic-camera and crash footage (never on
     test clips), including night and rain;
   - add consequence signals that still work when a track breaks, such as
     two tracks ending at the same point;
   - fix the occluded-impact consequence window and the metre-based
     thresholds in non-metric calibrations.
4. **Freeze a new configuration (`incident-week8-003`) and run the test
   clips once.** Report the results with an ablation of each change, and
   keep failures in the table.

Phase A ends when its time box ends, or earlier if changes stop producing a
measurable improvement on the development clips. Either way, the
configuration is frozen and the test clips are run once.

Also in this phase, if time allows:
- staged videos with measured ground truth, to test movement timing on
  real camera footage;
- a before-and-after measurement of human track review (identity errors,
  fragmentation, reviewer time).

### Phase B: complete the MVP (about 2 weeks)

- One clickable incident timeline combining movement events, incident
  candidates, and reviewer decisions.
- A temporal safety report and PDF built from the reviewed records, with
  evidence, confidence, and limitations for every claim.
- OCR road context (timestamps, route numbers, road signs), reusing the
  earlier MiniVision OCR cleanup and UAE sign parsing, with temporal voting.
- Privacy-redacted export, and one case package (annotated video,
  trajectory CSV, event JSON, PDF, manifest).
- Acceptance: one unseen fixed-camera case runs from upload to a reviewed
  export, and every report claim links to evidence.

### Phase C: final evaluation and handoff

- Final evaluation and ablations, runtime measurement, and failure analysis.
- Clean-install verification, final demonstration, and report.
- Stretch goals only after the MVP passes its acceptance tests: traffic
  signal state, heatmaps, similar-incident search, SAM 2 footprints.

The detailed scope, phases, and acceptance gates are in
[the implementation plan](IMPLEMENTATION_PLAN.md).
