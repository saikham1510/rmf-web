try:
    from .book_keeper import RmfBookKeeper, RmfBookKeeperEvents
    from .events import (
        RmfEvents,
        TaskEvents,
        alert_events,
        beacon_events,
        fleet_events,
        rmf_events,
        task_events,
    )
    from .health_watchdog import HealthWatchdog
    from .rmf_service import RmfService, tasks_service
    from .topics import topics

    HAS_RMF_IO = True
except ImportError as e:
    print(f"WARNING: Could not import RMF IO modules: {e}. Running in degraded mode.")
    HAS_RMF_IO = False
    # Create dummy objects to prevent import errors
    RmfBookKeeper = None  # type: ignore
    RmfBookKeeperEvents = None  # type: ignore
    RmfEvents = None  # type: ignore
    TaskEvents = None  # type: ignore
    alert_events = None  # type: ignore
    beacon_events = None  # type: ignore
    fleet_events = None  # type: ignore
    rmf_events = None  # type: ignore
    task_events = None  # type: ignore
    HealthWatchdog = None  # type: ignore
    RmfService = None  # type: ignore
    tasks_service = None  # type: ignore
    topics = None  # type: ignore
