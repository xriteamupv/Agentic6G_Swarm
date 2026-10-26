cmake_minimum_required(VERSION 3.8)
project(a6g_gz_robots)
find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)

ament_python_install_package(${PROJECT_NAME})
install(DIRECTORY models DESTINATION share/${PROJECT_NAME})
install(PROGRAMS scripts/summit_urdf DESTINATION lib/${PROJECT_NAME})
ament_environment_hooks("${CMAKE_CURRENT_SOURCE_DIR}/env-hooks/${PROJECT_NAME}.dsv.in")

if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(test_summit_urdf test/test_summit_urdf.py)
  ament_add_pytest_test(test_go2_model test/test_go2_model.py)
endif()

ament_package()
