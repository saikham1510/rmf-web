#!/bin/bash
# Source ROS and rmf setup to ensure all dependencies are available
source /opt/ros/humble/setup.bash
source ~/rmf_ws/install/setup.bash

# Navigate to api-server directory
cd ~/rmf-web-humble/packages/api-server

# Run uvicorn
exec env PYTHONPATH=. python3 -m uvicorn api_server.app:app --host 0.0.0.0 --port 8000


