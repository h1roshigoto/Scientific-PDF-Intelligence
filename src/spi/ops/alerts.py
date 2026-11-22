"""Evaluate aggregate local deployment alerts and emit state transitions."""

from time import time

from spi.upload_store import _positive_limit

from .telemetry import event, last_snapshot

PERIOD = 15


def alerts(samples, workers, previous, now, started):
    api, queue, backup, restore, health = (
        samples.get(name, {}) for name in ("api", "queue", "backup", "restore", "health")
    )

    def stale(stamp, limit):
        return now - (stamp or started) >= limit

    def increase(key):
        # A new API process resets all request counters.
        if not previous or api.get("instance") != previous.get("instance"):
            return 0
        return max(0, api.get(key, 0) - previous.get(key, 0))

    return {name for name, active in {
        "api_unavailable": stale(health.get("last_api_ok"), 45),
        "worker_stalled": not workers and now - started >= 45,
        "queue_backlog": queue.get("queue_depth", 0) >= 10,
        "queue_age": queue.get("oldest_queued_job_age_seconds", 0) >= 120,
        "processing_long": queue.get("oldest_processing_age_seconds", 0) >= 180,
        "api_5xx_increase": increase("http_5xx") >= 5,
        "login_abuse": increase("login_failures") >= 10,
        "rate_limit_spike": increase("rate_limit_hits") >= 10,
        "disk_low": queue.get("disk_low", False),
        "queue_metrics_unavailable": stale(queue.get("sample_time"), 60) or queue.get("queue_available") is False,
        "backup_overdue": stale(backup.get("last_success_timestamp"), _positive_limit("SPI_BACKUP_MAX_AGE", 26 * 3600)),
        "backup_failed": backup.get("last_result") == "failed",
        "restore_overdue": stale(restore.get("last_restore_timestamp"),
                                 _positive_limit("SPI_RESTORE_MAX_AGE", 31 * 86400)),
        "sandbox_unavailable": stale(health.get("last_sandbox_ok"), 60) and not stale(health.get("last_api_ok"), 45),
    }.items() if active}


def monitor(sleep, current_workers):
    started = time()
    previous, firing = {}, set()
    while True:
        samples = {name: last_snapshot(name) for name in ("api", "queue", "backup", "restore", "health")}
        workers = current_workers()
        current = alerts(samples, workers, previous, time(), started)
        for name in sorted(current - firing):
            event("monitoring", "alert", level="WARNING", alert=name, state="firing")
        for name in sorted(firing - current):
            event("monitoring", "alert", alert=name, state="resolved")
        event("monitoring", "sample", active_workers=len(workers),
              worker_state="processing" if "processing" in workers else "idle" if workers else "stalled",
              **samples)
        previous, firing = samples["api"], current
        sleep(PERIOD)
