"""
Housekeeping: purging receipt photos.

On a phone, the browser quota did the cleaning. On a server, nobody does:
without a purge, photos pile up indefinitely, for every group, until the volume
is full.

What disappears is **the photo, not the receipt**. The lines, the taxes and the
split were checked by a human on the verification screen; the photo is only
supporting evidence, useful for a few weeks. So we delete the file and set
`image_id` back to NULL, so the UI knows there is no photo left instead of
asking for one it cannot find.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import config, db

logger = logging.getLogger("splitticket.maintenance")


def purge_old_images(days: int | None = None) -> int:
    """
    Delete photos older than the retention window. Returns how many were deleted.

    A zero or negative retention disables the purge: that is an explicit choice
    by whoever hosts the instance, not a value to silently correct.
    """
    retention = config.IMAGE_RETENTION_DAYS if days is None else days
    if retention <= 0:
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention)).isoformat(timespec="seconds")
    rows = db.query("SELECT id, path, receipt_id FROM image WHERE created_at < ?", (cutoff,))
    if not rows:
        return 0

    removed = 0
    for row in rows:
        # The file first: if the database survives a crash between the two, we
        # are left with a reference without a file, which the image route
        # already turns into a 404. The reverse — a deleted row and an orphan
        # file — cannot be recovered from.
        (config.IMAGES_DIR / row["path"]).unlink(missing_ok=True)
        with db.transaction() as connection:
            connection.execute("DELETE FROM image WHERE id = ?", (row["id"],))
            connection.execute(
                "UPDATE receipt SET image_id = NULL WHERE id = ? AND image_id = ?",
                (row["receipt_id"], row["id"]),
            )
        removed += 1

    logger.info("purge: deleted %d photo(s) older than %d days", removed, retention)
    return removed


def purge_orphan_files() -> int:
    """
    Delete files on disk that no row references.

    A net for interruptions: an upload cut off between writing the file and
    inserting the row leaves a file that nothing names any more.
    """
    known = {row["path"] for row in db.query("SELECT path FROM image")}
    removed = 0
    for path in config.IMAGES_DIR.glob("*.bin"):
        if path.name not in known:
            path.unlink(missing_ok=True)
            removed += 1
    if removed:
        logger.info("purge: deleted %d orphan file(s)", removed)
    return removed


async def run_periodically() -> None:
    """
    Housekeeping loop, started at boot.

    One immediate pass first — a server that restarts often must still get its
    cleaning done — then one a day. Errors are logged and do not stop the loop:
    housekeeping must never bring the service down.
    """
    while True:
        try:
            purge_old_images()
            purge_orphan_files()
        except Exception:  # noqa: BLE001 — housekeeping must not bring the service down
            logger.exception("the purge failed; will try again on the next cycle")
        await asyncio.sleep(config.PURGE_INTERVAL_SECONDS)
