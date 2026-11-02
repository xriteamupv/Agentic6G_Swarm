# The workstation as a GPU node (SPEC A5.2, stage W0)

The laptop stays the main development machine. The workstation (Windows 11) runs only the
GPU-demanding work, inside WSL2 (Ubuntu 24.04):

- Gazebo with GPU rendering: rendered cameras and VLM triage, the Gazebo test suite, LiDAR
  and camera recording;
- local models served for the laptop (llama.cpp or vLLM, OpenAI-compatible API);
- codec experiments on recorded LiDAR.

Everything else (2D simulator, other benches, cloud-model runs, protocol lab, Postgres,
chat UI, publishing to `main`) stays on the laptop. Code moves through git (`develop`);
recorded datasets stay on the workstation and are copied when needed.

## 1. Windows side (PowerShell as administrator)

1. **NVIDIA driver:** install the current Windows driver for the GPU (it includes WSL
   support). Never install an NVIDIA driver inside WSL.
2. **WSL2 with Ubuntu 24.04:**
   ```powershell
   wsl --install -d Ubuntu-24.04
   wsl --update
   ```
3. **WSL settings:** create `%UserProfile%\.wslconfig`:
   ```ini
   [wsl2]
   networkingMode=mirrored   # WSL services reachable on the LAN at the Windows address
   dnsTunneling=true
   firewall=true
   memory=32GB               # adjust: by default WSL takes half of the RAM
   processors=12             # adjust to the CPU
   ```
   Mirrored networking needs Windows 11 22H2 or later. Apply with `wsl --shutdown`.
4. **Firewall:** let the laptop reach the model servers and SSH inside WSL (ports 8080
   llama.cpp, 8000 vLLM, 2222 SSH):
   ```powershell
   New-NetFirewallHyperVRule -Name a6g-wsl -DisplayName "a6g WSL services" -Direction Inbound `
       -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 8080,8000,2222
   ```
   If the laptop still cannot connect, also add an ordinary inbound rule for the same ports
   in Windows Defender Firewall (private network profile).
5. **No sleep during long runs:** `powercfg /change standby-timeout-ac 0`.
6. **Address:** `ipconfig` shows the workstation's LAN address; with mirrored networking
   WSL uses the same one.

## 2. Inside WSL (Ubuntu 24.04)

```bash
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[boot]
systemd=true
EOF
# then, in PowerShell: wsl --shutdown, and reopen Ubuntu
sudo apt update && sudo apt install -y git curl build-essential cmake iproute2 \
    python3-vcstool python3-rosdep openssh-server mesa-utils
nvidia-smi                    # must list the GPU (provided by the Windows driver)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**CUDA toolkit** (to build llama.cpp with CUDA, or for PyTorch/vLLM wheels that need it):
install NVIDIA's *WSL-Ubuntu* CUDA repository and the `cuda-toolkit-12-x` package only,
never the `cuda` or `cuda-drivers` meta-packages (they pull a Linux driver).

**SSH from the laptop** (for `rsync` of datasets), on port 2222 so it does not clash with
Windows:
```bash
sudo sed -i 's/^#\?Port .*/Port 2222/' /etc/ssh/sshd_config
sudo systemctl enable --now ssh
# on the laptop: ssh-copy-id -p 2222 <user>@<workstation>; rsync -e "ssh -p 2222" ...
```

## 3. The repository (in the WSL file system, not under /mnt/c)

```bash
mkdir -p ~/LangChain && cd ~/LangChain
git clone git@github.com:xriteamupv/Agentic6G_Swarm.git a6g-swarm   # add this WSL's SSH key to GitHub
cd a6g-swarm && git checkout develop
git config user.name robotic_ai && git config user.email xr@iteam.upv.es
uv sync --locked
```

Copy `.env` from the laptop only if cloud models are used on the workstation (e.g. VLM
triage with a cloud model): `scp ~/LangChain/a6g-swarm/.env` to the same place, mode 600.

## 4. ROS 2 Jazzy, Gazebo Harmonic and the Gazebo workspace

Install ROS 2 Jazzy (`ros-jazzy-desktop`) following the official instructions for Ubuntu
24.04, then:

```bash
sudo apt install -y ros-jazzy-ros-gz ros-jazzy-navigation2 ros-jazzy-nav2-bringup \
    ros-jazzy-robot-localization
sudo rosdep init 2>/dev/null; rosdep update
echo "source /opt/ros/jazzy/setup.bash" >> ~/.bashrc

mkdir -p ~/robotnik_ws/src && cd ~/robotnik_ws/src
vcs import robotnik < ~/LangChain/a6g-swarm/sim_gazebo/robotnik.repos   # pinned versions
cd ~/robotnik_ws && rosdep install --from-paths src --ignore-src -y -r && colcon build --symlink-install
echo "source ~/robotnik_ws/install/setup.bash" >> ~/.bashrc

cd ~/LangChain/a6g-swarm/sim_gazebo && source ~/.bashrc
rosdep install --from-paths src --ignore-src -y -r && colcon build --symlink-install
cd .. && env -u PYTHONPATH uv run a6g campus build
```

With ROS sourced in the shell, prefix the project's commands with `env -u PYTHONPATH`.

**GPU rendering in WSL:** `glxinfo -B` must report the D3D12 driver on the NVIDIA GPU
(WSLg). If it reports `llvmpipe` (software rendering), set `export GALLIUM_DRIVER=d3d12`
and check again; Gazebo's GUI then opens through WSLg. Rendering sensors (cameras, GPU
LiDAR) in a server-only run (`gz sim -s`) may need the GUI session or a recent Mesa; check
it with the sensor test of step 6 before recording data.

## 5. Local models for the laptop

llama.cpp with CUDA (GGUF models):
```bash
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp && cd ~/llama.cpp
cmake -B build -DGGML_CUDA=ON && cmake --build build -j --config Release
build/bin/llama-server -m <model>.gguf --host 0.0.0.0 --port 8080 -ngl 99
```
or vLLM (`vllm serve <model> --host 0.0.0.0 --port 8000`) in its own environment.

From the laptop, check and use it through the endpoint variables of `config/models.yaml`:
```bash
curl http://<workstation>:8080/v1/models
A6G_COORDINATOR_LLM_URL=http://<workstation>:8080/v1 A6G_COORDINATOR_LLM_MODEL=<model> \
    env -u PYTHONPATH uv run a6g bench decompose --model
```
Record the host, runtime, model file and quantisation with every result (SPEC rule 37).

## 6. Checks (acceptance of stage W0)

In WSL:
```bash
nvidia-smi
env -u PYTHONPATH uv run a6g config validate
env -u PYTHONPATH uv run pytest -m "not scenario"
env -u PYTHONPATH uv run pytest --gazebo          # the Gazebo suite
```
plus one rendered camera frame and one GPU LiDAR scan saved from the Go2 model in Gazebo.
From the laptop: a request to the model served on the workstation, and the coordinator
benches (`a6g bench decompose`, `a6g bench orders`) run against it.

## 7. Working across the two machines

- Commit and push from either machine to `develop` (same identity); pull before starting
  on the other one. Publishing to `main` and tags is done from the laptop.
- Datasets recorded on the workstation stay there (`~/LangChain/a6g-swarm/var/`); copy
  results to the laptop with `rsync -e "ssh -p 2222"` when needed. Result files that the
  paper or the benches use are committed as usual.
- ROS 2 runs on the workstation only: the laptop never joins its DDS domain.
