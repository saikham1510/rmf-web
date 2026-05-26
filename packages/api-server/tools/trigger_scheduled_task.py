#!/usr/bin/env python3
"""Trigger a scheduled patrol task via the API for manual testing.

Usage:
    python3 tools/trigger_scheduled_task.py

This will POST a scheduled patrol task for `TinyRobot` with a single schedule
starting now and `planned_end_at` set to 3 minutes from now (local time).
Adjust `API_URL`
if your server is not on localhost:8000.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import requests

API_URL = os.environ.get("API_URL", "http://localhost:8000")

if __name__ == "__main__":
    now_local = datetime.now()
    now_utc = datetime.utcnow().isoformat() + "Z"
    planned_end = (now_local + timedelta(minutes=3)).strftime("%H:%M")

    payload = {
        "task_request": {
            "category": "patrol",
            "description": {
                "places": [
                    {"waypoint": "cleaner_pantry"},
                    {"waypoint": "ceo_room"},
                ],
                "rounds": 50,
            },
            "labels": ["integration_test=scheduled_cancel", "robot=TinyRobot"],
            "fleet_name": "TinyRobot",
        },
        "schedules": [
            {
                "period": "minute",
                "start_from": now_utc,
                "planned_end_at": planned_end,
            }
        ],
    }

    url = f"{API_URL}/scheduled_tasks"
    print(f"Posting scheduled task to {url}\npayload={json.dumps(payload, indent=2)}")
    try:
        resp = requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Request failed: {e}")
        sys.exit(2)

    print("Status:", resp.status_code)
    try:
        print(resp.json())
    except Exception:
        print(resp.text)

    if resp.status_code == 201:
        print(
            "Scheduled task created. Watch server logs and task states for cancellation at planned end."
        )
    else:
        print("Failed to create scheduled task.")
