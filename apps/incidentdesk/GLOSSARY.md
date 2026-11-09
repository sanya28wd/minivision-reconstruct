# Incident Video Analysis Report

Turns an officer's uploaded traffic-incident videos into one detailed, evidence-cited report. The report covers what happened, whose fault it was, who is fined and how much, and the driver's behaviour where it is visible. Accident reconstruction (speeds, trajectories, crush physics) is a separate project and not part of this context.

## Language

### People

**Officer**:
The RTA or Dubai Taxi reviewer who uploads videos, reads the report and decides. They use the tool without the builder present.
_Avoid_: User, operator, supervisor, reviewer

**Evaluator**:
Anyone, other than the owner, who assesses the prototype itself (professor's reviewers, RTA staff, academics). They try samples and uploads and leave Feedback, but their approvals are not real decisions.
_Avoid_: Reviewer, tester, user

**Share Link**:
A forwardable secret link that gives anyone holding it use of the tool without signing in. The owner can switch it off or rotate it.
_Avoid_: Invite, access code, login

**Feedback**:
Free-text comment from an Officer or Evaluator about the tool itself (what to improve, what they disliked). The owner takes it in and turns it into fixes. It never enters incident analysis.
_Avoid_: Review, override, complaint

### Input

**Incident**:
The single real-world event that one report covers. It may contain zero, one or several collisions.
_Avoid_: Accident, case, crash

**View**:
One continuous video from one camera, uploaded as one file.
_Avoid_: Clip, angle, feed, POV

**Interior View**:
A view filmed inside the vehicle, showing the driver or cabin.
_Avoid_: Cabin cam, driver cam

**Exterior View**:
A view of the road scene: the vehicle's outward cameras, another vehicle's camera, or CCTV.
_Avoid_: Dashcam (a dashcam is only one kind of exterior view)

### Output

**Report**:
The single combined document produced for one incident from all of its views.
_Avoid_: Dossier, output, verdict

**Collision**:
One contact event inside an incident, between a vehicle and another road user or an object.
_Avoid_: Crash, impact, accident

**No Incident**:
The finding that the views show normal driving, with no collision and no violation.
_Avoid_: Peaceful, clean, negative

**Cannot Determine**:
The finding given when the views do not hold enough evidence to support a conclusion. It always states the reason and never comes with a guess.
_Avoid_: Unknown, unresolved, N/A

**Finding**:
One conclusion in a report (what happened, a fault status, a violation, a behaviour). It always cites the view and timestamp it rests on.
_Avoid_: Claim, result, output

**Contradiction**:
Two views giving incompatible findings about the same fact. It is shown to the officer, not resolved silently.
_Avoid_: Conflict, mismatch, discrepancy

### Fault and penalties (Dubai-native)

**Party**:
A road user involved in the incident: a vehicle and its driver, a pedestrian, a cyclist, or an animal.
_Avoid_: Actor, participant, entity

**Fault Status**:
Each party's standing for the incident. The three Dubai Police colours are used only with their official meaning: **At Fault** (pink), **Not at Fault** (green), and **White** (white), which the RTA issues when neither party is accused or the other party is unknown (`docs/research/UAE_ROAD_STRUCTURE_AND_PRIORITY.md`, F1). A fourth state, **Cannot Determine**, is not a police form. It means this report's evidence is not enough to decide. The report shows it as "Not determined by this report" with a neutral grey chip, never as a police form colour.
_Avoid_: Liability percentage, fault share, blame; calling Cannot Determine "white".

**Violation**:
A specific traffic-law breach by one party, seen in a view, which carries a fine and possibly black points. A party can have a violation without being At Fault for a collision.
_Avoid_: Offence, infraction

**Contributing Factor**:
A behaviour or condition that may have made a collision worse or harder to avoid, without making that party At Fault.
_Avoid_: Partial fault, secondary fault

### Accident classification (Dubai Police / MOI terms)

**Accident Outcome**:
The incident's consequence class: **Simple Accident** (حادث بسيط: no injury, vehicles driveable), **Injury Accident**, or **Fatal Accident** (وفيات).
_Avoid_: Severity level, minor/moderate/major

**Injury Grade**:
The grade of one injured person: **Serious** (بالغة), **Moderate** (متوسطة) or **Minor** (بسيطة). Recorded only when the injury is visible; otherwise Cannot Determine.
_Avoid_: Casualty level

**Accident Type**:
The physical form of a collision: **Collision between vehicles** (صدم وتصادم), **Run-over** (دهس), **Rollover** (تدهور), or **Other**.
_Avoid_: Collision type, crash type

**Cause**:
The official MOI accident-cause label that explains a collision, e.g. sudden swerving (الانحراف المفاجئ) or not leaving sufficient distance (عدم ترك مسافة كافية).
_Avoid_: Reason, root cause

**Referral**:
The flag that an incident must go to police or prosecution: hit-and-run, a visible injury, or a death.
_Avoid_: Escalation, dispatch

### Review

**Approval**:
The officer's acceptance of a report as their own decision.
_Avoid_: Sign-off, confirm

**Override**:
An officer's replacement of one finding, with a stated reason. It is recorded beside the original finding, never in place of it.
_Avoid_: Edit, correction

### Evaluation

**Run**:
One production of a report from a set of views. Every run is logged the same way, with its **Origin**: an officer upload through the app, or a benchmark run.
_Avoid_: Test, job, execution

**Benchmark**:
The labelled set of incidents, with their truth, used to score runs.
_Avoid_: Test set, gate clips, corpus

**Baseline**:
The best accepted benchmark result (accuracy and latency), which later changes are compared against.
_Avoid_: Reference run, golden run

**Regression**:
A change that leaves accuracy or latency worse than the baseline.
_Avoid_: Breakage, drift
