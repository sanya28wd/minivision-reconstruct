# Calibration of real ACCIDENT clips: decision

Decided on 2026-10-28, from the first frames of the 40 development clips only (no test clip was viewed).

## What the development clips look like

- Road markings are often hidden or unclear: snow-covered roads (6 of 40 at a glance), night scenes, glare.
- Views vary widely: high wide-angle views, near-fisheye lenses, one letterboxed clip, logos and text overlays,
  and one close-up that barely shows the road.
- 38 of 40 clips are "World" footage from many countries, so US marking dimensions (12 ft lanes, 40 ft dash cycle)
  cannot be assumed. About a dozen clips show clear, regular lane markings.
- No clip comes with camera parameters.

## Options considered

1. **Manual metric calibration per clip** (as for `accident-day-intersection-004`). About 100 calibrations would be
   needed. Most would rest on guessed marking dimensions. It would also require close inspection of every test
   clip, which the protocol forbids before the single test run. Rejected as the main approach.
2. **Image-space mode.** Positions stay in image pixels, and distances are measured relative to the size of the
   road users involved: for example, contact-point separation divided by the mean box height of the pair. No
   per-clip calibration is needed and nothing about a test clip has to be inspected. The benchmark's impact location
   is already given in normalised image coordinates, so location scoring does not depend on calibration.

## Decision

- **Main approach: image-space mode**, with movement and incident thresholds expressed in vehicle-size units
  instead of metres, in a new, explicitly versioned configuration. It will be built and tuned on development clips
  only.
- **Cross-check:** manually calibrate about 5 development clips that show clear, regular markings, and confirm that
  the image-space mode and the metric mode reach the same incident decisions on them.
- **Known limits:** a vehicle-size scale shrinks with distance and changes with vehicle type and viewing angle,
  so separations far from the camera or between a truck and a car are less exact. Speed is still not computed.

In deployment the situation is different: a fixed traffic camera is calibrated once, ideally with surveyed points,
and every clip from it reuses that calibration.
