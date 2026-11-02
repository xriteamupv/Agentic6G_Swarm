# Moving to the workstation (SPEC A5.2, stage W0)

Steps in order. The laptop part produces one private folder; everything else comes from
GitHub or is rebuilt. Commands assume Ubuntu 24.04 and the same layout as the laptop
(`~/LangChain/a6g-swarm`); a different user or folder works too, but read the note on the
Claude Code memory (step 9).

## 1. On the laptop: export the local-only state

```bash
cd ~/LangChain/a6g-swarm
scripts/migrate/export_local_state.sh ~/a6g-migration --with-runs
```

`~/a6g-migration` then holds the local backup branches (`backups.bundle`), `.env`
(API keys), Claude Code's project memory and the run directories the overhead bench
reads (`runs.tgz`, about 1.2 GB). Copy the folder to the workstation (`scp -r`, `rsync`
or a USB disk) and keep it private: it contains API keys.

## 2. System packages

```bash
sudo apt update && sudo apt install -y git curl build-essential iproute2 openssl \
    python3-vcstool python3-rosdep docker.io
sudo usermod -aG docker "$USER"   # log out and in again
```

- **NVIDIA driver and CUDA** (local models, Gazebo rendering): the driver from Ubuntu's
  "Additional Drivers" or `ubuntu-drivers install`; check with `nvidia-smi`.
- **uv:** `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Node.js 20+ and pnpm** (chat UI): e.g. from NodeSource, then `corepack enable`.

## 3. ROS 2 Jazzy and Gazebo Harmonic

Follow the official ROS 2 Jazzy installation for Ubuntu 24.04 (`ros-jazzy-desktop`), then:

```bash
sudo apt install -y ros-jazzy-ros-gz ros-jazzy-navigation2 ros-jazzy-nav2-bringup \
    ros-jazzy-robot-localization
sudo rosdep init 2>/dev/null; rosdep update
echo "source /opt/ros/jazzy/setup.bash" >> ~/.bashrc
```

## 4. The repository

```bash
mkdir -p ~/LangChain && cd ~/LangChain
git clone git@github.com:xriteamupv/Agentic6G_Swarm.git a6g-swarm   # needs your SSH key on GitHub
cd a6g-swarm
git checkout develop
git config user.name robotic_ai && git config user.email xr@iteam.upv.es   # commit identity
git fetch ~/a6g-migration/backups.bundle 'refs/heads/*:refs/heads/*'          # local backups
install -m 600 ~/a6g-migration/secrets/.env .env
uv sync --locked
```

Never push the `backup/*` branches.

## 5. The Robotnik sources and the Gazebo workspace

```bash
mkdir -p ~/robotnik_ws/src && cd ~/robotnik_ws/src
vcs import robotnik < ~/LangChain/a6g-swarm/sim_gazebo/robotnik.repos
cd ~/robotnik_ws && rosdep install --from-paths src --ignore-src -y -r
colcon build --symlink-install
echo "source ~/robotnik_ws/install/setup.bash" >> ~/.bashrc

cd ~/LangChain/a6g-swarm/sim_gazebo
source /opt/ros/jazzy/setup.bash && source ~/robotnik_ws/install/setup.bash
rosdep install --from-paths src --ignore-src -y -r
colcon build --symlink-install
cd .. && env -u PYTHONPATH uv run a6g campus build      # Gazebo world, Nav2 maps, 2D-sim files
```

With ROS sourced in the shell, prefix the project's commands with `env -u PYTHONPATH`.

## 6. Postgres, the run directories and the radio DT export

```bash
docker run -d --name a6g-pg --restart unless-stopped -e POSTGRES_USER=a6g \
    -e POSTGRES_PASSWORD=a6g -e POSTGRES_DB=a6g -p 127.0.0.1:5433:5432 postgres:16
tar -xzf ~/a6g-migration/runs.tgz -C ~/LangChain/a6g-swarm          # var/runs/...
gunzip -k sim_gazebo/data/network/n40_centroides_rsrp.csv.gz          # only to rebuild the map
```

## 7. The chat UI

```bash
cp chat-ui/.env.example chat-ui/.env
deploy/ui/build.sh                       # then: PORT=3000 var/chat-ui/start.sh
```

## 8. Checks (acceptance of stage W0)

```bash
env -u PYTHONPATH uv run a6g config validate
env -u PYTHONPATH uv run pytest                       # about 12 min on the laptop
env -u PYTHONPATH uv run pytest --gazebo              # the Gazebo suite
env -u PYTHONPATH uv run a6g bench scarcity --variants reconcile --fleets 3 --seeds 2 \
    --runs var/runs/w0_check --out var/w0_check --csv var/w0_check/fig.csv
```

The last command must reproduce the committed numbers for `reconcile`, fleet 3 (compare
with `paper/fig_scarcity.csv`), and its manifest must name the workstation as host. Then
start the 2D demo (README, "Running the demo") and the Gazebo stack (`uv run a6g-gz-stack`).
The protocol bench needs unprivileged user namespaces (`unshare --user --map-root-user`);
if Ubuntu's AppArmor restriction blocks it, see SPEC A4.13 (root or `CAP_NET_ADMIN`).

## 9. Claude Code's memory

Claude Code keeps the project's memory under `~/.claude/projects/<folder>/memory`, where
`<folder>` is the directory Claude Code is started from with `/` replaced by `-` (on the
laptop: `-home-iterate-LangChain`). Start Claude Code from the same folder on the
workstation and unpack the memory there:

```bash
D=~/.claude/projects/$(echo ~/LangChain | tr '/' '-')/memory
mkdir -p "$D" && tar -xzf ~/a6g-migration/claude-memory.tgz -C "$D"
```

## 10. Afterwards

Delete `~/a6g-migration` on both machines once the checks pass (it holds API keys). Keep
the laptop's clone until the workstation has produced its first results.
