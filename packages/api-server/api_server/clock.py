import time

from .ros import ros_node


def now() -> int:
    """
    Return current unix time in millis
    """
    node = ros_node()
    if node is None:
        # ROS not available, fallback to system time
        return int(time.time() * 1000)
    ros_time = node.get_clock().now()
    return ros_time.nanoseconds // 1000000
