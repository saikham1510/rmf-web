try:
    from .alerts import AlertRepository, alert_repo_dep
    from .cached_files import CachedFilesRepository, cached_files_repo
    from .fleets import FleetRepository, fleet_repo_dep
    from .rmf import RmfRepository, rmf_repo_dep
    from .tasks import TaskRepository, task_repo_dep
except ImportError as e:
    print(f"WARNING: Could not import repositories: {e}. Running in degraded mode.")
    AlertRepository = None  # type: ignore
    alert_repo_dep = None  # type: ignore
    CachedFilesRepository = None  # type: ignore
    cached_files_repo = None  # type: ignore
    FleetRepository = None  # type: ignore
    fleet_repo_dep = None  # type: ignore
    RmfRepository = None  # type: ignore
    rmf_repo_dep = None  # type: ignore
    TaskRepository = None  # type: ignore
    task_repo_dep = None  # type: ignore
