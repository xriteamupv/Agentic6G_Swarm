from glob import glob

from setuptools import find_packages, setup

package_name = "a6g_gz_bringup"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/launch", glob("launch/*.py")),
        (f"share/{package_name}/config/nav2", glob("config/nav2/*.yaml")),
        (f"share/{package_name}/config/bt", glob("config/bt/*.xml")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="UPV iTEAM",
    maintainer_email="robotic_ai@iteam.upv.es",
    description="Agentic6G UC2 Gazebo bring-up",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            f"{name} = {package_name}.{name}:main"
            for name in (
                "gt_localizer",
                "loc_eval",
                "velocity_monitor",
                "twist_mux_lite",
                "reverse_governor",
                "campus_markers",
                "nav_check",
                "gnss_noise_sim",
                "gz_control",
            )
        ],
    },
)
