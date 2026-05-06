import asyncio
import threading
from asyncio import Future
from datetime import datetime, timezone
from typing import Callable, Dict, Optional
from uuid import uuid4

import rclpy
import rclpy.node
import rclpy.qos
from fastapi import HTTPException
from rmf_task_msgs.msg import ApiRequest, ApiResponse

from api_server.logger import logger
from api_server.ros import ros_node as default_ros_node


class RmfService:
    """
    RMF uses a pseudo service protocol implmented using pub/sub. "Calling" a service
    involves publishing a request message with a request id and subscribing to a response.

    Any node can response to the request, so responses may come in out of order and there
    is no guarantee that there will be only one response, clients must keep track of the
    request ids which they published and drop and unknown and duplicated response ids.

    ===== REQUEST_ID LIFECYCLE RULE (STRICT) =====
    Per-execution model ONLY:
      1. request_id is generated ONLY inside call() via uuid4()
      2. request_id exists ONLY during active RMF request/response cycle
      3. _requests stores ONLY in-flight requests (added at line ~69, removed at line ~85)
      4. request_id is NEVER stored for future scheduled execution
      5. request_id is NEVER reused across separate executions
      6. Scheduled and immediate tasks must behave identically at RMF layer

    Invariant: After call() returns (success or timeout), the request_id is deleted
               and must never be accessed again.
    """

    def __init__(
        self,
        ros_node: Callable[[], rclpy.node.Node],
        request_topic: str,
        response_topic: str,
    ):
        self.ros_node = ros_node
        self._logger = logger.getChild(self.__class__.__name__)
        self._requests: Dict[str, Future] = {}
        self._api_pub = self.ros_node().create_publisher(
            ApiRequest,
            request_topic,
            rclpy.qos.QoSProfile(
                depth=10,
                history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
                durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._api_sub = self.ros_node().create_subscription(
            ApiResponse,
            response_topic,
            self._handle_response,
            rclpy.qos.QoSProfile(
                depth=10,
                history=rclpy.qos.HistoryPolicy.KEEP_LAST,
                reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
                durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._request_topic = request_topic
        self._response_topic = response_topic

    def destroy(self):
        """
        Unsubscribes to api responses and destroys all ros objects created by this class.
        """
        self._api_sub.destroy()
        self._api_pub.destroy()

    async def call(self, payload: str, timeout: float = 10) -> str:
        # LIFECYCLE RULE: request_id is generated ONLY here, at execution time
        # Default timeout increased to 10s to allow RMF processing time
        # Late responses after timeout are correctly ignored (logged as "unknown request_id")
        req_id = str(uuid4())
        msg = ApiRequest(request_id=req_id, json_msg=payload)
        fut = Future()

        # LIFECYCLE RULE: store request in active map only during execution
        if req_id in self._requests:
            # This should never happen (uuid4 collision is astronomically rare)
            raise RuntimeError(f"UUID collision detected for request_id: {req_id}")
        self._requests[req_id] = fut

        self._logger.info(
            "publishing RMF request request_id=%s timestamp=%s topic=%s",
            req_id,
            datetime.now(timezone.utc).isoformat(),
            self._request_topic,
        )
        self._api_pub.publish(msg)
        self._logger.info(f"sent request '{req_id}'")
        self._logger.debug(msg)
        try:
            response = await asyncio.wait_for(fut, timeout)
            self._logger.info(
                "RMF call succeeded request_id=%s active_requests=%d",
                req_id,
                len(self._requests),
            )
            return response
        except asyncio.TimeoutError as e:
            self._logger.warning(
                "RMF call TIMED OUT request_id=%s timeout=%s (response may arrive later; "
                "if frequent, increase timeout) active_requests=%d",
                req_id,
                timeout,
                len(self._requests),
            )
            raise HTTPException(500, f"rmf service timed out after {timeout}s") from e
        finally:
            # LIFECYCLE RULE: delete request_id after success, timeout, or exception
            # This ensures request_id is never reused or persisted for future execution
            if req_id in self._requests:
                del self._requests[req_id]
                self._logger.debug(
                    "cleaned up request_id=%s remaining_active=%d",
                    req_id,
                    len(self._requests),
                )

    def _handle_response(self, msg: ApiResponse):
        """
        Handle RMF response message by resolving the corresponding Future.

        This is called by ROS subscription callback for each ApiResponse received.
        The response is matched to the request via request_id lookup in _requests.

        Expected behavior:
        - Response arrives within timeout → Future resolved, entry deleted in finally
        - Response arrives AFTER timeout → request_id already deleted, this is OK
          (late response is correctly ignored; request is no longer relevant)
        """
        self._logger.info(f"got response '{msg.request_id}'")
        self._logger.debug(msg)

        # LIFECYCLE RULE: Only active requests should have entries in _requests
        fut = self._requests.get(msg.request_id)
        if fut is None:
            # This is EXPECTED and CORRECT when:
            # 1. Response arrived after timeout (5s default) and cleanup in finally
            # 2. Duplicate response for same request_id
            # This is NOT a violation of the per-execution rule.
            self._logger.warning(
                "Received response for unknown request_id: %s (likely late response after timeout; "
                "active_requests=%d)",
                msg.request_id,
                len(self._requests),
            )
            return

        # Response matched to active request: resolve Future
        fut.set_result(msg.json_msg)


_tasks_service: Optional[RmfService] = None
_tasks_service_lock = threading.Lock()


def tasks_service() -> RmfService:
    """
    Thread-safe singleton factory for RmfService.

    LIFECYCLE GUARANTEE:
    - Returns the same RmfService instance for all calls in this process
    - All requests use the same _requests dict for tracking active in-flight requests
    - Ensures request_id uniqueness within process scope (uuid4 guarantees global uniqueness)
    - Both scheduled and immediate tasks use the same instance, ensuring identical behavior

    Single-process only: Uvicorn runs with workers=1 (no multi-worker config),
    so singleton is process-scoped and thread-safe.
    """
    global _tasks_service
    if _tasks_service is None:
        with _tasks_service_lock:
            if _tasks_service is None:
                _tasks_service = RmfService(
                    default_ros_node, "task_api_requests", "task_api_responses"
                )
    return _tasks_service
