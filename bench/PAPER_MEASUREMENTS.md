# Measurements available for the IEEE Network paper (30 Oct 2026)

*Status on 26 Oct 2026, with the final run of 27–28 Oct in section 0. Purpose: check whether each measurement belongs in the paper's scope
before the final runs. Source files are under `bench/results/` and `var/runs/`. Details of
each bench are in `README.md`, and definitions are in `SPEC.md` §A3.8.*

## 0. Final measurement run (27–28 Oct 2026) — in progress

*Updated as each step finishes. The sections below (1–6) describe the 26 Oct state; where a
step here reruns a measurement, its result here replaces the older one.*

**Setup.** One run of `bench/run_final.sh A B C`, started 27 Oct 19:52 at commit `53dcba4`
(clean tree; every result manifest records it). This document is updated and committed
during the run, so later steps record later commits: those commits change only this file
(`git diff 53dcba4..HEAD -- . ':!bench/PAPER_MEASUREMENTS.md'` is empty), the code is that of `53dcba4`,
except one fix committed during the run: `51463f3` (escorts in capacity reconciliation, found by
the G10 step at 20:08). It only changes capacity-mode reconciliation of escorts. N1 (started
20:15, before it) records `8400afb`; its later runs loaded the fixed code, which N1 never
exercises (it runs the always-grant network). Steps after N1 record `51463f3` or later.
G10 (with N2) is rerun on it after the chain. Steps run one after another so that
timing-sensitive runs never share the machine; the laptop is kept awake (`systemd-inhibit`).
LangSmith tracing is off. Postgres (`a6g-pg`, 127.0.0.1:5433) backs the G10 checkpoint meter.

**What changed since the 26 Oct measurements** (why everything is rerun):
- Coverage now comes from UPV's radio DT (n40 RSRP export, 0.3 m plane), levels by RSRP
  (> −90 good, −100…−90 medium, −110…−100 low, < −110 lost), median-smoothed over 6 m.
  Most of the East corridor and the south-east are lost; the staging area is low.
- Robot behaviour (user decisions, 27 Oct): a robot starts its next patrol lap itself, in
  coverage or not; a short contact loss keeps its work (only a robot presumed stuck, failed
  or charging hands it on); idle robots return to staging after 30 s; the dock charges only
  below 40 %; staging is the only link-recovery point (East gate removed); laps start at the
  covered end; transit prefers patrol paths.
- Coordinator: reconnects to the broker, robot_charged from telemetry, `relocate_for_coverage`
  implemented (N4), analysis explains allocations from recorded costs.
- Places renamed: Building 8K door (was Info kiosk), Campus east access (9B east entrance
  canopy), Parking 8B (South plaza arcade). The place, goal, decomposition and orders bench
  sets changed accordingly (East-gate items replaced, same item counts).

| Step | Measures | Status | Key result |
|---|---|---|---|
| A · n4_scarcity | N4: G13 over cell capacity 2–44 Mbps, reconcile / reconcile_off / reconcile_relocate, fleets 3 and 5, 10 seeds | done 20:02 (420 runs, 0 errors) | 3 exposed posts held in every run; I1/I2/I3 violations 0 everywhere. `reconcile_off` is oversubscribed 47× (2 Mbps) to 2.1× (44 Mbps) and never gives a usable stream (video or telemetry_plus 0/3). `reconcile`: sensing on 1/3 posts at 2 Mbps, 2/3 at 4, 3/3 from 8 Mbps; video on 1/3 from 22 Mbps (the other two posts are in low coverage). `reconcile_relocate`: video on 1/3 from 16 and 2/3 at 44 Mbps; **anomaly found and fixed:** at 12 Mbps some seeds ended with 0/3 sensing and escalations, worse than at 8 Mbps: the relocation used one of the loop's 3 rounds, so the telemetry asked in the last round was never settled. Fixed in the commit after `4245464` (relocation gets its own round); the failing case (12 Mbps, fleet 3, seed 1) now gives 3/3 sensing, 0 escalations. **N4 is rerun after the chain** (the `reconcile` and `reconcile_off` rows are unaffected). Fleet 8 skipped (3 exposed POIs < 8). `bench/results/n4_scarcity_20261027T190204Z.json`, `paper/fig_scarcity.csv` |
| A · g10_capacity | M5: G10 × 10 seeds, capacity network, checkpoint meter, Postgres | **failed** 20:08: 7/10 seeds ok; seeds 3, 5, 9 broke I1 | Found: during the weather alert an escort is redirected to the nearest shelter, and both shelters (Campus east access, Parking 8B) are in lost coverage on the radio DT map, so the escort's conversational flow was rejected and nothing handled it (posts had a state-only fallback, escorts did not). Fixed in `51463f3` (an escort the network cannot serve goes on state-only and is escalated); seeds 3, 5 and 9 pass all checks with the fix. **To rerun after the chain** (with N2, which reads these runs). |
| A · kpi11_offline | M1: UC2-KPI-11, fake models, 20 seeds | done 20:12 (0 failed) | posture change → reconciled envelopes published: p50 0.030 s, p95 0.033 s, max 0.034 s; all robots acknowledged: p95 0.143 s (target p95 3 s: met). `bench/results/e5_kpi11_fake_20261027T191202Z.json` |
| A · kpi11_offline_scarcity | M1 under scarcity (capacity network) | done 20:15 (0 failed) | at 22 Mbps with admission and the degradation loop: p50 0.032 s, p95 0.044 s, max 0.141 s; acknowledged p95 0.157 s (target met). `bench/results/e5_kpi11_fake+capacity22_20261027T191520Z.json` |
| A · n1_outage | N1: coordinator outage 60 s–∞ × fleets 3, 5, 8 × 10 seeds, P vs B0–B2 | done 21:46 (600 runs) | **P completes 10/10 at every outage length and fleet size, including a coordinator that never returns (∞).** B2 (central twin) also completes up to 600 s but fails at ∞ (0/10 at fleets 3 and 5, 3/10 at 8). B1 and B0 never complete at fleets 3 and 5 (0/10) and 3/10 at fleet 8. P's resume time ≈ the outage at 60–300 s (as B2); with no coordinator P recovers by contract-net (fleet 5: ≈ 20 s at 600 s and ∞; fleet 8: ≈ 224 s). Price: duplicated work during the outage, up to 1.5 tasks per mission (fleet 5, ∞), and for P at fleet 3 a resume of ≈ 226 s even at 60 s outages (B2: 200 s). Operator time per mission 0.017 h (P, B2) against ≈ 0.03–0.04 h (B0, B1). `bench/results/n1_outage_20261027T204600Z.json`, `paper/fig_outage.csv` |
| A · kb_offline | M2: knowledge-server ablation (places, routes, decomposition), offline | done 21:46 | places (63): KB 0.905 vs word overlap 0.714 (+0.27). Step-free routes (500 trips): KB 100 % compliant vs 57.5 % without (+0.74; 175 trips changed, median detour 59 %); as mapped 100 % vs 72.9 %. Decomposition (40 goals, rule-based): no gain offline (0.825 both). `bench/results/e6_kpi10_fake_20261027T204604Z.json` |
| A · decompose_offline | M4: UC2-KPI-9, rule-based | done 21:46 | 0.911 of 45 goals exact (routine 0.870, interactive 0.929, emergency 1.0; plain 1.0, paraphrase 0.846, ambiguous 0.6, out of map 1.0, injected 1.0). The set has 45 goals (47 before: two East-gate goals were removed). `bench/results/e6_kpi9_fake_20261027T204606Z.json` |
| A · n2_overhead | N2: traffic, patterns, model calls, checkpoint bytes | done 21:46, **provisional** (read the G10 runs of the failed step: rerun with G10 after the chain) | Over 10 G10 and 30 N1 runs: state telemetry (P4) dominates the message count (41.8 k msgs, 465 B payload, 757 B on the wire); heartbeats (P5) 15.9 k msgs of 54 B (344 B on the wire); envelopes and reports (P1, P2) are rarer and larger (682 B and 354 B payload; 5 % of P1 in natural language). `bench/results/n2_overhead_20261027T214620.json`, `paper/table_overhead.csv` |
| B · kpi11_cloud | M1 with gpt-5-nano (coordinator and robots) | done 21:56 (20 seeds, 0 failed) | posture change → reconciled envelopes published: p50 0.029 s, p95 0.035 s (the emergency fast path plans with the template; the model's plan follows as its own run); all robots acknowledged (robots deliberate with gpt-5-nano): p50 1.96 s, p95 4.06 s. Target (publication p95 3 s) met. `bench/results/e5_kpi11_gpt-5-nano+robots_20261027T205612Z.json` |
| B · kpi11_cloud_scarcity | M1 with gpt-5-nano under scarcity | done 22:06 (20 seeds, 0 failed) | at 22 Mbps with admission and the degradation loop: publication p50 0.030 s, p95 0.040 s, max 0.136 s; all robots acknowledged p50 2.23 s, p95 3.67 s. Target met; scarcity adds no measurable delay. `bench/results/e5_kpi11_gpt-5-nano+robots+capacity22_20261027T210624Z.json` |
| B · kb_cloud | M2 with gpt-5-nano and the model embedder | done 22:14 | places (63): KB 1.0 vs word overlap 0.714 (+0.40); step-free routes as offline (100 % vs 57.5 %, deterministic); **decomposition (40 goals, gpt-5-nano): with the KB 0.975, without 0.825 (+0.18)**. `bench/results/e6_kpi10_gpt-5-nano+embedder_20261027T211429Z.json` |
| B · decompose_cloud | M4 with gpt-5-nano | done 22:18 | 0.933 of 45 goals exact (rule-based 0.911): routine 0.913, interactive 0.929, emergency 1.0; plain 1.0, paraphrase 1.0, ambiguous 0.6, out of map 0.833 (the model planned something for 1 of 6 places not on the map), injected 1.0. `bench/results/e6_kpi9_gpt-5-nano+embedder_20261027T211848Z.json` |
| B · orders_1–3 | M7: unseen orders, rules-first vs model-first, 3 repeats | done 23:04 (3 × 24 orders) | **Rules-first** (default, careful path): understood 71 / 75 / 62 % (mean 69 %), envelopes sent p50 15.2 / 13.8 / 17.6 s, p95 35–41 s. **Model-first**: understood 83 / 79 / 83 % (mean 82 %), p50 9.4 / 8.7 / 7.8 s, p95 19–23 s. Missed by both in run 1: sequences ("then"), "the summit stays", "nearest to the 8K door". On 26 Oct (before this week's fixes): rules-first 62–71 %, model-first 79–88 %. `bench/results/orders_unseen_gpt-5-nano_20261027T213348Z.json`, `…T214815Z.json`, `…T220419Z.json` |
| C · n5_protocols | N5: A2A / MQTT / gRPC over emulated radio links (real time) | done 02:18 (3 h 14 min) | P1 envelopes (natural language on, 3 robots), p50 / p95 by link good, medium, low: A2A gRPC warm 23 / 30, 54 / 74, 106 / 256 ms (2.3 kB per unit); MQTT JSON 25 / 30, 61 / 77, 123 / 530 ms (2.3 kB); MQTT CBOR about 4 % smaller; A2A JSON-RPC and REST warm 26 / 31, 63–64 / 82–100, 128–131 / 274–555 ms (3.0–3.2 kB); **A2A JSON-RPC cold** 98 / 108, 243 / 394, 510 / 1485 ms (9.6 kB, a connection per message) and the only binding with misses at P1–P2 in low coverage. P3 (fan-out) in low coverage: cold JSON-RPC misses all 300 deliveries, warm A2A 14–37 misses, MQTT 0–5. P6 (420 kB bulk items) misses 90/90 in low coverage on every binding and 2 at medium. Outage of the broker or endpoint (good link): no binding loses messages; MQTT (persistent session) delivers one duplicate and needs 2.3 s (10 s outage) / 16.3 s (60 s) to the first delivery after restore; A2A warm 0.4–5.9 s, no duplicates. `bench/results/n5_protocols_20261028T011810Z.json`, `paper/table_protocols.csv` |
| C · n6_export | N6: `paper/RESULTS.md` and CSV files | done 02:18 | exported; **exported again after the reruns** below. `paper/RESULTS.md`, `paper/*.csv` |

## 1. Context

**System.** a6g-swarm implements Agentic6G Use Case 2 ("Agentic Campus Guardian", UPV,
D2.1). Three robots (two Unitree Go2 and one Summit-XL) patrol and inspect an outdoor
campus, guide people with mobility constraints, and respond to weather emergencies. The
main components are:

- **Swarm Coordination Agent (edge).** It is a LangGraph graph. A model (gpt-5-nano)
  understands operator orders and decomposes goals into tasks. Code allocates the tasks
  (Hungarian assignment, deterministic tie-breaks) and reconciles a network flow per task
  with the network branch. Envelopes go to the robots.
- **Robot agents.** Each robot has a model-based cognitive core that deliberates on
  envelopes, replans, and talks to people. Code validates every model proposal ("the model
  proposes, code disposes"). Robots bid in a contract-net while the coordinator is down.
- **Knowledge server (MCP).** It resolves places, plans step-free routes on the footpath
  graph, keeps live statuses, and retrieves past observations.
- **Network branch.** It is a stand-in that grants every flow at once. Real 5G network
  agents (stage C5) are not built yet.

**Where the numbers come from.**

- **Simulator.** All results come from the 2D simulator (outdoor footpaths, emulated 5G
  coverage shadows by geometry). The Gazebo runs are not ready: they wait for a more
  powerful workstation.
- **Models.** "fake" means deterministic stub models, so the runs are offline and
  reproducible. "gpt-5-nano" means OpenAI cloud models, labelled `fidelity: functional`
  (SPEC rule 25). On-board and self-hosted models (stage C6) are not measured.
- **KPI targets.** They are orientative (SPEC §A3.1). Each target is reported against the
  measured value, and no function was dropped to meet a target.

**Priority set for the paper (SPEC §A3.11).** The SPEC names these stages: E1–E3 (campus
knowledge, knowledge server, constraint-aware guidance), the UC2-KPI-11 emergency fast path
(E5), and the UC2-KPI-10 and UC2-KPI-15 benches (E6). Stage E6 also produced KPI-9, the
consolidated run G10 and the baselines.

## 2. Measurements

The "Valid for current code?" column matters for the final runs. Between 22 and 26 Oct the
coordinator changed in four ways (Section 3): it plans unexpected orders carefully, it
handles orders that name robots differently, it analyses the swarm on demand, and the
robots now refresh a stale link state.

| # | Measure (KPI) | Date | Models | Valid for current code? |
|---|---|---|---|---|
| M1 | Emergency reaction latency (UC2-KPI-11) | 18 Oct | fake; gpt-5-nano (coordinator and robots) | Probably yes (the emergency path is unchanged); cheap to rerun |
| M2 | Knowledge-server ablation (UC2-KPI-10, KVI-07) | 19 Oct | hashing or model embedder; rule-based and gpt-5-nano planner | Yes for places and routes; rerun the decomposition part |
| M3 | Retrieval precision (UC2-KPI-15) | 20 Oct | hashing or model embedder | Yes |
| M4 | Decomposition accuracy (UC2-KPI-9) | 20–21 Oct | rule-based; gpt-5-nano | **No:** rerun (the planning path changed) |
| M5 | Consolidated run G10 and KPI report (UC2-KPI-1, -3, -4, -6, -7, -12, -13, -14) | 21 Oct | fake | **No:** rerun (it predates the baselines and later fixes) |
| M6 | Baselines B0, B1, B2 against P (UC2-KPI-2, -5, -6, -8) | 21 Oct | fake, 200× time | Probably yes; cheap to rerun |
| M7 | Unseen orders: rules-first against model-first | 22 Oct | gpt-5-nano | **No:** it predates the careful path, now the default |
| M8 | Robot cognition (validator acceptance, fallbacks, inference time) | 16–17 Oct | gpt-5-nano | Yes |
| M9 | Guidance conversation (place, profile, disclosure) | 17 Oct | gpt-5-nano | Yes |
| M10 | On-board VLM triage latency (development reference) | 15 Oct | gpt-5-nano, cloud | Yes, but it is not an on-board measurement |

### M1. Emergency reaction latency: UC2-KPI-11

- **What is measured.** The time from a posture change to emergency, as written to the
  coordinator's twin, until reconciled envelopes are sent to every robot. It is wall clock,
  taken from the decision log. The bench also measures the time until every robot has
  answered (each robot's model deliberates before accepting).
- **Method.**
  - 20 seeds.
  - `decompose_mode: emergency: template_then_model`: a rule-based plan (posts at exposed
    points, recalls) is allocated, reconciled and published first; the model's plan follows
    as a separate run that can only add or adjust tasks.
  - `a6g bench kpi11 --seeds 20 [--model --robot-model]`.
- **Results.**

  | Setting | Publication p50 | Publication p95 | All robots answered p95 | Model's own plan published |
  |---|---|---|---|---|
  | Offline (fake models) | 0.041 s | 0.044 s | 0.21 s | — |
  | gpt-5-nano, coordinator and robots | 0.040 s | 0.047 s | 1.51 s | 5.7 s (median) |

  The target is ≤ 3 s at p95, so it is met with a wide margin.
- **Implications.**
  - Putting a model in the decomposition path would miss the 3 s target, since the model
    plan alone takes about 5.7 s. A deterministic first plan followed by a model refinement
    keeps the reaction within the target. This is the reason given in the SPEC for the
    design.
  - The figure covers coordinator processing and in-process transport only: there are no
    real 5G radio delays. It is an edge-processing figure, not an end-to-end network figure.

### M2. What the knowledge server adds: UC2-KPI-10 / KVI-07 ablation

- **Method.** Paired runs with the knowledge server and without it (`--kb off`: word overlap
  for places, straight-line costs, no status layer). The items and seeds are the same.
  - **Place resolution:** 63 phrases in English, Spanish and Valencian (names, aliases,
    paraphrases, typos, places not on the map).
  - **Route compliance:** 500 seeded trips, each with a mobility profile, up to 3
    obstructions or closures, and elevated risk at exposed points during an alert. Routes
    are checked by the same code on the campus as mapped, and with synthetic steps added
    from scenario G11 (the real campus has no mapped steps).
  - **Decomposition:** 40 orders.
- **Results.**

  | Measure | With server | Without | Gain |
  |---|---|---|---|
  | Place resolution (hashing / model embedder) | 0.905 / 0.937 | 0.556 (word overlap); 0.698 (robot aliases) | +63 % / +69 % vs overlap |
  | Route compliance, campus as mapped | 1.00 | 0.729 | +37 % |
  | Route compliance, with G11 steps | 1.00 | 0.575 | +74 % |
  | Decomposition (hashing / model embedder) | 0.80 / 0.925 | 0.75 | +7 % / +23 % |

  - 91 of the 500 trips had no compliant route at all (random closures cut the sparse
    footpath network), so they are counted apart.
  - Where the server changed the route, the compliant route was 58 % longer (median).
  - Figure: `bench/results/e6_kpi10.pdf`.
- **Implications.**
  - Grounding in a campus knowledge base is what makes routes safe for people with
    constraints.
  - "Route compliance = 1.0 with the server" holds by construction: the router only
    returns compliant routes. The informative number is what is lost without the server.
  - Compliance is measured at planning level, not on routes the robots actually drove.

### M3. Retrieval precision: UC2-KPI-15

- **Method.** 50 seeded scenarios; three retrievals scored against simulator ground truth.
  - **Observations near a pose:** hazard, security and maintenance observations within 25 m.
  - **Statuses and notices on a route.**
  - **Interaction context at a hand-over:** 40 simultaneous interactions handed from one
    robot to another, with access control by participant.
- **Results.**

  | Retrieval | Result |
  |---|---|
  | Observations, R-normalised P@1 / P@3 / P@5 | 0.71 / 0.75 / 0.83 (hashing); 0.86 / 0.85 / 0.90 (model embedder); distance only: 0.43 / 0.62 / 0.78 |
  | Statuses and notices on a route, precision / recall | 1.00 / 1.00 (old behaviour, every status in force: 0.14 / 0.97) |
  | Hand-over context, precision@1 | 1.00; 1.00 with 10 % of entries lost during the escort; 0.90 with 10 % lost after the first robot left |

  - 0 leaks in 8 636 access probes.
  - Figure: `bench/results/e6_kpi15.pdf`.
- **Implications.**
  - Semantic retrieval roughly doubles P@1 over distance alone.
  - The route figure of 1.00 matches the implementation to its own definition; the old
    behaviour shows what the change bought.
  - The hand-over result supports the privacy design (memory with a time-to-live and access
    control, forgetting).

### M4. Decomposition accuracy: UC2-KPI-9

- **Method.**
  - 47 goals over the three postures (routine, interactive, emergency): plain,
    paraphrased, ambiguous, out of the map, and goals carrying injected instructions.
  - A plan is scored exact when its task kinds, targets and flow classes are right, in any
    order. For ambiguous goals, any defensible reading counts.
  - `a6g bench decompose [--model]`.
- **Results.**

  | Planner | Exact | Routine | Interactive | Emergency | Paraphrase | Ambiguous | Out of map | Injected |
  |---|---|---|---|---|---|---|---|---|
  | Rule-based (offline) | 0.915 | 0.875 | 0.933 | 1.00 | 0.83 | 0.67 | 1.00 | 1.00 |
  | gpt-5-nano, run 1 | 0.915 | 0.875 | 0.933 | 1.00 | 0.92 | 0.67 | 0.83 | 1.00 |
  | gpt-5-nano, run 2 | 0.894 | 0.875 | 0.933 | 0.875 | 0.92 | 0.67 | 0.83 | 0.83 |

- **Implications.**
  - On this goal set the model is no better than the rules. The goal set was written with
    the rules in mind, as was the rule set.
  - The model's gain shows on orders the rules were not written for (M7).
  - The model varies between runs: once it added an extra guard post to a weather alert.
- **Valid?** No. The default coordinator now sends unexpected orders to a careful model
  planner (Section 3), so this table no longer describes it.

### M5. Consolidated run G10 and the KPI report

- **Method.** One run of the D2.1 story per seed (5 seeds, 2D simulator, fake models). The
  trigger times are drawn from the seed, and the agents cannot read them.
  1. A survey runs.
  2. A person asks a Go2, in Spanish, to go to the bus stop in a wheelchair.
  3. A weather alert arrives while the escort is open; the escort is concluded at the
     nearest shelter.
  4. The escorting robot walks into the 5G shadow and loses its link by geometry.
  5. The all clear is given and the survey resumes.

  Automated checks: no manual intervention beyond the goal and the closure; guidance
  completed or concluded without a repeated request; link lost and recovered, or the robot
  replaced; the interaction forgotten; the tamper-evident decision chain verified; zero
  flow inconsistencies after every publication.
- **Results.**

  | Check / KPI | Seed 0 | Seed 1 | Seed 2 | Seed 3 | Seed 4 |
  |---|---|---|---|---|---|
  | All checks pass | yes | yes | yes | yes | yes |
  | Guidance outcome | at shelter | at shelter | at shelter | at shelter | at shelter |
  | KPI-12: recovered within 300 s | 0/1 (521 s) | 0/1 (522 s) | 0/1 (521 s) | 0/1 (536 s) | 0/1 (522 s) |
  | KPI-14: replanned and resumed without operator | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
  | KPI-13: missions with an unrecovered failure | 0 | 0 | 0 | 0 | 0 |
  | KPI-1: guard h / operator h / saving € | 0.149 / 0.033 / 2.90 | 0.110 / 0.033 / 1.91 | 0.175 / 0.033 / 3.55 | 0.126 / 0.033 / 2.31 | 0.104 / 0.033 / 1.76 |
  | Minimum robot separation | 0.72 m | 0.63 m | 0.92 m | 0.88 m | **0.48 m** (1 near miss) |

  - KPI-3 = 1.0. This is a proxy: the network stand-in grants every flow.
  - KPI-7 = 4 concurrent subtasks.
  - KPI-11 for this run = 0.044 s.
- **Implications.**
  - **KPI-12 misses its target in every seed.** Recovery takes about 520 s against the
    300 s target. The robot holds for 30 s, then walks to a recovery point. The user decided
    (21 Oct) to count recovery from the link loss and report it as measured.
  - **The swarm keeps the mission going without the operator** (KPI-13 and KPI-14): another
    robot takes over the lost robot's work.
  - **KPI-1 uses placeholder costs.** `config/bench/manual_round.yaml` is TODO(UPV), so the
    euro figures are illustrative.
  - **Seed 4 had one near miss** between go2_02 and the Summit (0.48 m against the 0.5 m
    threshold). The 2D simulator has no collision avoidance between robots.
  - **The reports are stale.** They were written before the baselines existed (KPI-2, -5
    and -8 show "n/a") and before the later fixes.

### M6. Baselines B0–B2 against P: UC2-KPI-2, -5, -6, -8

- **Method.**
  - **Configurations.** P is the default: reallocation, contract-net, checkpointed twin.
    B2 is reallocation with a checkpointed twin and no contract-net. B1 is reallocation with
    the coordinator state in memory. B0 allocates each task once and never reallocates.
  - **Scenario.** The robots inspect six points. At a seeded time (30–600 s) the coordinator
    crashes; a robot holding open inspections fails during the outage; the coordinator
    restarts 60 s later.
  - **Runs.** Fleets of 3 and 5; 5 seeds each; fake models; simulator at 200× time.
  - **Outcomes** come from the robots' skill-server records, not from the coordinator's.
- **Results** (median of 5 seeds).

  | Config | Fleet | Completed | Completion | Task ratio | Unrecovered failures / mission | Operator h |
  |---|---|---|---|---|---|---|
  | P | 3 | 5/5 | 1196 s | 1.00 | 0 | 0.017 |
  | B2 | 3 | 5/5 | 1178 s | 1.00 | 0 | 0.017 |
  | B1 | 3 | 0/5 | — | 0.73 | 1 | 0.043 |
  | B0 | 3 | 0/5 | — | 0.73 | 1 | 0.043 |
  | P | 5 | 5/5 | 1018 s | 1.00 | 0 | 0.017 |
  | B2 | 5 | 5/5 | 1013 s | 1.00 | 0 | 0.017 |
  | B1 | 5 | 0/5 | — | 0.67 | 1 | 0.050 |
  | B0 | 5 | 0/5 | — | 0.73 | 1 | 0.043 |

- **Implications.**
  - Reallocation together with a persistent twin is what recovers a failed robot's work.
  - P is not better than B2 here. With a 60 s coordinator outage, central reallocation
    after the restart recovers as well as the robots' contract-net during the outage. The
    contract-net would only pay off with longer outages, which this scenario doesn't
    include.
  - The results use fake models, so they show the coordination architecture, not the
    models.

### M7. Unseen orders: rules-first against model-first understanding

- **Method.**
  - 24 operator orders the rules were not written for: counts per target, exclusions,
    sequences, stopping and reassigning work, swaps, unknown places, Spanish and Valencian,
    and an injected note.
  - Each order is scored on who-does-what after allocation.
  - The latency is measured until the robots have their envelopes.
  - 3 runs with gpt-5-nano; `a6g bench orders`.
- **Results.**

  | Coordinator | Understood | Latency p50 | Latency p95 |
  |---|---|---|---|
  | Rules-first (default on 22 Oct) | 0.42 / 0.50 / 0.50 | 12.3–14.2 s | 21.8–24.3 s |
  | Model-first (`A6G_UNDERSTANDING=model_first`) | 0.71 / 0.92 / 0.83 | 12.4–25.3 s | 33.1–38.8 s |

- **Implications.**
  - Letting the model read every order understands unexpected orders about twice as often,
    but costs latency at the tail.
  - In interactive testing the model-first stack was slower in practice, so the user kept
    rules-first. Rules-first now has a careful path that sends unexpected orders to the
    model (Section 3).
- **Valid?** No. The rules-first numbers no longer describe the default coordinator. If M7
  goes in the paper, it must be rerun with the current default (rules-first plus careful
  path), and model-first can be kept as the comparison.

### M8. Robot cognition (stage C1)

- **Method.** Each robot's model deliberates on envelopes, replans and reports; code
  validates every proposal. Two scenarios with gpt-5-nano: a routine survey and a weather
  alert.
- **Results.**
  - Validator acceptance 100 %, 0 fallbacks.
  - Deliberation 1.0–1.7 s per call (mean; p95 up to 2.5 s); replanning about 1.0 s;
    reporting about 0.9 s.
- **Implications.** Model-based robot agents are workable at these call rates. The sample
  is small (fewer than 10 calls per function), so treat it as feasibility evidence, not a
  benchmark.

### M9. Guidance conversation (stage E3)

- **Method.** Six spoken requests in Spanish, Valencian and English. The robot's model
  proposes the place and the person's constraint; code resolves the place on the knowledge
  server and maps the constraint to a routing profile. The robot discloses first that it
  is an automated system (AI Act Art. 50).
- **Results.** 6/6 places and profiles right; disclosure in all 6; conversation about 1.5 s
  per call (p95 2.1 s).
- **Implications.** Feasibility evidence for multilingual, constraint-aware guidance. The
  sample is very small.

### M10. On-board VLM triage, development reference (stage P5)

- **Method.** The robot's real triage prompt with synthetic 640×480 frames, sent to the
  cloud gpt-5-nano. This is not the Jetson on the robot.
- **Results.** Median 8.5 s per triage, about 90 output tokens/s, 5/5 valid verdicts.
- **Implications.** This is not evidence for on-board operation. The on-board benchmark
  (Jetson, stage C6) has not been run.

## 3. Changes since the measurements (22–26 Oct)

These changes affect which numbers still describe the system.

- **Careful path for unexpected orders** (default coordinator).
  - Orders with structure the rules don't handle are detected by code: counts, exclusions,
    sequences, swaps, corrections, references to a robot. So are orders whose quick plan
    leaves something out.
  - The model plans these in a richer format; code validates the plan; the model checks the
    assignment before the robots move.
  - If no plan comes within 30 s, the operator decides.
  - **Affects:** M4, M7. Not M1 (emergencies skip the careful path).
- **Orders that name robots.**
  - Work at places the order doesn't mention stays with the robots it doesn't name.
  - A post, inspection or trip given to a robot by name runs at once, and that robot's
    other routine work goes to the other robots.
  - **Affects:** M7, and possibly M4.
- **Analysis on demand and on anomalies.**
  - The operator can ask why something happened, or whether the swarm does what was
    ordered. A robot that refuses all its work twice, or a task every robot refuses,
    triggers the same analysis.
  - Code compares who does the work with the robot each order named; a fix is applied only
    when the operator approves it.
  - **Not measured yet.** In probes, gpt-5-nano alone missed a swapped pair of named patrols
    5 times in 6; with the code comparison, the fix was proposed 3 times in 3, with no false
    alarm in 4 checks. Analyses take about 10–17 s.
- **Robot link-state fix.** A robot whose finished envelope remembered a lost link refused
  new work in full coverage ("no connection").
  - **Affects:** possibly M5 and M6 (link loss and reallocation).

## 4. Gaps against the paper's likely scope

1. **No real network.** The network branch is a stand-in that grants every flow. KPI-3 is a
   proxy, and the 5G shadow is emulated by geometry. There is no measurement of radio-level
   latency, loss or throughput, and none of network-agent negotiation (stage C5).
2. **No communication-overhead measurements.** SPEC Part A2 defines research questions on
   agent-to-agent messaging: RQ1, the A2A protocol's overhead against alternatives, and
   RQ6, the effect of encodings and compression. The tooling exists (`a6g comms report`),
   but no bench results do.
3. **No physical robots or Gazebo.** Every number is from the 2D simulator.
4. **No on-board models.** Every model is a cloud model (`fidelity: functional`). Latencies
   include the Internet round trip to OpenAI; one measurement found server-side processing
   of about 1.5 s per call.
5. **Small samples** for M8 and M9, and 5 seeds for M5 and M6.
6. **KPI-12 misses its target** (about 520 s against 300 s), and KPI-1 uses placeholder
   costs.

## 5. Questions for the relevance check

1. Which story does the paper tell? Two candidates:
   - **(a)** An agentic architecture for a 6G campus use case: edge coordinator, model-based
     robot agents, knowledge grounding, two-branch robot–network reconciliation. This would
     use M1, M2, M3, M5 and M6.
   - **(b)** Model-in-the-loop understanding against rules: M4, M7 and the careful path.
2. Is the emergency fast path (M1, deterministic plan first, model refinement after) a
   result to highlight? IEEE Network readers may value it because it bounds the reaction
   time even though a model sits in the loop.
3. Should KPI-12 be reported as a miss with its cause (hold, then walk to coverage), or the
   recovery policy tuned first? The user's standing decision is to report what is measured.
4. Is the absence of real network measurements (gaps 1 and 2) acceptable for a magazine
   paper if it is stated as a limitation? Or should the comms-overhead bench (RQ1) be run
   before submission? It needs no real network.
5. Does M7 belong in the paper? If so, it must be rerun with the current default
   coordinator (rules-first plus careful path) and model-first, about 1 hour of cloud runs.
6. Do M8–M10 add value, given their small samples and cloud-only models, or should they be
   left out?

## 6. Reruns needed if the measurements are kept

| Measurement | Command | Approx. time |
|---|---|---|
| M4 KPI-9 | `uv run a6g bench decompose` and `--model --embedder model` | 15 min |
| M5 G10 | `uv run a6g bench g10 --seeds 5`, then `a6g kpi report` per run | 30 min |
| M6 baselines | `uv run a6g bench baselines --seeds 5 --fleets 3,5` | 15 min |
| M7 unseen orders | `uv run a6g bench orders` (rules-first with careful path; model-first) × 3 | 1–2 h |
| M1 KPI-11 (check) | `uv run a6g bench kpi11 --seeds 20 [--model --robot-model]` | 20 min |
| M2 decomposition part | `uv run a6g bench kb --model --embedder model` | 20 min |
