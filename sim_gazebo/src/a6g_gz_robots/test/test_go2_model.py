import xml.etree.ElementTree as ET
from pathlib import Path

MODEL = Path(__file__).resolve().parents[1] / "models" / "go2_proxy" / "model.sdf"


def test_go2_proxy_matches_spec():
    model = ET.parse(MODEL).getroot().find("model")
    link = model.find("link")
    assert link.get("name") == "base_link"
    assert link.find("gravity").text == "false"  # kinematic proxy: no legged dynamics
    size = [float(v) for v in link.find("collision/geometry/box/size").text.split()]
    assert size == [0.70, 0.31, 0.40]
    sensors = {s.get("name"): s for s in link.findall("sensor")}
    lidar = sensors["utlidar"].find("lidar/scan")
    assert int(lidar.find("horizontal/samples").text) == 360
    assert int(lidar.find("vertical/samples").text) == 18
    assert sensors["utlidar"].find("gz_frame_id").text == "utlidar_lidar"
    img = sensors["camera"].find("camera/image")
    assert (img.find("width").text, img.find("height").text) == ("640", "480")
    assert {"imu", "navsat"} <= sensors.keys()
    plugins = {p.get("name") for p in model.findall("plugin")}
    assert "gz::sim::systems::VelocityControl" in plugins
    assert "gz::sim::systems::OdometryPublisher" in plugins
