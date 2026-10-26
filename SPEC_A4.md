## A4. Results for the IEEE Network submission: the network in the loop, agent protocols and paper exports

*Added on 2026-10-26; updated the same day with the parameters of the campus n40 cell (§A4.6.1). Merge this part into `SPEC.md` after Part A3 and add it to the "Parts" note at the top. It responds to the readiness review of the paper (project doc `claude/PAPER_PENDING_CHANGES.md`). Work runs in the 2D simulator; the protocol micro-benchmark (stage N5) runs on real sockets in Linux network namespaces. Items marked **TODO(UPV)** become configuration with safe defaults.*

This part amends §5, §12, §A1.4, §A1.12.1, §A2.5.4, §A2.7, §A3.7 and §A3.8 (see §A4.11). Everything else in this specification, Parts A1–A3 and rules 1–30 still applies, in particular: KPI targets are orientative, no function is dropped or simplified to meet a target, and results are reported as measured (§A3.1).

**Deadline.** Results freeze on **Thursday 29 Oct 2026 at 12:00**; the paper is submitted on 30 Oct. Proceed through the stages in the order of §A4.10 **without waiting for approval between stages**. Report at the end of each stage (what was built, tests, runs launched, numbers), and stop only when a stop rule of §A4.10 fires.

### A4.1 Why this part exists

The readiness review identified four objections a networking reviewer is likely to raise:

1. **The network is a stand-in.** The network branch grants every flow, so the network never constrains a decision: UC2-KPI-3 is 1.0 by construction and the zero-inconsistency result of reconciliation (§A3.7) is trivial. This contradicts the paper's own criticism of other architectures ("the network is assumed").
2. **No network-level numbers.** There are no measurements of agent traffic, of the A2A overhead, or of message latency over a constrained link, although the tooling exists (`a6g comms report`).
3. **The proposed configuration P does no better than B2** in the baselines, because a 60 s coordinator outage is too short for peer-to-peer renegotiation to matter.
4. **Several measurements no longer describe the current code** (M4, M5, M7 in `PAPER_MEASUREMENTS.md`).

The answer is not a packet-level simulator this week (that is the revision, §A4.12), but a network that can **refuse and degrade**: coverage levels taken from the network twin, a per-cell capacity, and a network stand-in whose grants follow from both. Most of this is already specified (link zones and shared cell capacity in §A2.5.4, the capacity-limited `noa_stub` in §A2.7, the degradation loop in §A3.7); this part brings it forward and fixes its scope.

### A4.2 Additional rules (extend §2, §A1.2, §A2.2 and §A3.2)

31. **Every result carries a manifest.** Each result file records the git commit and dirty flag, the full command line, hashes of the configuration files used, seeds, models with their fidelity (rule 25), the network mode (`always_grant | capacity`), the coverage source (`radio_dt | model | shadows`), the time factor of the simulator and the host. `a6g paper export` (§A4.9) lists every result whose commit differs from `HEAD` as possibly stale.

32. **Network emulation is labelled as such.** Results produced with the emulated network state it (`network: emulated`, with the coverage source and the capacity model version). They are never described as measured on the campus network. Parameters taken from campus measurements say so, with their provenance.

33. **One time base per kind of result.** The 2D simulator runs faster than real time (10–200×), so wall-clock network delays would be inflated in simulated time. Therefore:
    - in mission runs, the network acts on **decisions** (link level, link loss, grants, budgets), in simulated time, and messages are not delayed;
    - per-message latency is measured **only** in the real-time micro-benchmark of N5, and never inferred from accelerated runs.

34. **The network may say no.** With `network.mode: capacity`, the network stand-in grants, degrades or rejects flows from capacity and coverage. Agent code never assumes a grant; invariants I1–I3 (§A3.7) must hold with refusals.

### A4.3 Stage N0: reruns of measurements that no longer describe the code

Rerun with the current default coordinator (rules-first plus careful path) and write the manifest of rule 31:

| Measurement | Command | Notes |
|---|---|---|
| M1, UC2-KPI-11 | `uv run a6g bench kpi11 --seeds 20` and `--seeds 20 --model --robot-model` | Rerun again after N4 with `--network capacity` (reaction time under scarcity) |
| M7, unseen orders | `uv run a6g bench orders` × 3 runs, default coordinator and `A6G_UNDERSTANDING=model_first` | Cloud models, about 1–2 h; launch on day 1 |
| M2, decomposition part | `uv run a6g bench kb --model --embedder model` | Optional; places and routes are still valid |
| M5, G10 | After N4, with `--network capacity` and the coverage map of N3, 5 seeds (10 if time) | Checks of §A3.11 E6 must still pass |
| M6, baselines | Superseded by N1 (which includes the 60 s outage) | |

M4 (UC2-KPI-9) is not rerun for the paper; leave its section in `PAPER_MEASUREMENTS.md` marked as superseded.

*Accept:* the reruns finish with manifests; `PAPER_MEASUREMENTS.md` is updated with the new numbers and the "Valid for current code?" column.

### A4.4 Stage N1: outage sweep and fleet sizes for the baselines

Extend `a6g bench baselines` (§A3.8 baselines, README "Baselines B0–B2 against P"):

- **Outage length.** `--outage-s 60,180,300,600,inf`. The coordinator crashes at the seeded time (30–600 s, as today), a robot holding open inspections fails during the outage (as today), and the coordinator restarts after the given time. `inf` means it does not return before the mission horizon `--horizon-s` (default 3600 s simulated).
- **Fleets.** `--fleets 3,5,8`. Add synthetic manifests for the extra robots if needed (simulation only, mixing Go2-like and Summit-like robots as in the existing fleets).
- **Seeds.** 10 per cell. Fake models, 200× time. Run cells in parallel with `--jobs` if the stacks can be isolated (ports, `var/` directories); otherwise run overnight.
- **Outage kind.** Record which path the surviving robots used to learn of the robot failure and of the coordinator loss during the outage (MQTT last-will through the edge broker, A2A timeouts, heartbeat). If the robot-failure detection relies on the broker hosted at the edge, label the experiment `coordinator_crash` in the results, not `edge_partition`. **Optional**, only if detection already works without them: `--outage-kind edge`, which also stops the broker and the knowledge server. Do not implement new detection mechanisms this week.
- **New metrics** (in addition to completion, completion time, task ratio, unrecovered failures and operator hours):
  - `time_to_resume_s`: from the robot failure to the first of its open tasks started by another robot;
  - `resumed_during_outage`: the failed robot's tasks started by another robot before the coordinator returned;
  - `duplicates`: tasks executed by two robots;
  - `restart_to_reconciled_s`: from the coordinator restart to the reconciled state (§9.4);
  - messages and bytes per robot (from the comms log);
  - coordinator processing time per invocation, p50 and p95, per fleet size (scalability of allocation and reconciliation).
- **Output.** `bench/results/n1_outage_*.json` and `paper/fig_outage.csv` with one row per run: `config, fleet, outage_s, seed, completed, completion_s, task_ratio, time_to_resume_s, resumed_during_outage, duplicates, operator_h`. A draft plot (`bench/plot_outage.py`) is for sanity only; the paper figure is drawn from the CSV.

*Accept:* the sweep runs unattended; with the 60 s outage the results reproduce M6 within the seed variance (or the difference is explained); the README section on baselines is updated.

### A4.5 Stage N2: agent traffic and the overhead of twin-as-memory

No new behaviour; measurement and instrumentation only.

- **Traffic.** Run `a6g comms report` over the G10 runs (after N4) and over the N1 runs with P at the 60 s outage. Produce, per posture (routine, interactive, emergency) and per fleet size:
  - per-robot uplink and downlink bytes per second, payload and wire bytes as accounted by the binding (label the wire bytes `accounted`: in-process stacks use the in-memory transport);
  - per pattern (P1–P11): message count, mean and p95 payload bytes, wire bytes, and the share of bytes in `nl.*` fields.
- **Model calls.** From the cloud runs (N0 M1 and M7, the C1 and E3 cloud acceptance runs if their logs exist): per role and function, number of calls, input and output tokens (mean), latency p50 and p95. Label `fidelity: functional`, cloud endpoint, latency includes the Internet round trip.
- **Checkpoint overhead (design principle DP1 of the paper).** Instrument the coordinator's checkpointer (`put`, `put_writes` and their async variants) with the serialized bytes and the write latency, per event type (goal, report, failure, posture change, operator reply). Record the size of the latest checkpoint and the number of versions over a mission. Measure with the Postgres checkpointer (`a6g-coordinator --checkpointer postgres`) on 5 G10 seeds if the bench can use it; otherwise with the bench's checkpointer, and say which. Do the same for the robots' local SQLite checkpoints (bytes and latency per write).
- **Output.** `bench/results/n2_overhead_*.json` and `paper/table_overhead.csv`.

*Accept:* the report covers every pattern seen in G10; checkpoint bytes and latency are reported with n, median and p95.

### A4.6 Stage N3: coverage levels from the network twin

The binary coverage shadows become a coverage map with levels, taken from the network twin's radio layer.

#### A4.6.1 The campus cell

One Nokia n40 gNB on the rooftop of Building 8G covers the pilot sector, so **all robots share one cell** and network scarcity is sector-wide. Its parameters (UPV, 2026-10-26):

| Parameter | Value |
|---|---|
| Network, node, BTS | 5G_UPV (Nodo2), BTS ID 267, software SBTS24R3_ENB_0000_001040_000009 |
| Site | Building 8G, 39.4784439 N, −0.3338678 E (height TODO(UPV)) |
| Cell | NRCELL-9 (MRBTS-267/NRBTS-1), physical cell ID 41, NR cell ID 4374537, cell type 4DL4UL: 4 TX and 4 RX antenna paths |
| Carrier | n40, TDD, NR-ARFCN 476000 (2380.000 MHz), 20 MHz channel (the cell can be reconfigured to 30 MHz) |
| UL:DL logical data slot ratio | 3:7, i.e. 30 % of data slots are uplink |
| Maximum output power | 26.5 dBm |
| DL MIMO | 4×4 or 4×2 closed-loop spatial multiplexing |
| Expected cell size | 15 km: a random-access (cell range) setting, **not** a coverage radius; not used by the model |
| **Measured uplink** (UPV network document, TCP goodput, one device) | **about 22 Mbps at 20 MHz: the maximum single-UE uplink of the cell, measured in the velodrome (line of sight, centre of the simulated area)**; about 44–46 Mbps after reconfiguring to 30 MHz |
| **Measured coverage** (UPV maps along the track loop of the velodrome, which lies in the simulated area but is not a footpath the robots patrol) | RSRP about −95 to −77 dBm and SINR about 8 to 26 dB, read from the plots: strongest on the south-west straight, weakest at the north-east end and the north-west curve. No outage on the loop. **No CSV is available**: use the values read from the plots (§A4.6, `calibration: plots`) until the radio DT map arrives |
| Robot modem | The Go2 carries a 5G outdoor router (12 V, Ethernet), not a RedCap module; model and uplink capabilities TODO(UPV) |
| Other cells on campus | A second n40 cell (NRCELL-9 of MRBTS-270, 20 MHz) at the Nexus site on Building 6G. B7 LTE and n258 mmWave at both sites; n78 indoors in Building 8G. None of them is modelled (§A4.12) |
| Exposure | The campus network has a Network-as-Code server integrated with CAPIF and reference slices: the real counterpart of the capacity stand-in, for the revision (§A4.12) |

**Derived values** (TS 38.306 maximum data rate: R_max = 948/1024, overhead 0.08 UL and 0.14 DL in FR1, 30 kHz subcarrier spacing with 51 PRB, about the same with 15 kHz and 106 PRB; multiplied by the TDD slot share). The harness recomputes them from the configuration and writes them into the manifests:

- **Uplink peak per UE:** about 26 Mbps with one layer and 64QAM (87.6 Mbps continuous × 0.3); about 35 Mbps with 256QAM; twice that with two uplink layers.
- **Downlink peak:** about 306 Mbps (4 layers, 256QAM, × 0.7).
- **Default uplink cell capacity: 22 Mbps,** the measured TCP goodput of one device at 20 MHz. It is about 85 % of the single-layer 64QAM peak, so it is consistent with one uplink layer. It is also consistent with the >12 Mbps measured with RedCap devices.
- **Reference-signal power per resource element:** about 26.5 − 10·log10(612) ≈ −1.4 dBm plus antenna gain, if 26.5 dBm is the total power over the carrier (TODO(UPV): total or per port).

The peaks are derived. The 22 Mbps is the maximum uplink a single device reached in the velodrome. It is therefore taken at the best SINR read from the velodrome plots (about 25 dB) and is the anchor and cap of the uplink-rate model below. Label both as such (rule 32), and replace them with the uplink survey when it exists. The measured SINR is a downlink quantity; the emulation assumes uplink quality follows it (record this assumption in the manifests).

**Map.** Draw the site as a marker labelled "n40 gNB (Building 8G)" in:
- the live view (`/view`);
- `a6g campus edit`;
- the coverage preview (`a6g network coverage --preview`);
- the Gazebo campus overlay (optional, visual-only marker like the POI markers, §A1.5).

**Configuration.** The site position goes in the campus file, because geometry is defined once (rule 16):

```yaml
network_sites:
  - {id: site_8g, name: "Building 8G rooftop", lat: 39.4784439, lon: -0.3338678, height_m: 20}   # height TODO(UPV)
```

Radio parameters, coverage, link profiles and capacity go in `config/network.yaml`:

```yaml
network:
  mode: always_grant            # always_grant (tests, default) | capacity (N4 benches, G10 rerun)
  cells:
    - id: n40_nodo2_cell9       # 5G_UPV (Nodo2), BTS 267, NRCELL-9
      site: site_8g
      band: n40
      pci: 41
      nci: 4374537
      nrarfcn: 476000           # 2380.000 MHz
      bandwidth_mhz: 20
      duplex: tdd
      ul_slot_share: 0.3        # UL:DL logical data slot ratio 3:7
      scs_khz: 30               # TODO(UPV): confirm (51 PRB at 30 kHz; 106 PRB at 15 kHz)
      max_output_power_dbm: 26.5      # TODO(UPV): total or per port
      antenna: {ports: 4, gain_dbi: 10, azimuth_deg: null, tilt_deg: null}   # TODO(UPV); null azimuth = omnidirectional
      dl_mimo: "4x4 or 4x2 closed-loop spatial multiplexing"
  coverage:
    source: model               # model until the radio DT export arrives (Tue 27 Oct), then radio_dt | shadows (today's polygons)
    radio_dt_file: data/network/upv_vera_cpi_rsrp.csv          # TODO(UPV): lat,lon,cell_id,rsrp_dbm from the ray-tracing radio DT
    model: {pathloss: 3gpp_38901_uma, h_ut_m: 1.5, los_from_footprints: true, shadow_fading: false}
    calibration: plots          # no measurement CSV; values read from UPV's velodrome plots (RSRP and SINR along the track loop)
    calibration_targets:
      loop: osm                 # the velodrome track loop: from OSM (leisure=track or the pitch it surrounds), else drawn in `a6g campus edit`
      rsrp_dbm: {median: -86, min: -95, max: -77}
      sinr_db:  {min: 8, max: 26}
    sinr_from_rsrp: {offset_db: 101, clip_db: [-10, 26]}   # SINR ≈ RSRP + 101 dB, read from the plots (−77 dBm ↔ ~24 dB, −92 dBm ↔ ~9 dB)
    sample_step_m: 2            # sampling along footpaths (robots only move on footpaths)
    levels_by: sinr
    thresholds_sinr_db: {good: 13, cell_edge: 5, degraded: -5}     # ≈ RSRP −88 / −96 / −106 dBm with the offset above; below degraded, or RSRP < lost_rsrp_dbm: lost
    lost_rsrp_dbm: -115
    hysteresis_db: 3
    min_dwell_s: 2              # simulated seconds before a level change takes effect
    shadows_override: true      # existing coverage_shadows are forced to `lost` (placeholders, not measured)
  uplink_rate:                  # maximum uplink rate of one robot at a location (N4)
    anchor: {ul_mbps: 22, sinr_db: 25}     # maximum single-UE uplink measured in the velodrome, at its best plotted SINR
    spectral_efficiency: {alpha: 0.6, se_max_bps_hz: 5.55}   # SE = min(alpha·log2(1 + SINR), se_max): attenuated Shannon, 64QAM cap
    # rate(x) = anchor.ul_mbps · SE(SINR(x)) / SE(anchor.sinr_db), capped at anchor.ul_mbps
  link_profiles:                # representative values per level for N5 (netem)
    # ul_mbps = rate at the lower SINR edge of each level (conservative), derived by the harness
    good:      {ul_mbps: derived, dl_mbps: 100, rtt_ms: 19, jitter_ms: 5,  loss_pct: 0.1}   # rate(13 dB) ≈ 11.6 Mbps (22 at the best spots); rtt from measured n40 (9.76 DL + 8.89 UL one-way)
    cell_edge: {ul_mbps: derived, dl_mbps: 20,  rtt_ms: 45, jitter_ms: 15, loss_pct: 1}     # rate(5 dB) ≈ 5.4 Mbps; rtt, jitter, loss TODO(UPV)
    degraded:  {ul_mbps: derived, dl_mbps: 5,   rtt_ms: 80, jitter_ms: 30, loss_pct: 3}     # rate(-5 dB) ≈ 1.0 Mbps; rtt, jitter, loss TODO(UPV)
    lost:      {outage: true}
```

**Coverage sources.**

- **`radio_dt` (preferred, from Tue 27 Oct).** Import the RSRP map exported from UPV's ray-tracing radio DT of the n40 cell (calibrated with measurement campaigns): `a6g network import-coverage --rsrp <csv>`. Interpolate to sample points every `sample_step_m` along the footpath graph. For each sample, keep the serving cell (strongest RSRP; one cell today), its RSRP, the SINR (from the export if it has one, otherwise from `sinr_from_rsrp`) and the level. Write `sim/<campus>_coverage.json` (deterministic, generator rule of §A1.5). Re-run the N4 sweep and the G10 rerun with this source if it arrives before Wed 28 Oct 12:00; otherwise keep `model` and record it.
- **`model` (until then).**
  - RSRP = reference-signal power per resource element + antenna gain − PL.
  - PL comes from the 3GPP TR 38.901 UMa path-loss model (Table 7.4.1-1) at 2.38 GHz, with the antenna height of the site and h_UT = 1.5 m (the model's lower validity bound; robot antennas sit lower, so the map is slightly optimistic).
  - LOS or NLOS is decided **deterministically**: NLOS when the straight line from the site to the point crosses a building footprint (OSM cache, §A1.5). No shadow fading.
  - **Calibration against the velodrome plots** (`a6g network calibrate`; there is no measurement CSV):
    - **Loop.** Take the velodrome track loop from OSM (an Overpass query for `leisure=track`, or the pitch it surrounds, inside the pilot box, cached with the ODbL note like the other extracts). If OSM does not have it, ask UPV to draw it as one polygon in `a6g campus edit`. Store it in the campus file as `calibration_loops`.
    - **RSRP.** Sample the loop every 2 m and set a single RSRP offset (it absorbs the antenna gain and the total-or-per-port question) so that the model's median RSRP on the loop equals −86 dBm. Report the predicted minimum and maximum against the plotted −95 and −77 dBm, and how many loop samples are LOS and NLOS by the footprint test.
    - **SINR.** SINR = RSRP + 101 dB, clipped to [−10, 26] dB. Report the predicted SINR range on the loop against the plotted 8–26 dB.
    - **If the predicted range is far off** (spread more than twice or less than half the plotted 18 dB), report it and continue: the radio DT map will replace the model.
    - **Labels.** Record `coverage: model (38.901 UMa, offset matched to the plotted velodrome RSRP; SINR from the plots)`. Footpaths behind buildings (NLOS) are extrapolated; the manifests say so.
  - Draw the velodrome loop with its predicted levels in the coverage preview, next to the footpaths, so the match to the plots can be checked by eye.
  - Print the share of footpath length per level and the level at every exposed POI. If more than 80 % of the footpath length is `good`, say so: on this campus scarcity is then a matter of capacity, not coverage, and the relocation variant of N4 is skipped (§A4.7).
- **`shadows`.** Today's behaviour (binary), kept for regression.

**Simulator and robots** (amends §12 and §5):

- Each tick, the simulator looks up each robot's serving cell and level from its position on the footpaths, with hysteresis and minimum dwell. `RobotState` gains `link_level` (`good | cell_edge | degraded | lost`) and `cell_id`; `link_quality` is derived (`lost → lost`, `degraded → degraded`, otherwise `ok`), so the link-loss policy (§A1.12.1) is unchanged and still acts on `lost`.
- The live view colours the footpaths by level and shows the sites.
- `a6g network coverage [--preview coverage.html]` prints the share of footpath length per level and per sector, the level at each POI, staging and link-recovery point, and the overlap between the new `lost` samples and the old `coverage_shadows`.
- `a6g config validate` warns when a link-recovery point is not `good` and lists the level of every exposed POI.
- G10 must still contain its link-loss step (the shelters lie in a shadow). The shadow polygons are placeholders, and the measured velodrome shows no outage. If the map removes the shadow, keep it as an override (`shadows_override`) and label it `injected shadow` in the results: link loss in G10 is then a stress case, not a measured property of the campus.

**N3b (optional, only after N4 passes): coverage-aware recovery.** New link-loss policy `recover_to_coverage` (amends §A1.12.1): after the hold, the robot drives to the nearest reachable footpath point whose level is at least `recover_min_level` (default `good`), taken from its local copy of the coverage map, instead of the nearest configured recovery point. Compare it with `hold_then_recover` on G10 (5 seeds): link recovery time (UC2-KPI-12), distance driven, tasks delayed. The default policy stays `hold_then_recover` until UPV decides.

*Accept:*
- Import and calibration are deterministic; unit tests cover the interpolation, the serving-cell choice, hysteresis and dwell.
- The 2D-simulator scenario tests pass with `coverage.source: shadows` and with the new map (with `shadows_override` if needed).
- `a6g network coverage` produces the level summary; the coverage source and its provenance appear in every manifest.

### A4.7 Stage N4: a network stand-in that can refuse, and the degradation loop

Implements the capacity model of `noa_stub` (§A2.7) and the degradation loop of §A3.7 for the paper, **code only**: grant decisions are code (rule 21). The stub's model would only write `nl.rationale`; it is off in all benches (`--stub-llm off`), and the manifest records it.

**Capacity model** (`config/network.yaml`, extends the block above):

```yaml
  capacity:
    cells: {n40_nodo2_cell9: {ul_capacity_mbps: 22}}   # measured UL TCP goodput of the campus cell at 20 MHz (§A4.6.1); swept in G13
    cost: spectral_efficiency   # a flow streaming at x costs rate × SE(anchor) / SE(SINR(x)) cell units (= rate at the anchor or better)
    # consistency: a robot alone in the cell at x gets uplink_rate(x), the same function as §A4.6
    flow_requirements:          # from §A3.7; TODO(UPV) from the uplink survey
      telemetry:          {ul_mbps: 0.0, always: true}
      survey_uplink:      {ul_mbps: 1.0, best_effort: true}
      verification_burst: {ul_mbps: 8.0, max_s: 30}
      conversational:     {ul_mbps: 0.1, max_rtt_ms: 150}
      video_control:      {ul_mbps: 4.0, guaranteed: true}
      telemetry_plus:     {ul_mbps: 0.3, guaranteed: true}
```

**Admission** (per reconciliation round, deterministic):

1. Each requested flow is evaluated at the location where it will stream (the POI or post, not the robot's current position): serving cell and level from the N3 map.
2. Guaranteed and burst flows are taken in order of task priority, then POI risk (§A3.6.2), then `flow_id`. A flow is **granted** if three conditions hold: its rate fits the robot's maximum uplink rate at that location (`uplink_rate`); its RTT requirement fits the level's `rtt_ms`; and its cost (`rate × SE(anchor)/SE(SINR(x))`) fits the remaining capacity of the cell. Otherwise it is **rejected** with reason `coverage` (location too poor, or lost) or `capacity` (cell full).
3. Best-effort flows share what is left and are **degraded** (with the achieved rate) when they get less than requested.
4. Grants are released when their task ends, is moved or is cancelled. When a robot's serving cell or the level at an active streaming location changes, the stub re-evaluates the affected flows and sends a `NetworkEvent`, which triggers `reconcile` on the coordinator.
5. Every decision goes into the flow records (§A3.7) and an `AchievableServiceReport`.

**Degradation loop in `reconcile`** (§A3.7, now implemented):

1. For a rejected or degraded `video_control` flow, switch posts from `video` to `telemetry` sensing, lowest-risk posts first, and re-derive the flows.
2. Repeat up to `reconcile_max_iter` (3).
3. If a task with priority ≥ `recharge_reassign_min_priority` still has no flow, escalate to the operator.
4. **Variant `reconcile.relocate_for_coverage`** (off by default; run only if N3 found an exposed POI below `good`, otherwise record why it was skipped): for a post rejected for `coverage`, look for an alternative viewpoint on the footpaths within `relocate_radius_m` (default 20 m) of the POI, with a level of at least `cell_edge` and no building footprint between the viewpoint and the POI; take the best level, then the shortest distance, and re-allocate the task there (`Decision(kind="reconcile", rule="relocate_for_coverage")`).

**Budgets (I3, rule 29).** A robot opens a streaming class only when its flow is granted, and at the granted rate; a rejected or degraded flow leaves it on telemetry. Budget logs show the opening time and rate.

**Baseline without network-aware planning** (`reconcile: off`, for the experiment only): envelopes are published as planned (video at every post); flows are requested after publication; each robot streams at the nominal rate once at its post. The cell model then shares capacity among active streams by weighted fair share (in resource units) and computes each stream's effective rate. A stream is `usable` if its effective rate is at least its class requirement.

**Scenario G13, emergency under network scarcity** (2D simulator): a weather alert (emergency posture) with every exposed POI of the campus to be held; posts with `sensing_mode: video` by default. Measure at steady state (all reachable posts held, or 300 s simulated after the alert):

- `posts_held`, `posts_video_usable`, `posts_sensing_usable` (video usable, or telemetry_plus granted);
- `oversubscription` (demanded resource units / cell capacity), for `reconcile: off`;
- tasks without a flow and flows without a task after every publication (I1, I2);
- `posture_change_to_reconciled_s` (UC2-KPI-11 under scarcity), reconciliation iterations, escalations;
- relocations and the extra distance driven (relocate variant).

**Sweep.** `uv run a6g bench scarcity`: uplink capacity of the campus cell ∈ {2, 4, 8, 12, 16, 22, 44} Mbps (22 is the measured 20 MHz goodput; 44 the measured 30 MHz goodput, the "what if the operator widens the carrier" point) × variants {`reconcile_off`, `reconcile`, `reconcile_relocate`} × fleets {3, 5} (and 8 if the campus has at least 8 exposed POIs) × 10 seeds; fake models; 200× time. Output `bench/results/n4_scarcity_*.json` and `paper/fig_scarcity.csv` (one row per run).

**Also:** UC2-KPI-3 is computed from the grants under `network.mode: capacity` (no longer a proxy; still emulated, rule 32). Rerun M1 and G10 with `--network capacity` (N0).

*Accept:*
- Unit tests: admission is deterministic and respects per-level rate, RTT and cell capacity; release on task end; re-evaluation on a level or cell change.
- I1 and I2 hold after every publication in G13 and G10 with the capacity model, including refusals; budget logs show no stream above telemetry before its grant (I3).
- Sanity: when capacity exceeds demand, `reconcile` and `reconcile_off` give the same video coverage.
- Under scarcity, `reconcile` shows no oversubscription and every remaining video post has a grant (the E5 scarcity acceptance of §A3.11).

### A4.8 Stage N5: agent protocols over emulated radio links (micro-benchmark, real time)

A subset of C3 and C4 (§A2.5.3, §A2.6): the micro-benchmark only, on real sockets. It does not use the robot simulator, so it runs in real time (rule 33).

**Environment.** Linux network namespaces with `ip netns` and `tc` (root or `CAP_NET_ADMIN` on the workstation; TODO(UPV) if not available, stop and report rather than falling back silently to the shim):

- namespace `a6g-edge`: the coordinator-side endpoints and the MQTT broker;
- namespaces `a6g-r1 … a6g-rN` (N = 3 and 5): the robot-side endpoints;
- each robot namespace is connected by a veth pair to a bridge in namespace `a6g-ran`; `tc netem` (delay = RTT/2, jitter, loss, rate) is applied on the robot side (uplink) and on the bridge side (downlink) with the per-level link profile of N3. Robot-to-robot traffic crosses the bridge, so it sees two radio legs, as UE-to-UE traffic through the UPF would.

Check before measuring that the RTT measured with `ping` is within 10 % of the configured value for each profile.

**Bindings compared** (same `AgentMessage` semantics and patterns; agent code untouched, rule 22):

| Binding | Mode | Notes |
|---|---|---|
| `a2a_jsonrpc` | **cold**: new client and connection per message, agent card fetched per new client | As typically deployed |
| `a2a_jsonrpc` | **warm**: pooled persistent connection, cached agent card | Isolates the connection cost |
| `a2a_grpc` | warm channel | Only if the installed `a2a-sdk` supports the gRPC transport (rule 12: check its documentation); otherwise record "not supported in a2a-sdk x.y" |
| `a2a_rest` (HTTP+JSON) | warm | Same condition; this is the REST style ACP had before it was folded into A2A |
| `mqtt` | persistent session (clean start false), QoS 1; request/response with correlation ids and reply topics; encodings `json` and `cbor` | Binding exists |
| `zenoh` | router at the edge, peer mode between robots | **Optional**, only if everything else is done |

TLS 1.3 on every binding (HTTPS, MQTT over TLS, gRPC over TLS), with the same local CA (`a6g certs init` if it exists, otherwise a self-signed CA created by the harness); mutual TLS if already available, otherwise server authentication on all, recorded. ACP and ANP are out of scope (§A4.12).

**Patterns and payloads.** Payloads are sampled (fixed seed) from the recorded G10 comms logs, so sizes are those of real `AgentMessage`s:

- P1 envelope dispatch with acknowledgement (coordinator → robot), with `nl` policy `full` and `none`;
- P2 report with acknowledgement (robot → coordinator), `nl` `full` and `none`;
- P3 contract-net round: CFP from one robot to its N−1 peers, bids, award and acknowledgement, deadline 1.5 s;
- P4 robot state at 1 Hz (publish/subscribe);
- P6 escalation with a 300 KB JPEG attachment (robot → coordinator);
- P7 human request (robot → coordinator).

**Link conditions.** Profiles `good`, `cell_edge`, `degraded` from `config/network.yaml`, plus two outage tests: the robot link goes down for 10 s and for 60 s during a stream of P2 reports (one every 2 s).

**Repetitions.** P1, P2, P7: 200 messages; P3: 100 rounds; P4: 300 s; P6: 30 messages; each block 3 times, blocks in an order shuffled by seed.

**Metrics** (per message or round):

- wire bytes up and down, including TCP and TLS handshakes and retransmissions, from per-namespace interface counters before and after each block, divided by the count; verify on one block per binding with a packet capture;
- payload bytes;
- completion latency (send to acknowledgement, response or end of round): p50, p95, p99;
- deadline misses (P1 acknowledgement ≥ 2 s; P3 round ≥ 1.5 s), timeouts and retries;
- outage tests: time from link restoration to the first delivered message, messages lost, duplicated and delivered late.

**Output.** `uv run a6g bench protocols --matrix config/bench/protocols.yaml` writes `bench/results/n5_protocols_*.json` and `paper/table_protocols.csv`.

*Accept:*
- The matrix runs unattended and repeat blocks agree within their reported variance.
- Sanity: on `good`, warm A2A P1 latency is close to one RTT plus server time; cold adds at least two RTTs (TCP and TLS) plus the agent-card fetch.
- **Reduced scope if time runs out (stop rule):** `a2a_jsonrpc` cold and warm against `mqtt` with `cbor`, patterns P1, P3 and P6, three profiles, one outage test.

### A4.9 Stage N6: export for the paper

`uv run a6g paper export --out paper/` collects the latest valid results:

- `paper/RESULTS.md`: one table per result with n, median and IQR (or p95 for latencies), units, and the manifest summary (commit, models and fidelity, network mode, coverage source, seeds); results whose commit differs from `HEAD` are flagged (rule 31);
- CSV files: `fig_outage.csv` (N1), `fig_scarcity.csv` (N4), `table_overhead.csv` (N2), `table_protocols.csv` (N5), `table_cost.csv` (M1 rerun offline, cloud and under scarcity; M7 rerun; M2 places and routes; M3 retrieval and leak probes; G10 checks and link recovery time, with N3b if run);
- draft plots for sanity only (the paper figures are drawn from the CSV files).

Then update `PAPER_MEASUREMENTS.md` (new sections for N1–N5, validity column) so it can be uploaded to the project with `RESULTS.md` and the CSV files.

*Accept:* `RESULTS.md` covers every row of the CSV files; no result is missing its manifest.

### A4.10 Schedule and stop rules

| When | Stages |
|---|---|
| Mon 26 Oct | N0: launch M1 (offline and cloud) and M7 (cloud). N1: implement the outage, fleet and metric options, launch the sweep overnight. N2: checkpoint instrumentation |
| Tue 27 Oct | N2 reports on N1 runs. N3 coverage map with `model` (start Monday if time allows); switch to `radio_dt` as soon as UPV delivers the export. Start N4 |
| Wed 28 Oct | N4: finish, tests, G13 sweep, M1 and G10 reruns with the capacity model. N5 |
| Thu 29 Oct, 12:00 | Freeze: N6 export, `PAPER_MEASUREMENTS.md` updated, everything committed |

**Stop rules.**

- If N4 does not pass its acceptance by **Wed 28 Oct, 20:00**: stop N4, commit what exists, report exactly what works (admission alone, loop without relocation, …). The paper then presents RQ3 at the mechanism level.
- If N5 is not complete by **Thu 29 Oct, 09:00**: run the reduced scope of §A4.8 and export.
- N3b, the `zenoh` binding, `--outage-kind edge` and fleet 8 in G13 are optional; skip them before compromising N1, N4 or N5.
- Never change defaults that the existing test suite relies on (`network.mode: always_grant`, `coverage.source: shadows`, `hold_then_recover`); benches select the new modes explicitly.

### A4.11 Changes to existing sections

| Section | Change |
|---|---|
| §5 | `RobotState` gains `link_level` and `cell_id`; `link_quality` is derived from the level |
| §12 | Coverage map with levels (N3); `a6g bench scarcity` (G13); outage and fleet options of `a6g bench baselines` |
| §A1.4 | Campus file gains `network_sites` (the n40 gNB on Building 8G, WGS84, rule 16); new `config/network.yaml` (cell parameters, coverage, link profiles, capacity); the site is drawn in every map view |
| §A1.12.1 | Optional link-loss policy `recover_to_coverage` (N3b) |
| §A2.5.4 | Link levels come from the coverage map; in accelerated mission runs they act on decisions only (rule 33); `netns` emulation is used by N5 |
| §A2.7 | `noa_stub` capacity model implemented, code only for grants (`--stub-llm off` in benches) |
| §A3.7 | Degradation loop, relocation variant and I3 implemented (N4) |
| §A3.8 | UC2-KPI-3 from grants under `network.mode: capacity`; baselines gain outage length and fleet 8; overhead report (N2); paper export (N6) |

### A4.12 Out of scope (this week; candidates for the revision)

- **A packet-level 5G network in the loop.** Simu5G in emulation mode (real agent processes exchange packets through a simulated 5G RAN in real time) or ns-3 5G-LENA. They need mission runs at 1× and several days of setup.
- **The real network in the loop.** Replace the capacity stand-in's grants with quality-of-service-on-demand requests to the campus Network-as-Code server, which is integrated with CAPIF, on the 8G n40 cell. This is the natural next step for the paper's NEF path. Also model the Nexus n40 cell, with a second cell, handover and capacity.
- **ACP** (folded into A2A under the Linux Foundation; its REST style is covered by `a2a_rest`).
- **ANP** (DID-based identity, meta-protocol negotiation, JSON-LD descriptions). Revision candidate: compare onboarding and discovery cost across organizations, A2A agent card with OAuth or mutual TLS against ANP's DID authentication.
- `zenoh` in full missions; Gazebo runs; self-hosted models (C6); the real network and robots (P4b, P4c).

### A4.13 Open items (configuration with safe defaults until answered)

- TODO(UPV): RSRP export of the ray-tracing radio DT of the n40 cell (CSV `lat, lon, cell_id, rsrp_dbm`), promised for Tue 27 Oct.
- TODO(UPV): of the campus cell (§A4.6.1):
  - antenna height on Building 8G, azimuth, tilt and gain;
  - whether 26.5 dBm is the total or the per-port output power;
  - subcarrier spacing;
  - the TDD pattern behind the 3:7 ratio.
- TODO(UPV): the robots' 5G modems. The Go2 kit is a 5G outdoor router over Ethernet: confirm its model, uplink layers and 256QAM uplink support, and what the Summit carries.
- TODO(UPV): whether the Nexus n40 cell (Building 6G) also covers parts of the pilot sector. If it does, add it as a second site and cell (position, PCI); until then it is ignored and the result manifests say so.
- TODO(UPV): the coordinator's edge host (§15). It is not the HPE MEC server of the network document (UPV, 26 Oct); its CPU, memory and GPU are still open. It does not affect the paper runs.
- TODO(UPV): level thresholds and the uplink rates per level, from the uplink survey. The cell capacity is swept in G13, so its default only matters for G10.
- TODO(UPV): root or `CAP_NET_ADMIN` on the workstation for the network namespaces of N5.
- TODO(UPV): whether `recover_to_coverage` becomes the default link-loss policy (after N3b).
