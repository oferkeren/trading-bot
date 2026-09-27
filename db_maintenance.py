import os
import shutil
import sqlite3

from datetime import (
    datetime,
    timezone,
    timedelta
)

from dotenv import load_dotenv


load_dotenv()


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

DB_FILE = os.path.join(
    BASE_DIR,
    "trading.db"
)

BACKUP_DIR = os.path.join(
    BASE_DIR,
    "backups"
)

BACKUP_RETENTION_DAYS = int(
    os.getenv(
        "DB_BACKUP_RETENTION_DAYS",
        "14"
    )
)


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(
        timezone.utc
    )


def timestamp_name():
    return now_utc().strftime(
        "%Y%m%dT%H%M%SZ"
    )


def ensure_backup_dir():
    os.makedirs(
        BACKUP_DIR,
        exist_ok=True
    )


# ============================================================
# DATABASE CHECKS
# ============================================================

def integrity_check(
    db_path
):
    conn = sqlite3.connect(
        db_path,
        timeout=30
    )

    try:
        result = conn.execute(
            "PRAGMA integrity_check;"
        ).fetchall()

    finally:
        conn.close()


    messages = [
        row[0]
        for row in result
    ]


    if messages != [
        "ok"
    ]:
        raise RuntimeError(
            (
                "SQLite integrity_check failed: "
                + "; ".join(
                    messages
                )
            )
        )


    return True


def foreign_key_check(
    db_path
):
    conn = sqlite3.connect(
        db_path,
        timeout=30
    )

    try:
        rows = conn.execute(
            "PRAGMA foreign_key_check;"
        ).fetchall()

    finally:
        conn.close()


    if rows:
        raise RuntimeError(
            (
                "SQLite foreign_key_check "
                f"reported {len(rows)} issue(s)"
            )
        )


    return True


# ============================================================
# WAL
# ============================================================

def ensure_wal_mode():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30
    )

    try:
        mode = conn.execute(
            "PRAGMA journal_mode=WAL;"
        ).fetchone()[0]


        conn.execute(
            "PRAGMA wal_checkpoint(PASSIVE);"
        )


    finally:
        conn.close()


    if str(
        mode
    ).lower() != "wal":
        raise RuntimeError(
            (
                "Unable to enable WAL mode. "
                f"Current mode={mode}"
            )
        )


    return mode


# ============================================================
# BACKUP
# ============================================================

def create_backup():
    ensure_backup_dir()


    backup_name = (
        "trading-"
        + timestamp_name()
        + ".db"
    )


    backup_path = os.path.join(
        BACKUP_DIR,
        backup_name
    )


    source = sqlite3.connect(
        DB_FILE,
        timeout=30
    )


    destination = sqlite3.connect(
        backup_path,
        timeout=30
    )


    try:
        source.backup(
            destination,
            pages=100
        )


        destination.commit()


    finally:
        destination.close()
        source.close()


    integrity_check(
        backup_path
    )


    foreign_key_check(
        backup_path
    )


    return backup_path


# ============================================================
# RETENTION
# ============================================================

def cleanup_old_backups():
    ensure_backup_dir()


    cutoff = (
        now_utc()
        -
        timedelta(
            days=
                BACKUP_RETENTION_DAYS
        )
    ).timestamp()


    removed = []


    for name in os.listdir(
        BACKUP_DIR
    ):

        if not (
            name.startswith(
                "trading-"
            )
            and
            name.endswith(
                ".db"
            )
        ):
            continue


        path = os.path.join(
            BACKUP_DIR,
            name
        )


        try:
            modified = os.path.getmtime(
                path
            )

        except OSError:
            continue


        if modified < cutoff:

            os.remove(
                path
            )

            removed.append(
                path
            )


    return removed


# ============================================================
# DISK SPACE
# ============================================================

def check_disk_space():
    usage = shutil.disk_usage(
        BASE_DIR
    )


    free_mb = (
        usage.free
        /
        1024
        /
        1024
    )


    if free_mb < 500:
        raise RuntimeError(
            (
                "Low disk space: "
                f"{free_mb:.1f} MB free"
            )
        )


    return free_mb


# ============================================================
# MAIN
# ============================================================

def main():
    print(
        "TradingMax DB maintenance started",
        flush=True
    )


    free_mb = check_disk_space()


    print(
        f"Disk free: "
        f"{free_mb:.1f} MB",
        flush=True
    )


    integrity_check(
        DB_FILE
    )


    print(
        "Live DB integrity: OK",
        flush=True
    )


    foreign_key_check(
        DB_FILE
    )


    print(
        "Foreign key check: OK",
        flush=True
    )


    mode = ensure_wal_mode()


    print(
        f"Journal mode: {mode}",
        flush=True
    )


    backup_path = (
        create_backup()
    )


    print(
        f"Backup created: "
        f"{backup_path}",
        flush=True
    )


    removed = (
        cleanup_old_backups()
    )


    print(
        f"Old backups removed: "
        f"{len(removed)}",
        flush=True
    )


    print(
        "TradingMax DB maintenance completed",
        flush=True
    )


if __name__ == "__main__":
    main()
