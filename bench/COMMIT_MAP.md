# Commit map (history rewrite, 28 Oct 2026)

On 28 Oct 2026 the history was rewritten to set the author e-mail and drop tool attribution lines from
commit messages; file contents are unchanged. Commit IDs recorded before that (result manifests in
`bench/results/` and `bench/PAPER_MEASUREMENTS.md`) are the **old** IDs; this table gives the
commit with the same content in the published history.

| old | new | subject |
|---|---|---|
| `63aeb2b` | `005811d` | Snapshot before SPEC A4 (paper results: network in the loop) |
| `6fdd63f` | `365aa36` | A4 rule 31: result manifests in every bench |
| `04bda5f` | `9c178c8` | A4 N1: outage sweep, fleet 8, resume/duplicate/traffic metrics; idle robots bid in contract-net, untaken tasks retried, no redistribution out of coverage, sim peer links honour coverage |
| `9f18407` | `9a93e18` | A4 N2: checkpoint meter, coordinator model-call records, sim time on comms records, overhead report; run start commit in manifests |
| `0614a32` | `2aebdf7` | A4 N1: one subprocess per run (row.json, resumable); A4 N3: network.yaml, gNB site and velodrome loop in the campus |
| `722847e` | `73b5548` | A4 N3: coverage levels from the network twin (38.901 UMa + velodrome calibration, radio-DT import), link_level/cell_id, hysteresis and dwell, a6g network, view and editor markers |
| `9a60588` | `8f43707` | A4 N4: capacity network stand-in (admission, best effort, release, re-evaluation, fair share), degradation loop in reconcile, grants in envelopes and I3 budget records, G13 and a6g bench scarcity, KPI-3 from grants |
| `d85ec20` | `46e997d` | A4: --network/--coverage options for kpi11 and g10 |
| `8b634e9` | `5fa45cc` | A4: G10 resets the Postgres twin between seeds |
| `8389d9e` | `55ab8b1` | A4 N5: protocol micro-benchmark over emulated radio links (rootless netns + netem; A2A JSON-RPC cold/warm, REST, gRPC; MQTT JSON/CBOR; TLS 1.3; P1-P4, P6, P7, outages; capture check); MQTT clients set TCP_NODELAY |
| `35b9460` | `913ddef` | A4 N6: a6g paper export (RESULTS.md, figure and table CSVs, possibly-stale flags), draft plots, export tests |
| `4c034a3` | `bf76036` | SPEC: merge Part A4; README: the network in the loop and the paper results |
| `52cb238` | `12e39c8` | A4: grants cached in the robot runtime (an await before each skill shifted G11's timing); rule-22 test exempts the protocol bench harness |
| `ed46f4b` | `957b31a` | A4 N3: coverage over the whole pilot area (outdoor grid, 4 m), footpath samples kept for robots on paths; heatmap in the live view and preview |
| `faf2219` | `72904d9` | A4 N3: 3D buildings for line of sight (OSM heights), gNB height from its rooftop, 2 m area grid drawn as areas only; robots report RSRP and SINR (live view, next to battery) |
| `baaf819` | `5834e7a` | A4 N3: fully emulated coverage (no shadow polygons; model default), one sector at 300 deg (38.901 pattern), antenna set back on the roof, tree-lined north-west walk, offset fitted to plotted min/median/max; levels good/medium/low/lost; injected outage zones for G10 and tests; posts held without sensing when no stream fits (I1) |
| `081c835` | `55f8c54` | A4 N3: radio DT export path (sim_gazebo/data/network), import-coverage defaults to it |
| `96aa4e3` | `7ec77ea` | A4 N3: coverage from UPV's radio DT (definitive); fixes before the final measurements |
| `c77bb48` | `eadd5b8` | Live-test fixes (2026-10-27): patrols, contact loss, charging, 5-8 robots, orders, view |
| `53dcba4` | `55f0787` | Analysis explains allocations: candidates (each robot's cost or reason) in allocate decisions, how each open task was given; answers the question asked, no mismatch talk unless an order is not followed |
| `8400afb` | `cc7442f` | PAPER_MEASUREMENTS: final run section (in progress) |
| `51463f3` | `ae13744` | Reconcile: an escort the network cannot serve at all (shelter in lost coverage) goes on state-only and is escalated, like a post (G10 seeds 3, 5, 9 broke I1 on the radio DT map) |
| `88e8100` | `01c232c` | PAPER_MEASUREMENTS: N4, G10 (failed, fixed), KPI-11 offline results |
| `4245464` | `15ca933` | PAPER_MEASUREMENTS: which commit N1 records |
| `e1f3610` | `86eb122` | Reconcile (relocate variant): the move to a viewpoint does not use up the degradation loop's rounds (N4 at 12 Mbps ended every post without a stream, seed-dependent) |
| `7046911` | `cc6677b` | PAPER_MEASUREMENTS: N4 anomaly explained and fixed |
| `8eca74c` | `0dd134a` | PAPER_MEASUREMENTS: N1, M2 and M4 offline results |
| `bbc4152` | `1f321dd` | PAPER_MEASUREMENTS: N2 (provisional), phase B started |
| `3f7bba8` | `f7a5386` | PAPER_MEASUREMENTS: KPI-11 with cloud models |
| `ac22fde` | `d1e90d8` | PAPER_MEASUREMENTS: KPI-11 cloud under scarcity |
| `8a75765` | `dbeb973` | PAPER_MEASUREMENTS: KB ablation with cloud models |
| `2c8d806` | `f26a61c` | PAPER_MEASUREMENTS: KPI-9 with cloud models |
| `428d176` | `4ac7a22` | PAPER_MEASUREMENTS: orders run 1 |
| `874a45f` | `502a6b2` | PAPER_MEASUREMENTS: orders, 3 runs |
| `9586a8f` | `2d944fa` | PAPER_MEASUREMENTS: N5 and N6 export |
| `1423d61` | `2b232fa` | PAPER_MEASUREMENTS: N4, G10, N2 reruns; final export |
| `dc3e9fc` | `7f73fd7` | bench/results: raw result files of the final measurement run (27-28 Oct 2026) |
| `a1d582b` | `791e35a` | Add Apache-2.0 license |
