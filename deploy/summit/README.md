# Summit-XL deployment notes

## Reversing: short, slow, never blind

The Summit's rear ~96° is a LiDAR blind spot (GPS mast). It may reverse only within the
manifest's `reverse_limits` (`config/robots/summit_01.yaml`): **0.15 m/s, 0.5 m per move**.
Three layers enforce this; the same settings apply in Gazebo and on the real robot.

1. **Controller** — Nav2 MPPI (`sim_gazebo/src/a6g_gz_bringup/config/nav2/summit.yaml` on the develop branch):
   `vx_min: -0.15`; `PreferForwardCritic` weight 50 (stock 5) with
   `threshold_to_consider: 0.5`, so reversing is only worth it for final corrections within
   0.5 m of a goal; `CostCritic` with `consider_footprint: true` rejects any trajectory,
   reverse ones included, that the costmap (static map + obstacles seen on the way in)
   marks as a collision. Velocity smoother `min_velocity[0]: -0.15`.
2. **Base-side governor** — `reverse_governor` (package `a6g_gz_bringup`) sits between
   twist_mux and the base driver (`cmd_vel_muxed` → `robotnik_base_control/cmd_vel_unstamped`).
   It caps backward speed and, once one continuous reverse move reaches 0.5 m on wheel
   odometry, zeroes backward commands until the robot has driven 0.2 m forward. This holds
   even when every forward option is blocked (a dead end), where MPPI alone would back out.
3. **Behaviour trees** — `bt/` keeps every `BackUp` node removed, and `behavior_server`
   loads no BackUp / DriveOnHeading plugin: no multi-metre recovery reversing.

The ros_adapter never publishes velocities (except the stop Twist), so none of this is in
the adapter.

**Still to do on the real robot (UPV / Robotnik):**
- confirm 0.15 m/s and 0.5 m are acceptable with the robot's safety configuration;
- insert `reverse_governor` between twist_mux and the base controller (or configure the
  equivalent limit in the Robotnik controller), and switch the controller to the MPPI block
  above;
- apply the BT diff in `bt/` to the trees the Summit uses today.
