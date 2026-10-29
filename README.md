# Agentic6G Swarm: an agentic robot swarm over 5G

A fleet of autonomous robots patrols a university campus, guides visitors and reacts to
emergencies, coordinated by **AI agents** that talk over a **5G network**. An operator gives
orders in plain language ("Patrol the campus", "Send all Go2 to base", "Why is summit_01 at
the bus stop?"). A coordinator agent at the network edge turns them into tasks and hands
them out, and an agent on each robot decides how to carry them out. The robots keep working
when the network or the coordinator fails.

The swarm runs in a 2D simulation of the pilot area on the campus of the Universitat
Politècnica de València (UPV). Its 5G coverage comes from UPV's radio digital twin of the
campus cell, calibrated with measurements on the campus. This repository holds the code, the simulator and the benchmarks behind our
IEEE Network submission (see [Citation](#citation)).

![The 2D view during a patrol](docs/img/2d-view-patrol.png)
*Five robots on the UPV campus. Three patrol their zones and two wait at the staging area.
The coloured map is the 5G coverage; go2_02 has just entered a lost-coverage zone.*

## Contents

- [What the swarm can do](#what-the-swarm-can-do)
- [Architecture](#architecture)
- [The 2D view](#the-2d-view)
- [Installation](#installation)
- [Running the demo](#running-the-demo)
- [Reproducing the paper](#reproducing-the-paper)
- [Repository layout](#repository-layout)
- [Future work](#future-work)
- [Citation](#citation) · [Acknowledgments](#acknowledgments) · [License](#license)

## What the swarm can do

- **Plain-language operation.** Goals ("patrol the campus"), orders by name, type or count
  ("the summit holds the bus stop, two robots patrol the south plaza"), status questions,
  and explanations of any decision. Requests are understood by an LLM (gpt-5-nano by default)
  together with deterministic rules that the model cannot override; for example, an emergency
  is never downgraded.
- **Planning and fair allocation.** Goals are broken into tasks (patrols, guard posts,
  escorts, inspections). Tasks are assigned by an optimal (Hungarian) assignment over travel
  time, battery, commitments and 5G coverage, one priority level at a time.
- **Autonomous robots.** Each robot agent accepts or refuses tasks against its own state,
  orders its work, starts its next patrol lap by itself and goes to charge when its battery
  runs low. When it is idle it asks for work and returns to the staging area.
- **Emergencies first.** A weather alert pauses routine patrols, sends robots to guard the
  exposed points and ends escorts at the nearest shelter. The new orders go out within a
  fraction of a second.
  After the operator's "all clear", the routine work resumes.
- **Robust to failures.** A robot that loses 5G keeps working, queues its reports and looks
  for coverage. If it stays silent for too long, its work goes to others. If the
  coordinator fails, the robots share out open tasks among themselves (contract-net) and
  finish the mission.
- **Network-aware.** Coverage levels come from the radio digital twin. In capacity mode the
  network can refuse streams, and the coordinator then degrades them (video to telemetry) or
  moves a guard post to a nearby spot with better coverage.
- **Guiding people.** A robot escorts a visitor along a route that respects what the visitor
  said (for example, step-free for a wheelchair). It uses a campus knowledge server for place
  names and route statuses, and forgets personal details when the escort ends.
- **Traceable.** Every decision, whether made by code, a model or the operator, is logged
  with its inputs and reasons. The coordinator answers "why?" from that log.

## Architecture

```mermaid
flowchart TB
    op([Operator]) -->|chat| ui["Chat UI<br/>(agent-chat-ui)"]
    ui -->|HTTP, streamed runs| coord

    subgraph edge["Edge server"]
        coord["<b>Swarm Coordinator</b><br/>LangGraph agent<br/>understand → plan → allocate → publish"]
        kb["Campus knowledge server<br/>(MCP)"]
        net["5G network stand-in<br/>coverage map · admission control"]
        log[("Decision log")]
        coord --- kb
        coord --- net
        coord --- log
    end

    coord <-->|"A2A v1.0: task envelopes, replies, reports"| r1
    coord <-->|A2A v1.0| r2
    r1 -.->|"MQTT: state, heartbeats"| coord
    r1 <-.->|"A2A contract-net<br/>(coordinator down)"| r2

    subgraph robot1["Robot (one per robot)"]
        r1["<b>Robot agent</b><br/>LangGraph agent<br/>evaluate → sequence → execute → report"]
        s1["Skill server (MCP)<br/>navigate · follow_route · hold_post · inspect · ..."]
        r1 -->|MCP| s1
    end
    r2["Robot agent ..."]

    s1 --> w["2D simulator  ·  Gazebo  ·  real robot (ROS 2, Nav2)"]
```

Each layer owns its own decisions:

| Layer | Decides | Built with |
|---|---|---|
| **Swarm Coordinator** (edge) | *what* is done and *by whom*: understands the operator, plans tasks, allocates them, reacts to events, explains | LangGraph graph served by the LangGraph Agent Server; an LLM for language, code for allocation |
| **Robot agents** (one per robot, independent processes) | *whether* and *in which order*: accepts or refuses tasks, sequences them, handles battery, link loss and coordinator outages | LangGraph graph with a local SQLite checkpointer, A2A server |
| **Skill layer** | *how* the robot moves: nine safe skills with preconditions and time budgets (never an LLM) | MCP server, backed by the 2D simulator, or by Nav2 through a ROS adapter in Gazebo and on the real robots |

The agents are separate processes that communicate over standard protocols: **A2A v1.0**
(`a2a-sdk` 1.2) for tasks and reports, **MQTT** for telemetry and heartbeats, and **MCP**
for skills and the knowledge server. Every message is recorded with its size and timing.

A typical mission:

```mermaid
sequenceDiagram
    actor O as Operator
    participant C as Coordinator
    participant R as Robot agent
    participant S as Skills
    O->>C: "Patrol the campus"
    C->>C: plan 3 patrols, allocate (travel, battery, coverage)
    C->>R: envelope: tasks, priority, budgets, network grants
    R->>R: evaluate against own state
    R-->>C: accept
    R->>S: follow_route (lap 1)
    S-->>R: lap done
    R->>S: follow_route (lap 2, started by the robot itself)
    Note over R: 5G lost: keep patrolling,<br/>queue reports, seek coverage
    R-->>C: queued reports (coverage back)
    O->>C: weather alert
    C->>R: new envelope: hold post at the bus stop (emergency)
    R-->>C: accept, patrol paused
```

## The 2D view

The simulator serves a live map at `http://127.0.0.1:8300/view` (drag to pan, scroll to
zoom, double-click to fit). The screenshot below is from the same run as the one at the
top, after a weather alert.

![The 2D view during a weather alert](docs/img/2d-view-weather-alert.png)
*Weather alert: go2_03, go2_04 and summit_01 leave their patrols to guard the three exposed
points (Building 9B door, Bike rack, Bus stop), and go2_01 stays at staging. go2_02 is out of
coverage in the east, so it has not heard the alert yet: it keeps patrolling and will
receive its new orders when it regains coverage.*

| On the map | Meaning |
|---|---|
| Grey blocks | Buildings (OpenStreetMap) |
| Shaded cells | 5G coverage from the radio digital twin, by received power (RSRP): **good** (green, > −90 dBm), **medium** (yellow, −100 to −90), **low** (orange, −110 to −100), **lost** (purple, < −110: no link) |
| Dashed yellow outline | The pilot area |
| Solid / dashed / dotted black lines | The three patrol zones (North-west walk, South plaza, East corridor), with their waypoints |
| Blue dots · orange dots | Points of interest · exposed points (guarded during a weather alert) |
| Green square | Staging area: where robots wait, charge and recover their link |
| Red triangle | The n40 5G base station on Building 8G |
| Brown lines | Footpaths (OpenStreetMap), which robots use between zones |
| Coloured markers and trails | The robots, in colours distinct from the coverage colours |

Each robot card on the right shows:
- the robot's state (still, moving, link lost);
- its current task, prefixed by the mission's posture (`rou` routine, `int` interactive,
  `eme` emergency);
- reports queued while offline;
- the signal it measures (RSRP, SINR and coverage level);
- its battery.

The operator talks to the coordinator through the chat UI in [`chat-ui/`](chat-ui/), our
modified version of LangChain's [agent-chat-ui](https://github.com/langchain-ai/agent-chat-ui):
it shows the swarm's own updates live, and every step of the coordinator as a tool call with
its result:

<img src="docs/img/operator-chat.png" alt="The operator chat during the weather alert" width="700">

## Installation

Tested on Ubuntu 24.04 (x86-64, 8 cores, 16 GB RAM).

- [uv](https://docs.astral.sh/uv/) installs Python 3.11 and every dependency.
- An OpenAI API key is optional: without one, the coordinator uses deterministic templates.
- Docker is needed only for the Postgres-backed benchmark.
- Node.js (20 or later) with pnpm is needed only for the chat UI.

```bash
git clone https://github.com/xriteamupv/Agentic6G_Swarm.git
cd Agentic6G_Swarm
uv sync                                  # creates .venv with Python 3.11
cp example.env .env                      # then put your OPENAI_API_KEY in .env (optional)
uv run a6g config validate               # checks every configuration file
uv run pytest -m "not scenario"          # without the scenario tests (about 9 minutes)
uv run pytest                            # all tests (about 12 minutes); never calls a model
```

> If you have a ROS workspace sourced in your shell, prefix every command with
> `env -u PYTHONPATH` so that ROS's Python packages do not leak into the project's
> environment.

### The operator chat UI

The chat UI in `chat-ui/` runs as a production build (a self-contained Next.js server):

```bash
cp chat-ui/.env.example chat-ui/.env     # points the UI at http://localhost:2024, assistant `coordinator`
deploy/ui/build.sh                       # installs its dependencies and builds it into var/chat-ui/
```

Rebuild after changing the UI or its `.env`: the coordinator's URL is fixed at build time
(set `NEXT_PUBLIC_API_URL=http://<edge-host>:2024` to use it from another machine). To run
it as a systemd user service, see [deploy/ui/README.md](deploy/ui/README.md). `pnpm dev`
inside `chat-ui/` is only for working on the UI itself: it is much slower on long mission
threads.

### Editing and building the campus map

The campus is defined in `config/campus/upv_vera_cpi.yaml`: points of interest, staging
area, link-recovery points, patrol zones and the 5G site, in WGS84. Buildings and footpaths
come from the OpenStreetMap extract in `sim_gazebo/data/osm/`.

```bash
uv run a6g campus edit       # editor in the browser (http://127.0.0.1:8350): patrol zones,
                             # move POIs, the staging area and recovery points, then save
uv run a6g campus zones      # check the patrol zones: length, footpaths, survey route
uv run a6g campus build      # regenerate the files derived from the campus
uv run a6g config validate   # check the result
```

The editor saves to the campus file and keeps a backup of the previous version.
`campus build` regenerates the Gazebo world, the Nav2 prior map and the 2D simulator's obstacles and route graph in
`sim_gazebo/src/a6g_gz_worlds/`. Restart the simulated fleet to load the changes.

The 5G coverage map (`sim_gazebo/src/a6g_gz_worlds/sim/`) is built from UPV's radio digital twin export with
`uv run a6g network import-coverage` (the export itself is UPV data and is not included).
`uv run a6g network coverage --preview cov.html` summarises the coverage levels per patrol
zone and point of interest, and draws the map.

## Running the demo

Use three terminals in the repository folder:

```bash
# 1. The simulated campus and fleet (5 robots, 10x faster than real time)
uv run a6g-sim-stack --speed 10 --robots 5
#    live view: http://127.0.0.1:8300/view

# 2. The Swarm Coordinator on the LangGraph Agent Server (:2024)
uv run a6g dev serve                     # A6G_COORDINATOR_LLM=off uv run ... without an API key

# 3. Talk to it, from the command line ...
uv run a6g goal "Patrol the campus"
#    ... or from the chat UI (built once, see Installation):
PORT=3000 var/chat-ui/start.sh           # then open http://localhost:3000
```

Things to try in the chat, one message at a time:

- *Patrol the campus*
- *Escort the visitor to the bus stop*
- *status*
- *Send all Go2 to base*
- *Why is summit_01 patrolling the south plaza?*

Faults and events can be injected from another terminal:

```bash
uv run a6g sim inject weather_alert              # emergency: guard the exposed points
uv run a6g sim inject link_loss go2_02 60        # a robot loses 5G for 60 s
uv run a6g sim inject robot_fail go2_01          # a robot breaks down
uv run a6g sim inject anomaly poi_kiosk "unattended backpack"   # the operator is asked to decide
```

Stopping terminal 2 (Ctrl+C) simulates a coordinator failure, and the robots carry on by
themselves. `uv run a6g dev reset` clears all saved state between sessions.

## Reproducing the paper

Every experiment runs on the simulator with fixed seeds and writes a JSON result to
`bench/results/`, with a manifest: commit and dirty flag, command, configuration hashes,
seeds, models and their fidelity, network mode, coverage source, time base and host. The 5G
network is in the loop through emulation built on measured data: the coverage map of
UPV's radio digital twin (calibrated with measurements on the campus), the cell's measured
uplink capacity (22 Mbps, one device, 20 MHz) and per-level link profiles applied with
`tc netem`. Our raw result files are in `bench/results/`, the log of the final run (checks and corrections included) in [bench/PAPER_MEASUREMENTS.md](bench/PAPER_MEASUREMENTS.md), and commit IDs recorded before 28 Oct 2026 are mapped to the published history in [bench/COMMIT_MAP.md](bench/COMMIT_MAP.md).

**Environment.** Ubuntu 24.04, x86-64, 8 cores, 16 GB RAM; Python 3.11 from `uv` with the
locked dependencies (`uv sync --locked`); Docker for one Postgres container; for the
protocol benchmark, rootless Linux network namespaces (`unshare`, `ip`/`tc` from iproute2,
`openssl`; no sudo). Postgres, once:

```bash
docker run -d --name a6g-pg -e POSTGRES_USER=a6g -e POSTGRES_PASSWORD=a6g -e POSTGRES_DB=a6g \
    -p 127.0.0.1:5433:5432 postgres:16
```

**Everything in one command:** `bench/run_final.sh` (phases `A` offline, `B` cloud models,
`C` protocols, then the export; `bench/run_final.sh A` needs no API key). It runs the steps
one after another, so that timing measurements never share the machine, keeps the machine
awake, and logs each step to `var/logs/final/`.

**One command per artefact** (times on the machine above):

| Paper artefact | Command | Time | Output |
|---|---|---|---|
| Fig. 3: coordinator outages, P against B0–B2, fleets 3/5/8, 10 seeds | `uv run a6g bench baselines --jobs 6` | 1.5 h | `bench/results/n1_outage_*.json`, `paper/fig_outage.csv` |
| Fig. 4: emergency under scarcity, 2–22 Mbps, fleets 3/5, 10 seeds; `reconcile_off`, `admission_only`, `reconcile`, `reconcile_relocate` | `uv run a6g bench scarcity --jobs 6` | 17 min | `bench/results/n4_scarcity_*.json`, `paper/fig_scarcity.csv` |
| SINR sensitivity (±3 dB) of Fig. 4 | `uv run a6g bench scarcity --jobs 6 --sinr-shift -3 --variants reconcile,reconcile_relocate --fleets 5 --runs var/runs/n4_sinr_m3 --out bench/results/sensitivity --csv paper/fig_scarcity_sinr_m3.csv` (and `+3`) | 5 min each | as named |
| Campus map (Fig. 2) | `uv run python paper/plot_campus_map.py` (after Fig. 4) | seconds | `paper/fig_campus_map.pdf`, `.png` |
| Table II: agent protocols over emulated links | `uv run a6g bench protocols` (`--reduced` for a short run) | 3 h 15 min | `bench/results/n5_protocols_*.json`, `paper/table_protocols.csv` |
| Scenario G10 (consolidated run, invariants, checkpoints) | `uv run a6g bench g10 --seeds 10 --out var/runs/g10_n2 --network capacity --checkpoint-meter --persistence postgres --pg-dsn postgresql://a6g:a6g@127.0.0.1:5433/a6g` | 7 min | `var/runs/g10_n2/` |
| Traffic and checkpoint overhead (after G10 and Fig. 3) | `uv run a6g bench overhead --persistence postgres` | < 1 min | `bench/results/n2_overhead_*.json`, `paper/table_overhead.csv` |
| Emergency reaction time (M1) | `uv run a6g bench kpi11 --seeds 20` (`--network capacity`; `--model --robot-model` for gpt-5-nano) | 3–10 min | `bench/results/e5_kpi11_*.json` |
| Knowledge server (M2) | `uv run a6g bench kb` (`--model --embedder model`) | < 1–8 min | `bench/results/e6_kpi10_*.json` |
| Retrieval precision (M3) | `uv run a6g bench retrieval` (`--embedder model`) | < 1–5 min | `bench/results/e6_kpi15_*.json` |
| Goal decomposition | `uv run a6g bench decompose` (`--model --embedder model`) | < 1–4 min | `bench/results/e6_kpi9_*.json` |
| Unseen operator orders (M7) | `uv run a6g bench orders` (API key; we ran it three times) | 15 min | `bench/results/orders_unseen_*.json` |
| `paper/RESULTS.md` and the CSV files | `uv run a6g paper export` | seconds | `paper/` |

**Seeds, configuration, models, prompts.** Seeds are the benches' defaults (10 per cell for
Figs. 3 and 4, 20 for M1, 50 for M3, seed 26 and 3 repeats for Table II). The configuration
is the repository's `config/`: `network.yaml` (cell, coverage levels, uplink model, link
profiles, flow classes), `bench/protocols.yaml` (Table II matrix), `campus/upv_vera_cpi.yaml`
and `sim/fleet.yaml`. Cloud runs use `gpt-5-nano` for the coordinator and robot roles and
`openai:text-embedding-3-small` for the embedder (`config/models.yaml`); robot prompts are
versioned in `prompts/onboard_vlm/*.md` (front matter `version`), and the coordinator's
prompts live in its code. **Offline runs with fake models reproduce every architecture
result** (Figs. 3 and 4, invariants, checkpoints, traffic) up to timing noise; runs with
gpt-5-nano need an API key in `.env` and vary between runs (unseen orders understood: 62–75 %
over three runs), so repeat them and report the spread.

**Definitions.**
- *Mission completion*: all six inspection points inspected within the horizon, from the
  simulator's own skill records (never from the coordinator's state).
- *Time to resume*: from the robot failure to the first of its open inspections started by
  another robot.
- *Usable sensing*: a post's video or `telemetry_plus` flow granted by admission; without
  admission (`reconcile_off`), its weighted fair share of the cell reaches the class rate
  (video 4 Mbps, `telemetry_plus` 0.3 Mbps; video is never adapted).
- *Oversubscription*: summed cost of the active streams over the cell capacity, with cost
  `rate x SE(anchor) / SE(SINR)`, `SE(SINR) = min(0.6 log2(1 + SINR), 5.55)` (SINR linear),
  and SINR = RSRP + 101 dB clipped to [−10, 26] dB.
- *Deadline miss* (Table II): a contract-net round (P3) of 1.5 s or more; any other request
  whose acknowledgement takes 2.0 s or more.

**Link profiles (netem, per coverage level).** Uplink: the capacity model's rate at the
level's lower SINR edge.

| Level | Uplink | Downlink | RTT | Jitter | Loss |
|---|---|---|---|---|---|
| good | 9.97 Mbps | 100 Mbps | 19 ms (measured) | 5 ms | 0.1 % |
| medium | 3.11 Mbps | 20 Mbps | 45 ms | 15 ms | 1 % |
| low | 0.45 Mbps | 5 Mbps | 80 ms | 30 ms | 3 % |

Only the good-level round-trip time is measured (n40, 9.76 ms downlink + 8.89 ms uplink);
the other delays, jitters and losses are assumptions.

**Data.** `sim_gazebo/data/network/n40_centroides_rsrp.csv.gz` is UPV's radio digital twin export (n40 RSRP,
0.3 m above ground). The coverage maps derived from it are committed; to rebuild them,
`gunzip -k sim_gazebo/data/network/n40_centroides_rsrp.csv.gz` and run `uv run a6g network import-coverage`.
Buildings and footpaths are OpenStreetMap data (ODbL, see the attribution file next to the
extract). Message traces are written by every run (`comms.jsonl` in its run directory: one
line per inter-agent message, with pattern, binding, sizes and timings); they contain
simulated data only.

## Repository layout

| Path | Contents |
|---|---|
| `packages/a6g_coordinator` | The Swarm Coordinator (LangGraph graph, A2A gateway, telemetry) |
| `packages/a6g_robot_agent` | The robot agent |
| `packages/a6g_allocator` | The shared cost function and the task allocator |
| `packages/a6g_skill_mcp` · `a6g_ros_adapter` | Skill server (MCP) and its ROS 2 / Nav2 adapter |
| `packages/a6g_transport` | Messaging over A2A and MQTT, with traffic measurement |
| `packages/a6g_kb` · `a6g_interactions` | Campus knowledge server, and the guidance dialogue (forgets personal data) |
| `packages/a6g_sim` · `a6g_devstack` | The 2D simulator, the simulated deployment and the benchmarks |
| `packages/a6g_cli` | The `a6g` command |
| `chat-ui/` | The operator chat (modified agent-chat-ui, Next.js) |
| `config/` | Campus, fleet, robots, models, network and coverage configuration |
| `sim_gazebo/` | Gazebo worlds and ROS 2 bring-up for the Go2 and Summit-XL |
| `bench/` · `paper/` | Benchmark inputs and results · exported tables and figure data |
| `tests/` | Unit and scenario tests |
| `docs/IMPLEMENTATION.md` | Detailed implementation notes, every command and option |

## Future work

The following parts are started or planned but are **not** used for the results above:

- **Gazebo simulation.** A multi-robot Gazebo Harmonic stack already runs the same agents on
  Unitree Go2 and Robotnik Summit-XL models (Nav2, GNSS localisation). Still missing:
  - calibration against the physical robots;
  - modelled GNSS degradation near buildings (positioning is ideal RTK today);
  - rendered-camera perception;
  - running the network experiments in Gazebo.
- **Physical robots.** Deployment on the real Go2 (Jetson dock) and Summit-XL on the campus,
  as systemd services. The ROS adapter and the real-robot profiles exist but are not yet
  validated on the hardware.
- **On-board vision-language models.** Perception triage is designed around an on-board VLM
  with confirmation at the edge, plus a benchmark script for the Jetson. In the 2D simulator
  the camera is a placeholder and anomalies are injected. Self-hosted models replacing the
  cloud LLM are also future work.
- **Live network.** Running the experiments over the campus 5G network itself, with the
  network's exposure API in place of the admission stand-in, and the delay, jitter and loss
  of every coverage level taken from on-site measurements.
- **Flood risk twin and governance hardening.** These cover:
  - a flood model for the exposed points;
  - tamper-evident logs;
  - access control on shared memory;
  - mutual TLS between agents;
  - Helm-based edge deployment (the Docker Compose files in `deploy/` are untested).
- **Indoor operation.** Floors, lifts and indoor maps are out of scope; the use case is outdoor.

## Citation

The paper describing this work has been submitted to *IEEE Network*.

## Acknowledgments

This work was partially supported by the Smart Networks and Services Joint Undertaking (SNS JU)
under the European Union's Horizon Europe research and innovation programme through Agentic6G
project under Grant Agreement No. 101290342.

## License

Apache License 2.0, see [LICENSE](LICENSE). Copyright 2026 Universitat Politècnica de València.
`chat-ui/` is derived from LangChain's agent-chat-ui and keeps its MIT licence ([chat-ui/LICENSE](chat-ui/LICENSE)).
