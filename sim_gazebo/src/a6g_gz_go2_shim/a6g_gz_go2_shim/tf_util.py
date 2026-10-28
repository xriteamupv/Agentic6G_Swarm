"""tf2 helpers shared by the cloud nodes."""

import numpy as np
from geometry_msgs.msg import TransformStamped
from sensor_msgs.msg import PointCloud2, PointField
from sensor_msgs_py import point_cloud2

from a6g_gz_go2_shim.logic import matrix_from_tq


def matrix(t: TransformStamped) -> np.ndarray:
    tr, q = t.transform.translation, t.transform.rotation
    return matrix_from_tq((tr.x, tr.y, tr.z), (q.x, q.y, q.z, q.w))


def xyz(cloud: PointCloud2) -> np.ndarray:
    pts = point_cloud2.read_points_numpy(cloud, field_names=("x", "y", "z"), skip_nans=True)
    return np.asarray(pts, dtype=np.float64).reshape(-1, 3)


XYZ_FIELDS = [
    PointField(name="x", offset=0, datatype=PointField.FLOAT32, count=1),
    PointField(name="y", offset=4, datatype=PointField.FLOAT32, count=1),
    PointField(name="z", offset=8, datatype=PointField.FLOAT32, count=1),
]


def make_cloud(header, points: np.ndarray) -> PointCloud2:
    data = np.ascontiguousarray(points, dtype=np.float32)
    return PointCloud2(
        header=header,
        height=1,
        width=len(data),
        fields=XYZ_FIELDS,
        is_bigendian=False,
        point_step=12,
        row_step=12 * len(data),
        data=data.tobytes(),
        is_dense=True,
    )
