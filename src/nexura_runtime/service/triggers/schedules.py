# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Timezone-aware next fire calculation, DST transitions, and missed-run policies."""

from __future__ import annotations

import math
import zoneinfo
from datetime import datetime, timedelta

from nexura_runtime.service.triggers.errors import InvalidScheduleError
from nexura_runtime.service.triggers.models import MissedRunPolicy, ScheduleConfig

# Hard floor protecting the scheduler loop (the API enforces a larger, configurable minimum).
HARD_MIN_INTERVAL_SECONDS = 0.01
HARD_MAX_INTERVAL_SECONDS = 366 * 24 * 3600.0


# Never-matching expressions (e.g. '0 0 31 2 *') are rejected after scanning this horizon.
MAX_CRON_SEARCH_MINUTES = 60 * 24 * 366 * 5


def validate_interval(interval_seconds: float) -> None:
    """Reject non-finite, tiny or enormous intervals that could stall or overflow the scheduler."""
    if (
        not math.isfinite(interval_seconds)
        or interval_seconds < HARD_MIN_INTERVAL_SECONDS
        or interval_seconds > HARD_MAX_INTERVAL_SECONDS
    ):
        raise InvalidScheduleError(
            f"interval_seconds must be between {HARD_MIN_INTERVAL_SECONDS} and "
            f"{HARD_MAX_INTERVAL_SECONDS} seconds (got {interval_seconds})."
        )


def get_zoneinfo(tz_name: str) -> zoneinfo.ZoneInfo:
    """Safely resolve an IANA timezone string into a ZoneInfo instance."""
    try:
        return zoneinfo.ZoneInfo(tz_name)
    except Exception as exc:
        raise InvalidScheduleError(f"Unknown timezone identifier: '{tz_name}'") from exc


def _parse_field(pattern: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron expression field (e.g. '*', '*/5', '1,2,3', '10')."""
    pattern = pattern.strip()
    if pattern == "*":
        return set(range(min_val, max_val + 1))

    if pattern.startswith("*/"):
        try:
            step = int(pattern[2:])
            if step <= 0:
                raise ValueError
            return {v for v in range(min_val, max_val + 1) if (v - min_val) % step == 0}
        except ValueError as exc:
            raise InvalidScheduleError(f"Invalid cron step pattern: '{pattern}'") from exc

    results: set[int] = set()
    for part in pattern.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start_s, end_s = part.split("-", 1)
                start, end = int(start_s), int(end_s)
                if start > end or start < min_val or end > max_val:
                    raise ValueError
                results.update(range(start, end + 1))
            except ValueError as exc:
                raise InvalidScheduleError(f"Invalid cron range: '{part}'") from exc
        else:
            try:
                val = int(part)
                if val < min_val or val > max_val:
                    raise ValueError
                results.add(val)
            except ValueError as exc:
                raise InvalidScheduleError(f"Invalid cron value: '{part}'") from exc

    return results


def calculate_next_cron_fire(
    from_timestamp: float,
    cron_expr: str,
    timezone_name: str = "UTC",
    *,
    max_search_minutes: int = MAX_CRON_SEARCH_MINUTES,
) -> float:
    """Calculate next Unix epoch fire timestamp for a 5-part cron expression in timezone."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        raise InvalidScheduleError(
            f"Cron expression must have 5 fields (got {len(fields)}): '{cron_expr}'"
        )

    # 1: minute (0-59), 2: hour (0-23), 3: day_of_month (1-31)
    # 4: month (1-12), 5: day_of_week (0-6)
    valid_mins = _parse_field(fields[0], 0, 59)
    valid_hours = _parse_field(fields[1], 0, 23)
    valid_dom = _parse_field(fields[2], 1, 31)
    valid_months = _parse_field(fields[3], 1, 12)
    # Map 7 to 0 (Sunday) if present
    valid_dow_raw = _parse_field(fields[4], 0, 7)
    valid_dow = {0 if d == 7 else d for d in valid_dow_raw}

    tz = get_zoneinfo(timezone_name)
    start_dt = datetime.fromtimestamp(from_timestamp, tz=tz)

    # Advance by 1 minute from the reference time and truncate seconds/microseconds
    curr = (start_dt + timedelta(minutes=1)).replace(second=0, microsecond=0)

    limit = curr + timedelta(minutes=max_search_minutes)
    # Skip ahead by day / hour instead of stepping minute by minute (bounded CPU per call).
    while curr < limit:
        # Python weekday: Monday=0 ... Sunday=6. Standard cron: Sunday=0, Monday=1 ... Saturday=6
        cron_dow = (curr.weekday() + 1) % 7
        if curr.month not in valid_months or curr.day not in valid_dom or cron_dow not in valid_dow:
            curr = curr.replace(hour=0, minute=0) + timedelta(days=1)
        elif curr.hour not in valid_hours:
            curr = curr.replace(minute=0) + timedelta(hours=1)
        elif curr.minute not in valid_mins:
            curr += timedelta(minutes=1)
        else:
            return curr.timestamp()

    raise InvalidScheduleError(f"No fire time found within search window for cron: '{cron_expr}'")


def calculate_next_fire(
    from_timestamp: float,
    schedule: ScheduleConfig,
) -> float:
    """Calculate the next fire timestamp based on either interval_seconds or cron_expr."""
    if schedule.interval_seconds is not None and schedule.interval_seconds > 0:
        validate_interval(schedule.interval_seconds)
        return from_timestamp + schedule.interval_seconds
    elif schedule.cron_expr:
        return calculate_next_cron_fire(
            from_timestamp=from_timestamp,
            cron_expr=schedule.cron_expr,
            timezone_name=schedule.timezone,
        )
    else:
        raise InvalidScheduleError(
            f"Schedule '{schedule.schedule_id}' must specify interval_seconds or cron_expr."
        )


def evaluate_missed_runs(
    schedule: ScheduleConfig,
    current_time: float,
    *,
    max_catchup_runs: int = 100,
) -> tuple[list[float], float]:
    """Evaluate missed runs on startup/restart according to schedule's MissedRunPolicy.

    Returns:
        Tuple of (list of catchup timestamps to enqueue, new next_run_at timestamp).
    """
    if not schedule.is_active or schedule.next_run_at >= current_time:
        # Not missed
        return [], schedule.next_run_at

    policy: MissedRunPolicy = schedule.missed_run_policy

    if policy == "fire_all":
        # Fire missed occurrences between next_run_at and current_time, bounded per call; any
        # remainder is caught up by subsequent ticks because next_run_at only advances this far.
        catchups: list[float] = []
        next_fire = schedule.next_run_at
        count = 0
        while next_fire < current_time and count < max_catchup_runs:
            catchups.append(next_fire)
            following = calculate_next_fire(next_fire, schedule)
            if following <= next_fire:  # defensive: never loop without progress
                raise InvalidScheduleError(f"Schedule '{schedule.schedule_id}' cannot advance.")
            next_fire = following
            count += 1
        return catchups, next_fire

    if policy in ("skip", "fire_once"):
        # Jump straight to the first fire at/after current_time without iterating every
        # missed occurrence (downtime x small interval would otherwise be a huge loop).
        next_fire = _first_fire_at_or_after(schedule, current_time)
        if policy == "skip":
            return [], next_fire
        return [schedule.next_run_at], next_fire

    return [], schedule.next_run_at


def _first_fire_at_or_after(schedule: ScheduleConfig, current_time: float) -> float:
    """First scheduled fire time >= current_time, computed without per-occurrence iteration."""
    if schedule.interval_seconds is not None and schedule.interval_seconds > 0:
        validate_interval(schedule.interval_seconds)
        interval = schedule.interval_seconds
        steps = math.ceil((current_time - schedule.next_run_at) / interval)
        candidate = schedule.next_run_at + max(steps, 1) * interval
        # Guard float rounding so the result is never behind current_time.
        while candidate < current_time:
            candidate += interval
        return candidate
    candidate = calculate_next_fire(current_time - 1.0, schedule)
    guard = 0
    while candidate < current_time:
        following = calculate_next_fire(candidate, schedule)
        if following <= candidate or guard > 4:
            raise InvalidScheduleError(f"Schedule '{schedule.schedule_id}' cannot advance.")
        candidate = following
        guard += 1
    return candidate
