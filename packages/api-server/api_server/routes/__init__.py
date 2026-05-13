try:
    from .admin import router as admin_router
    from .alerts import router as alerts_router
    from .beacons import router as beacons_router
    from .building_map import router as building_map_router
    from .dispensers import router as dispensers_router
    from .doors import router as doors_router
    from .fleets import router as fleets_router
    from .ingestors import router as ingestors_router
    from .internal import router as internal_router
    from .lifts import router as lifts_router
    from .main import router as main_router
    from .tasks import *
except ImportError as e:
    print(f"WARNING: Could not import route modules: {e}. Running in degraded mode.")
    # Create dummy routers
    admin_router = None  # type: ignore
    alerts_router = None  # type: ignore
    beacons_router = None  # type: ignore
    building_map_router = None  # type: ignore
    dispensers_router = None  # type: ignore
    doors_router = None  # type: ignore
    fleets_router = None  # type: ignore
    ingestors_router = None  # type: ignore
    internal_router = None  # type: ignore
    lifts_router = None  # type: ignore
    main_router = None  # type: ignore
    tasks_router = None  # type: ignore
    favorite_tasks_router = None  # type: ignore
    scheduled_tasks = None  # type: ignore
