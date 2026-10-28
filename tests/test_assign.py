"""Hungarian allocation."""

from a6g_allocator import VirtualState, assign
from a6g_common.schemas import Pose2D
from conftest import inspect_poi, return_home, survey


def fleet(cfg, **poses):
    """robot_id -> VirtualState at (x, y) with full battery."""
    return {rid: VirtualState(Pose2D(x=x, y=y), 100.0) for rid, (x, y) in poses.items()}


def manifests(cfg):
    m = dict(cfg.manifests)
    m["go2_02"] = m["go2_01"].model_copy(update={"robot_id": "go2_02"})
    return m


def test_each_task_goes_to_the_closest_capable_robot(cfg) -> None:
    robots = fleet(cfg, go2_01=(-75, 40), go2_02=(-25, -25), summit_01=(55, 12))
    tasks = [survey("sec_north"), survey("sec_south"), survey("sec_east")]
    result = assign(tasks, robots, manifests(cfg), cfg.campus, cfg.allocator)
    assert {a.task_id: a.robot_id for a in result.assignments} == {
        "t_survey_sec_north": "go2_01",
        "t_survey_sec_south": "go2_02",
        "t_survey_sec_east": "summit_01",
    }
    assert result.unallocated == {} and result.rounds == 1


def test_deterministic_and_ties_go_to_lowest_robot_id(cfg) -> None:
    robots = fleet(cfg, go2_02=(0, 0), go2_01=(0, 0))  # identical robots, same place
    runs = [
        assign([inspect_poi("poi_kiosk")], robots, manifests(cfg), cfg.campus, cfg.allocator)
        for _ in range(5)
    ]
    assert {r.assignments[0].robot_id for r in runs} == {"go2_01"}


def test_more_tasks_than_robots_uses_rounds(cfg) -> None:
    robots = fleet(cfg, go2_01=(0, 0))
    tasks = [inspect_poi("poi_kiosk"), inspect_poi("poi_library_door"), survey("sec_south")]
    result = assign(tasks, robots, manifests(cfg), cfg.campus, cfg.allocator)
    assert len(result.assignments) == 3 and result.rounds == 3
    assert [a.round for a in result.assignments] == [1, 2, 3]


def test_capability_battery_and_exclusions(cfg) -> None:
    robots = fleet(cfg, summit_01=(0, 0), go2_01=(0, 0))
    escort = inspect_poi("poi_kiosk").model_copy(update={"required_capabilities": ["escort"]})
    result = assign([escort], robots, manifests(cfg), cfg.campus, cfg.allocator)
    assert result.assignments[0].robot_id == "go2_01"  # summit has no escort capability

    result = assign(
        [escort],
        robots,
        manifests(cfg),
        cfg.campus,
        cfg.allocator,
        excluded={(escort.task_id, "go2_01")},
    )
    assert result.assignments == [] and escort.task_id in result.unallocated

    weak = {"go2_01": VirtualState(Pose2D(x=0, y=0), 26.0)}
    result = assign([survey("sec_east")], weak, manifests(cfg), cfg.campus, cfg.allocator)
    assert result.unallocated == {"t_survey_sec_east": "battery"}


def test_pinned_tasks_stay_with_their_robot(cfg) -> None:
    robots = fleet(cfg, go2_01=(50, 50), go2_02=(0, 0))
    home = return_home("t_home_go2_01")
    result = assign(
        [home], robots, manifests(cfg), cfg.campus, cfg.allocator, pinned={home.task_id: "go2_01"}
    )
    assert [(a.task_id, a.robot_id) for a in result.assignments] == [("t_home_go2_01", "go2_01")]


def _hold(poi: str):
    from a6g_common.schemas import PoiTarget, Posture, TaskKind
    from a6g_coordinator.decompose import make_task

    return make_task(TaskKind.HOLD_POST, PoiTarget(poi_id=poi), Posture.EMERGENCY)


def test_a_robot_holds_one_post_at_a_time(cfg) -> None:
    """A hold lasts until its deadline: a second post queued behind it is never covered."""
    robots = fleet(cfg, go2_01=(0, 0), summit_01=(1, 0))
    posts = [_hold("poi_bike_rack"), _hold("poi_library_door"), _hold("poi_bus_stop")]
    result = assign(posts, robots, manifests(cfg), cfg.campus, cfg.allocator)
    by_robot = result.by_robot()
    assert all(len(v) == 1 for v in by_robot.values()) and len(by_robot) == 2
    assert len(result.unallocated) == 1  # waits for a free robot
    held = assign(posts[:1], robots, manifests(cfg), cfg.campus, cfg.allocator, holding={"go2_01"})
    assert held.by_robot() == {"summit_01": [posts[0].task_id]}
