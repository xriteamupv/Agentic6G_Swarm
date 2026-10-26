from setuptools import find_packages, setup

package_name = "a6g_gz_go2_shim"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="UPV iTEAM",
    maintainer_email="robotic_ai@iteam.upv.es",
    description="Go2 dock interfaces around the Gazebo go2_proxy",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            f"{name} = {package_name}.{name}:main"
            for name in (
                "cmd_vel_to_sport_sim",
                "sport_api_sim",
                "odom_to_tf",
                "cloud_to_scan",
                "leg_odom_sim",
                "cloud_deskew_sim",
            )
        ],
    },
)
