# Paper fixes, night of 29–30 Oct 2026: report

*From Claude Code to the paper-writing session. Code references are to the `develop` branch
at the commit named in each result's manifest (the same files exist on `main`, which is
the release; line numbers can differ slightly there).*

## Summary

- **A done.** `admission_only` added and all four scarcity variants rerun at one commit
  (480 runs, 2–22 Mbps). It behaves exactly as the cost model predicts: 0/3 posts with
  sensing at 2–8 Mbps, 1/3 (video at the medium post) from 12 Mbps; I1–I3 hold. The other
  variants reproduce the 28 Oct numbers exactly. `paper/fig_scarcity.csv` and
  `paper/RESULTS.md` updated (definitions of the variants and of "usable" included).
- **B1–B4 confirmed**, with two precisions (claims are settled by estimated cost, not
  completion time; deadline misses are 1.5 s for contract-net and 2.0 s for the other
  patterns) and an explanation of the duplicates (robots out of coverage presumed lost).
  **B5 run:** the emulator agrees with the paper's ±3 dB ranges at the sweep resolution.
- **C done:** `paper/fig_campus_map.pdf` / `.png` (one column) and a double-column variant;
  the two low posts relocate about 12 m to medium-coverage viewpoints (5.7 and 2.9 dB).
- **D done** except the GitHub release and the Zenodo DOI, which need your GitHub
  account (steps below): code, `paper/`, the radio DT export and a "Reproducing the paper"
  README section are on `main`, tagged `ieee-network-2026`.
- **Also rerun:** retrieval precision (M3), last run on 20 Oct, before this week's campus
  changes: identical numbers, now with manifests.
- `paper/RESULTS.md` now flags a result only when the code it ran changed after its commit
  (naming the changed parts) and leaves superseded results out (the 18 and 26 Oct M1 and
  order runs were listed before).

## A. Admission-only baseline (Fig. 4)

**Implemented** as `network.reconcile.mode: admission_only` (variant `admission_only` of
`a6g bench scarcity`): the capacity stand-in with admission on, exactly as for `reconcile`
(`packages/a6g_coordinator/src/a6g_coordinator/network.py`, unchanged); in the reconcile
node (`graph.py`, `reconcile`) one request pass, then no switch to telemetry, no further
rounds, no relocation; a post the network cannot serve is held without a stream and
escalated, by the same final step as today, so I1 holds. All four variants were rerun at
one commit (`af40683`), 2–22 Mbps (no 44 Mbps: `paper/fig_scarcity.csv` now holds 2–22
only), fleets 3 and 5, 10 seeds. New test:
`tests/test_capacity_network.py::test_g13_admission_only_refuses_without_adapting`.

**Result: as predicted by the cost model.** `admission_only` gives 0/3 posts with sensing
at 2, 4 and 8 Mbps (all three videos refused: the two low posts for coverage at every
capacity, the medium post for capacity) and 1/3 from 12 Mbps (video at the medium post,
cost 10.9 cell-Mbps), with 3 and then 2 escalations per run, zero I1/I2/I3 violations and
one reconciliation round. `reconcile` gives 1/3, 2/3 and 3/3 posts with sensing at 2, 4
and 8 Mbps and keeps 3/3 above; the other three variants reproduce the 28 Oct results
exactly. So admission control alone never gives the low posts any sensing; the gain of
Fig. 4 comes from reconciliation (degrading video to telemetry where it fits) and, for
video at the low posts, from relocation.

*Result: `bench/results/n4_scarcity_20261029T232612Z.json` (commit `af40683`, dirty False; 480 runs, 0 errors; fake models, up to 200x, `network.mode: capacity`, coverage `radio_dt`; time to reconciled envelopes is wall time with 6 runs in parallel).*


**Fleet 3** (10 seeds; counts are the same in every seed unless shown as a range with *)

| variant | Mbps | posts with video | posts with sensing | escalations per run | I1/I2/I3 | posture change → reconciled s, median (p95) | rounds (max) | oversubscription |
|---|---|---|---|---|---|---|---|---|
| reconcile_off | 2 | 0/3 | 0/3 | 0 | 0/0/0 | 0.086 (0.206) | 1 | 46.635 |
| reconcile_off | 4 | 0/3 | 0/3 | 0 | 0/0/0 | 0.095 (0.165) | 1 | 23.318 |
| reconcile_off | 8 | 0/3 | 0/3 | 0 | 0/0/0 | 0.104 (0.163) | 1 | 11.659 |
| reconcile_off | 12 | 0/3 | 0/3 | 0 | 0/0/0 | 0.120 (0.150) | 1 | 7.773 |
| reconcile_off | 16 | 0/3 | 0/3 | 0 | 0/0/0 | 0.092 (0.118) | 1 | 5.829 |
| reconcile_off | 22 | 0/3 | 0/3 | 0 | 0/0/0 | 0.096 (0.127) | 1 | 4.24 |
| admission_only | 2 | 0/3 | 0/3 | 3 | 0/0/0 | 0.100 (0.117) | 1 |  |
| admission_only | 4 | 0/3 | 0/3 | 3 | 0/0/0 | 0.099 (0.199) | 1 |  |
| admission_only | 8 | 0/3 | 0/3 | 3 | 0/0/0 | 0.106 (0.173) | 1 |  |
| admission_only | 12 | 1/3 | 1/3 | 2 | 0/0/0 | 0.103 (0.211) | 1 |  |
| admission_only | 16 | 1/3 | 1/3 | 2 | 0/0/0 | 0.098 (0.245) | 1 |  |
| admission_only | 22 | 1/3 | 1/3 | 2 | 0/0/0 | 0.086 (0.096) | 1 |  |
| reconcile | 2 | 0/3 | 1/3 | 2 | 0/0/0 | 0.117 (0.256) | 3 |  |
| reconcile | 4 | 0/3 | 2/3 | 1 | 0/0/0 | 0.102 (0.163) | 3 |  |
| reconcile | 8 | 0/3 | 3/3 | 0 | 0/0/0 | 0.113 (0.151) | 2 |  |
| reconcile | 12 | 0/3 | 3/3 | 0 | 0/0/0 | 0.110 (0.145) | 3 |  |
| reconcile | 16 | 0/3 | 3/3 | 0 | 0/0/0 | 0.107 (0.155) | 3 |  |
| reconcile | 22 | 1/3 | 3/3 | 0 | 0/0/0 | 0.098 (0.121) | 2 |  |
| reconcile_relocate | 2 | 0/3 | 1–2*/3 | 1–2* | 0/0/0 | 0.121 (0.163) | 4 |  |
| reconcile_relocate | 4 | 0/3 | 3/3 | 0 | 0/0/0 | 0.127 (0.147) | 3 |  |
| reconcile_relocate | 8 | 0/3 | 3/3 | 0 | 0/0/0 | 0.124 (0.187) | 3 |  |
| reconcile_relocate | 12 | 0/3 | 3/3 | 0 | 0/0/0 | 0.135 (0.210) | 4 |  |
| reconcile_relocate | 16 | 1/3 | 3/3 | 0 | 0/0/0 | 0.121 (0.183) | 4 |  |
| reconcile_relocate | 22 | 1/3 | 3/3 | 0 | 0/0/0 | 0.118 (0.160) | 4 |  |

**Fleet 5** (10 seeds; counts are the same in every seed unless shown as a range with *)

| variant | Mbps | posts with video | posts with sensing | escalations per run | I1/I2/I3 | posture change → reconciled s, median (p95) | rounds (max) | oversubscription |
|---|---|---|---|---|---|---|---|---|
| reconcile_off | 2 | 0/3 | 0/3 | 0 | 0/0/0 | 0.170 (0.284) | 1 | 46.635 |
| reconcile_off | 4 | 0/3 | 0/3 | 0 | 0/0/0 | 0.147 (0.337) | 1 | 23.318 |
| reconcile_off | 8 | 0/3 | 0/3 | 0 | 0/0/0 | 0.157 (0.314) | 1 | 11.659 |
| reconcile_off | 12 | 0/3 | 0/3 | 0 | 0/0/0 | 0.170 (0.280) | 1 | 7.773 |
| reconcile_off | 16 | 0/3 | 0/3 | 0 | 0/0/0 | 0.150 (0.321) | 1 | 5.829 |
| reconcile_off | 22 | 0/3 | 0/3 | 0 | 0/0/0 | 0.147 (0.365) | 1 | 4.24 |
| admission_only | 2 | 0/3 | 0/3 | 3 | 0/0/0 | 0.142 (0.301) | 1 |  |
| admission_only | 4 | 0/3 | 0/3 | 3 | 0/0/0 | 0.161 (0.246) | 1 |  |
| admission_only | 8 | 0/3 | 0/3 | 3 | 0/0/0 | 0.150 (0.256) | 1 |  |
| admission_only | 12 | 1/3 | 1/3 | 2 | 0/0/0 | 0.172 (0.283) | 1 |  |
| admission_only | 16 | 1/3 | 1/3 | 2 | 0/0/0 | 0.154 (0.357) | 1 |  |
| admission_only | 22 | 1/3 | 1/3 | 2 | 0/0/0 | 0.140 (0.187) | 1 |  |
| reconcile | 2 | 0/3 | 1/3 | 2 | 0/0/0 | 0.161 (0.395) | 3 |  |
| reconcile | 4 | 0/3 | 2/3 | 1 | 0/0/0 | 0.157 (0.268) | 3 |  |
| reconcile | 8 | 0/3 | 3/3 | 0 | 0/0/0 | 0.192 (0.311) | 2 |  |
| reconcile | 12 | 0/3 | 3/3 | 0 | 0/0/0 | 0.161 (0.327) | 3 |  |
| reconcile | 16 | 0/3 | 3/3 | 0 | 0/0/0 | 0.193 (0.302) | 3 |  |
| reconcile | 22 | 1/3 | 3/3 | 0 | 0/0/0 | 0.158 (0.235) | 2 |  |
| reconcile_relocate | 2 | 0/3 | 2/3 | 1 | 0/0/0 | 0.188 (0.377) | 4 |  |
| reconcile_relocate | 4 | 0/3 | 3/3 | 0 | 0/0/0 | 0.185 (0.314) | 3 |  |
| reconcile_relocate | 8 | 0/3 | 3/3 | 0 | 0/0/0 | 0.203 (0.375) | 3 |  |
| reconcile_relocate | 12 | 0/3 | 3/3 | 0 | 0/0/0 | 0.185 (0.493) | 4 |  |
| reconcile_relocate | 16 | 1/3 | 3/3 | 0 | 0/0/0 | 0.184 (0.346) | 3 |  |
| reconcile_relocate | 22 | 1/3 | 3/3 | 0 | 0/0/0 | 0.166 (0.309) | 3 |  |


## B. Confirmations

### B1. B1 baseline — confirmed

B1 (`central_memory`) keeps the coordinator's state in memory only: on a restart its
checkpointer is a fresh `MemorySaver` (`packages/a6g_devstack/src/a6g_devstack/stack.py:470`),
so the mission thread starts empty. Robot reports about tasks the coordinator no longer
knows are skipped (`packages/a6g_coordinator/src/a6g_coordinator/graph.py:2500`:
`entry = tasks.get(tid); if entry is None: continue`), and nothing rebuilds the tasks from
reports. "Loses its state with the coordinator and, by design, does not rebuild it from
robot reports" is accurate.

### B2. Claims after an outage — confirmed, with one precision; how duplicates arise

- **Settlement.** When a robot claims a task (a peer award made during the outage, or a
  task it reports running) that the coordinator has given to another active robot,
  `_claim` keeps the robot with the lower estimated cost from where both are now, and drops
  the task from the other (`graph.py:1832–1871`, decision at `:1870`). *Precision:* the
  comparison is on the allocator's cost (travel time, battery, commitment, with the
  allocator weights), not on completion time alone; "the sooner robot keeps the task" is
  right in practice (travel time dominates) but "the cheaper robot by estimated cost" is
  exact.
- **Idempotent awards.** A robot accepts an award once per `award_id`
  (`packages/a6g_robot_agent/src/a6g_robot_agent/graph.py:2125`); resending the same award
  is harmless. Confirmed.
- **How duplicates still arise (N1: up to 1.2 duplicated tasks per mission on average).**
  Award ids are per call for proposals (`award_id = "aw_" + cfp_id`,
  `packages/a6g_robot_agent/src/a6g_robot_agent/runtime.py:1325`), so idempotency protects
  against a repeated award, not against two different awards for one task. Duplicates come
  from work handed away from a robot that is silent but still working:
  - during the outage, the redistribution initiator (the lowest alive robot id in its own
    view, `runtime.py:1229–1259`) treats a peer whose heartbeat has timed out
    (`_peer_alive`, `runtime.py:1216`) as dead and awards its open tasks; a robot that is
    only out of coverage keeps working on them;
  - with the coordinator up, a robot silent for longer than its bound is presumed stuck
    (`robot_lost_long`, `graph.py:1102`) and its work is reassigned, again while it may
    still be working out of coverage.
  Both robots then inspect the point; claims are settled only when the silent robot
  reconnects, often after it has finished. In our N1 runs, all 75 P runs with a duplicate
  had a peer award during the outage, and 50 of them also a `robot_lost_long` release. A
  traced example (fleet 5, 300 s outage, seed 0): Campus east access, which lies in lost
  coverage, was given away twice from robots working there out of contact (08:06:51 by the
  coordinator, 08:11:04 by a peer) and inspected by two robots. Suggested wording: "Awards
  are idempotent and claims are settled by estimated cost when the coordinator returns;
  duplicates remain when a robot that is out of coverage, not failed, is presumed lost and
  its task re-awarded while it keeps working."

### B3. SINR and spectral efficiency — confirmed

- SE is computed on linear SINR: `min(0.6 * log2(1 + 10 ** (sinr_db / 10)), 5.55)`
  (`packages/a6g_common/src/a6g_common/netconfig.py:161–165`).
- SINR = RSRP + 101 dB, clipped to [−10, 26] dB (`config/network.yaml:32`,
  `packages/a6g_common/src/a6g_common/coverage.py:211–213`). Check: the posts' smoothed
  RSRP −102.2 / −92.4 / −102.3 dBm give −1.2 / 8.6 / −1.3 dB.
- Two details: the 5.55 cap is never reached inside the clip (it needs about 27.8 dB), and
  one robot's uplink is also capped at the measured 22 Mbps (`netconfig.py:167–172`).

### B4. Protocol settings (Table II) — recorded in `paper/RESULTS.md` (N5 section)

- **MQTT** (`protolab/endpoint.py:255–269`, reconnect loop `:382–400`): `aiomqtt` 2.5
  (paho), MQTT 3.1.1, amqtt broker at the edge; QoS 1 for requests and replies; clean
  session false (3.1.1 has no session expiry: the broker keeps the session); keepalive
  10 s; connect timeout 10 s; reconnect after a fixed 0.5 s (no exponential backoff);
  TCP_NODELAY.
- **A2A JSON-RPC and REST** (`endpoint.py:315–352`): `a2a-sdk` with `httpx` 0.28, request
  timeout 10 s, no library retries, default pool (no limit, keep-alive expiry 5 s).
  Reused: one client and connection per block, agent card fetched once. New connection: a
  new client, TLS handshake and agent-card fetch per message. Server keep-alive 600 s.
- **gRPC**: `grpc.aio` secure channel with default options: no keepalive pings, no retry
  policy (only gRPC's transparent retry of RPCs never sent).
- **Evidence image**: confirmed. JSON carries the JPEG as base64 text, CBOR as raw bytes
  (`endpoint.py:70`, `:86–105`); P6 wire size on the good link 422.0 kB (MQTT JSON) and
  421.9–428.0 kB (A2A) against 317.0 kB (MQTT CBOR): ratio 1.33, the base64 overhead.
- **Delivery guarantees**: every pattern is request and acknowledgement (one reply per
  request), so every binding acknowledges each message end to end. In the outage tests the
  client resends the same message id every 1 s until acknowledged (`endpoint.py`, outage
  pattern), so delivery is at least once for every binding. Difference: MQTT also
  redelivers by itself (QoS 1, persistent session), which produced its one duplicate per
  outage; the A2A bindings rely on the client's resend only (no duplicates seen).
- **Deadlines**: a contract-net round (P3) misses at ≥ 1.5 s; any other request misses
  when its acknowledgement takes ≥ 2.0 s (`protolab/lab.py:263–264`). The paper's "deadline
  miss (contract-net round over 1.5 s)" is right for P3; say "≥ 2 s to acknowledgement" for
  the other patterns if Table II shows their misses.

### B5. SINR sensitivity (±3 dB)

Run: `reconcile` and `reconcile_relocate`, fleet 5, 10 seeds, 2–22 Mbps, every SINR sample
shifted by −3 dB and by +3 dB (new option `a6g bench scarcity --sinr-shift`, recorded in the
manifest as `sinr_shift_db`; it shifts the SINR where the capacity stand-in reads it:
admission rate and cost, and the viewpoint search; levels, which are by RSRP, do not
move). Results: `bench/results/sensitivity/n4_scarcity_20261029T233002Z.json` (−3 dB) and
`…T233333Z.json` (+3 dB), figure data `paper/fig_scarcity_sinr_m3.csv` and
`paper/fig_scarcity_sinr_p3.csv`; 240 runs, 0 errors, I1/I2/I3 violations 0. Every seed gave
the same counts.

Posts with sensing (video) out of 3, fleet 5:

| Mbps | reconcile −3 dB | reconcile 0 dB | reconcile +3 dB | relocate −3 dB | relocate 0 dB | relocate +3 dB |
|---|---|---|---|---|---|---|
| 2 | 1 (0) | 1 (0) | 1 (0) | 1 (0) | 2 (0) | 2 (0) |
| 4 | 1 (0) | 2 (0) | 2 (0) | 2 (0) | 3 (0) | 3 (0) |
| 8 | 2 (0) | 3 (0) | 3 (0) | 2 (0) | 3 (0) | 3 (0) |
| 12 | 3 (0) | 3 (0) | 3 (0) | 3 (0) | 3 (0) | 3 (1) |
| 16 | 3 (0) | 3 (0) | 3 (1) | 3 (0) | 3 (1) | 3 (1) |
| 22 | 3 (0) | 3 (1) | 3 (1) | 3 (0) | 3 (1) | 3 (1) |

**The emulator agrees with the cost model at the sweep resolution.** Cost model
(`SE(25 dB)/SE(SINR)`): cost factor at the low posts 17.9–18.2 (−3 dB), 10.2–10.4 (0),
6.3 (+3); full sensing needs 11.96 / 7.00 / 4.41 Mbps, and video at the medium post plus
sensing at the other two 25.9 / 17.1 / 12.2 Mbps. Emulator (`reconcile`): full sensing from
12 / 8 / 8 Mbps (the next sweep point above 11.96, 7.0 and 4.41), video at the medium post
never up to 22 / from 22 / from 16 Mbps (above 25.9, 17.1, 12.2). The paper's ranges
(cost factor 6–18, full sensing 4.4–12 Mbps instead of 7, video 12–26 Mbps instead of 17)
are confirmed. Two details: at +3 dB the low posts still cannot carry video in place
(one robot's uplink there is about 3.5 Mbps, below 4 Mbps), and with relocation the
medium-coverage viewpoints make video reachable one sweep step earlier at +3 dB (12 Mbps).
The −3 dB run started at commit `2211f8d`, the +3 dB run at `bef61d3` with the export
module modified; neither change touches the code these runs execute (coordinator, network
stand-in, scenario), which is that of `af40683`.

## C. Campus map (replaces Fig. 2)

- `paper/fig_campus_map.pdf` (vector) and `paper/fig_campus_map.png` (600 dpi): one
  column, 88 × 72 mm, all text 8 pt, no branding. Double-column variant:
  `paper/fig_campus_map_wide.pdf` / `.png` (181 × 110 mm).
- Script: `paper/plot_campus_map.py` (deterministic; reads the campus and coverage files of
  the simulator and the relocation viewpoints of `reconcile_relocate` at 4 Mbps, fleet 5,
  also saved in `paper/relocation_viewpoints.csv`).
- Colours: the live view's hues, stepped for colour-vision deficiencies (good #1B9E77,
  medium #CFAE00, low #D55E00, lost #8E63B8; every adjacent pair passes a CVD separation
  check); footpaths are cased in dark grey so the yellow stays visible on white.
- Draft caption: *"Pilot area of the UPV campus with footpaths coloured by the coverage
  level of the n40 radio digital twin. Labels give each exposed post's level and SINR; at
  4 Mbps, `reconcile_relocate` moves the two low posts about 12 m to medium-coverage
  footpath viewpoints (arrows). The staging area is the only link-recovery point."*

## D. Release

1. **Repository** `https://github.com/xriteamupv/Agentic6G_Swarm` is public. `main` holds
   the code of the results, `paper/` (`RESULTS.md`, every CSV, the campus map and its
   script), the radio DT export (`data/network/n40_centroides_rsrp.csv.gz`, 43 MB) and the
   README section "Reproducing the paper" (environment, one command per artefact with
   runtime and outputs, seeds, configuration, model ids, prompt versions, definitions,
   netem profiles, data). `develop` holds the same plus the development history, raw
   result files and the measurement log.
2. **Data release checks.** Radio DT export: approved for publication by UPV (the author's
   confirmation, 30 Oct); published compressed because the CSV (114 MB) exceeds GitHub's
   100 MB file limit. OSM data keep their ODbL attribution (`data/osm/ATTRIBUTION.md`). No
   secrets (scan for API keys, tokens and private keys: none; only `.env.example` files with
   empty values) and no personal data (all people in the scenarios are simulated; no
   e-mail addresses in the tree).
3. **Tag:** `ieee-network-2026` on `main` (@@MAINCOMMIT@@).
4. **Release and DOI: to do in the GitHub web UI** (the GitHub CLI is not installed here):
   first enable the repository in Zenodo (zenodo.org → GitHub → switch on
   `xriteamupv/Agentic6G_Swarm`), then create the release from the tag (Releases → Draft a
   new release → tag `ieee-network-2026`, title "Code and experiments of the IEEE Network
   paper", notes below). Zenodo archives the release and mints the DOI only if it was
   enabled before the release is published.
   Suggested notes: *"Code, configuration and experiment scripts of the paper submitted to
   IEEE Network (Oct 2026): LangGraph swarm coordinator and robot agents over A2A v1.0 and
   MQTT, 2D campus simulator with UPV's n40 radio digital twin, and the benchmarks behind
   every figure and table (see README, Reproducing the paper). paper/ holds the exported
   results."*

## E. Future work (evaluated, not implemented)

**E1. Restructure around three reader questions** (paper-side). Results that support each:
- *What state and authority should a cognitive twin retain?* Fig. 3 / N1 (P against B0–B2:
  B1 loses the twin and never completes at fleets 3 and 5; B2 keeps it but has no peer
  fallback), G10 checks (decision chain, forgetting of personal data), the checkpoint
  overhead of N2 (coordinator 5594 writes, median 16.9 kB), B1/B2 confirmations above.
- *How are physical tasks made consistent with network resources?* Fig. 4 / N4 with the
  four variants (admission alone against reconciliation and relocation), I1–I3 at zero in
  every run, emergency reaction time under scarcity (M1), the campus map with relocations.
- *What do the experiments show about recovery, feasibility and overhead?* N1 completion,
  time to resume and duplicates; N4 sensing and video against capacity; N2 traffic per
  pattern; Table II latency, deadline misses and outage recovery.

**E2. Full protocol results in the repository.** Effort: about half a day. The data exist:
`paper/table_protocols.csv` already has every block (bindings × modes × encodings ×
levels × patterns P1–P7 × natural language on/off × 3 and 5 robots, plus outages). Work:
a rendered page (Markdown, or an HTML table with filters) generated by `paper_export.py`
next to the CSV, a column dictionary, and a README link. The paper can then keep latency,
deadline misses and outage recovery, and cite the page for payload sizes.

**E3. New failure experiments.** Shared design: a fault injector in the simulated
deployment (`a6g_devstack/stack.py`, `bench_baselines.py` as the template: seeded fault
time and duration, many seeds, outcomes from the simulator's own records). Metrics:
divergence between robots' envelopes and the twin (tasks owned by different robots, or
open in one and done in the other), ownership conflicts and duplicate executions on
reconnection, time from recovery to a consistent state (first reconciled publication with
no divergence), and mission completion.
- *Twin-store loss* (2–3 days): wrap the coordinator's checkpointer
  (`a6g_observability/checkpoint_meter.py` already wraps it) to fail writes and reads for a
  period (Postgres down) or to restore an older checkpoint (roll the mission thread back by
  N steps); the coordinator must keep running from memory or rebuild from robot reports.
  Touches `a6g_coordinator/persistence.py`, `graph.py` (behaviour when a checkpoint write
  fails; resync from robot envelopes, which today only B1-style restarts exercise).
- *Edge partition* (3–5 days): stop the MQTT broker and the knowledge server, not the
  coordinator process. Today robots detect peers and the coordinator through broker
  heartbeats and last-wills (`a6g_robot_agent/runtime.py`, `_peer_alive`), so peers would
  need an A2A heartbeat between them (`a6g_transport`), and the knowledge client a cached
  fallback (`a6g_kb/client.py`). This is the largest change.
- *Network partition* (2–3 days): isolate a subset of robots from the edge and from some
  peers. The simulator already decides peer reachability from coverage
  (`a6g_sim/world.py`, peer links); a partition matrix injected per run, with the
  contract-net initiator rule (`runtime.py:1229`) as the main point to study (two
  partitions each elect an initiator and award the same tasks).

## Contradictions with the paper (Sections V-B to V-D)

Nothing contradicts the claims listed in the brief, but these points need a check
against the text (I do not have Sections V-B to V-D here):
1. **Claims after an outage (B2):** settled by the lower *estimated cost* (allocator cost
   function), not strictly by who finishes sooner.
2. **Duplicates:** if the text attributes them to the claim settlement or to award
   handling, it should instead say that they come from robots out of coverage presumed
   lost (by peers during the outage, or by the coordinator after the silence bound) while
   they keep working.
3. **Deadline miss:** 1.5 s applies to contract-net rounds (P3) only; other patterns miss
   at 2.0 s to acknowledgement.
4. **Fig. 4 without 44 Mbps:** the 28 Oct text mentioned video at 2/3 posts at 44 Mbps
   with relocation; that point is no longer in the figure. Up to 22 Mbps, video is
   reached at one post: from 22 Mbps with `reconcile`, from 16 Mbps with relocation.
5. **Admission alone:** if the text says admission control alone gives "partial
   sensing", it does not: it gives none up to 8 Mbps and only the medium post's video
   from 12 Mbps.
6. **M3 retrieval** numbers are unchanged by the rerun (P@1 0.712 offline, 0.861 with the
   embedder model).
