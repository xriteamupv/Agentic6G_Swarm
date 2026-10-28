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
- [Replicating the experiments](#replicating-the-experiments)
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

## Replicating the experiments

Every experiment runs on the simulator with fixed random seeds, and writes a JSON result to
`bench/results/`. Each result carries a manifest: the commit, configuration hashes, seeds,
models, network mode and host. The 5G network is in the loop through emulation built on
measured data: the coverage map of UPV's calibrated radio digital twin, the cell's measured
uplink capacity and per-level link profiles applied with `tc netem`.

**One command for everything:**

```bash
docker run -d --name a6g-pg -e POSTGRES_USER=a6g -e POSTGRES_PASSWORD=a6g -e POSTGRES_DB=a6g \
    -p 127.0.0.1:5433:5432 postgres:16   # once: the database for the G10 run
bench/run_final.sh          # all phases: A offline, B cloud models, C protocols, then the export
bench/run_final.sh A        # only the offline experiments (no API key needed)
```

The script runs the steps one after another so that timing measurements never share the
machine, and keeps the laptop awake. It writes a log per step to `var/logs/final/` and a
status line per step to `var/logs/final/status.txt`. Finally it exports the paper's tables
and figure data to `paper/` (`RESULTS.md` plus CSV files).

**Or run the experiments one by one** (times measured on the machine above):

| Experiment | What it shows | Command | Time |
|---|---|---|---|
| Coordinator outages | Missions complete when the coordinator fails for 60 s to forever, against three centralised baselines; fleets of 3, 5 and 8 | `uv run a6g bench baselines --jobs 6` | 1.5 h |
| Network scarcity | An emergency over a cell of 2 to 44 Mbps: admission control, degradation and relocation for coverage, against no control | `uv run a6g bench scarcity --jobs 6` | 10 min |
| End-to-end scenario (G10) | Patrol, an escort with a wheelchair user, a weather alert, link loss and the all-clear, with consistency checks; 10 seeds | `uv run a6g bench g10 --seeds 10 --out var/runs/g10_n2 --network capacity --checkpoint-meter --persistence postgres --pg-dsn postgresql://a6g:a6g@127.0.0.1:5433/a6g` | 7 min |
| Emergency reaction time | From the alert to new orders published and acknowledged by every robot | `uv run a6g bench kpi11 --seeds 20` (add `--network capacity`, or `--model --robot-model` for gpt-5-nano) | 3–10 min |
| Agent traffic and memory overhead | Messages, bytes per interaction pattern, model calls and checkpoint sizes (reads the two runs above, so run it after them) | `uv run a6g bench overhead --persistence postgres` | < 1 min |
| Knowledge server | Place resolution, step-free routes and task planning with and without the campus knowledge server | `uv run a6g bench kb` (`--model --embedder model` with gpt-5-nano) | < 1–8 min |
| Goal decomposition | Exact plans for 45 goals: routine, interactive, emergency, paraphrased, ambiguous, off-map and prompt-injected | `uv run a6g bench decompose` (`--model --embedder model`) | < 1–4 min |
| Unseen operator orders | 24 orders never used in development, rules-first against model-first understanding | `uv run a6g bench orders` (needs an API key) | 15 min |
| Agent protocols over radio links | A2A (JSON-RPC, REST, gRPC) and MQTT over emulated good, medium and low 5G links, and outages | `uv run a6g bench protocols` (`--reduced` for a short run) | 3 h 15 min |
| Paper tables | `paper/RESULTS.md` and CSV files from the latest results | `uv run a6g paper export` | seconds |

Notes:
- **Offline runs are deterministic.** They use the same seeds and a fake model, so they
  reproduce our numbers up to timing noise.
- **Runs with gpt-5-nano vary from one run to the next.** For example, the share of unseen
  orders understood ranged from 62 % to 75 % over three runs. Repeat them, and report the
  spread.
- **The protocol benchmark sends real traffic in real time** through rootless Linux network
  namespaces with `tc netem`. It needs `unshare`, `ip`/`tc` (iproute2) and `openssl`, but
  no sudo. Run it on an otherwise idle machine.
- **Our own results** are in `bench/results/`. The log of the final run, including checks
  and corrections, is in [bench/PAPER_MEASUREMENTS.md](bench/PAPER_MEASUREMENTS.md).
  Commit IDs recorded before 28 Oct 2026 are mapped to the published history in
  [bench/COMMIT_MAP.md](bench/COMMIT_MAP.md).

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
