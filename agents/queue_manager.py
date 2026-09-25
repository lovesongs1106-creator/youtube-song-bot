#!/usr/bin/env python3
"""Legacy queue module — compatibility shim over :mod:`agents.queue_engine`.

Consolidated during the 2026-09 stabilization pass: both modules operated on
the same tables, and ``queue_engine`` is now the single implementation (it has
duplicate protection, transactional batch inserts, and the fixed SEO-metadata
import used by the background processor).

All public names are re-exported so any lingering import keeps working.
New code must import from ``agents.queue_engine`` directly.
"""

from __future__ import annotations

import warnings

warnings.warn(
    "agents.queue_manager is deprecated; use agents.queue_engine instead.",
    DeprecationWarning,
    stacklevel=2,
)

from agents.db import init_all_tables as init_queue_tables  # noqa: E402,F401
from agents.queue_engine import (  # noqa: E402,F401
    add_multiple_items,
    add_multiple_items_transactional,
    add_queue_item,
    cancel_all_pending,
    get_next_pending,
    get_queue,
    get_summary,
    is_paused,
    run_queue_processor,
    set_paused,
    update_status,
)

__all__ = [
    "init_queue_tables",
    "add_queue_item",
    "add_multiple_items",
    "add_multiple_items_transactional",
    "get_queue",
    "get_next_pending",
    "update_status",
    "cancel_all_pending",
    "get_summary",
    "is_paused",
    "set_paused",
    "run_queue_processor",
]
