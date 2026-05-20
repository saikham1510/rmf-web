#!/usr/bin/env python3
import os
import sqlite3
from pathlib import Path


def main():
    here = Path(__file__).resolve().parent.parent
    default_db = here / "run" / "db.sqlite3"
    db_path = Path(os.environ.get("DB_PATH", str(default_db))).resolve()

    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return 1

    con = sqlite3.connect(str(db_path))
    try:
        cols = [
            r[1]
            for r in con.execute(
                "PRAGMA table_info('scheduledtaskschedule')"
            ).fetchall()
        ]
        if "next_run_at" in cols:
            print("'next_run_at' already present; nothing to do.")
            return 0
        con.execute(
            "ALTER TABLE scheduledtaskschedule ADD COLUMN next_run_at TIMESTAMP;"
        )
        con.commit()
        print("Added 'next_run_at' column to scheduledtaskschedule.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
