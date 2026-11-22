"""``LAYERLENS_TRACE_ID_FILE``: where an agent tells ``layerlens env run exec`` the id of its own trace.

``exec`` sets the variable for each attempt, beside ``LAYERLENS_ANSWER_FILE``, and links the
id the file holds when the attempt ends to the attempt it submits. The SDK's tracing offers
the id of each trace it uploads (:func:`offer_trace_id`), so an agent instrumented with the
SDK links its trace with no code. Only an empty file takes an id: the first trace an attempt
uploads is the one linked, and an agent that writes the file itself, before or after, decides.
"""

from __future__ import annotations

import os
import logging
import threading
from typing import BinaryIO

log: logging.Logger = logging.getLogger(__name__)

TRACE_ID_FILE_ENV = "LAYERLENS_TRACE_ID_FILE"

# The upload workers of several clients may offer an id at once.
_lock = threading.Lock()


def offer_trace_id(trace_id: str) -> None:
    """Write ``trace_id`` to the file ``LAYERLENS_TRACE_ID_FILE`` names, if that file holds no id yet.

    Does nothing when the variable is unset. Never raises: tracing must not break the agent
    it observes, so a file that cannot be read or written is logged instead.
    """
    path = os.environ.get(TRACE_ID_FILE_ENV)
    if not path or not trace_id:
        return
    with _lock:
        try:
            # Written in place, never replaced by a rename: a rename fails on a file bind-mounted
            # into a container, and a file a container's root user created anew could be
            # unreadable to exec. Read as bytes: an id the agent wrote in UTF-16 is still an id.
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            with os.fdopen(fd, "r+b") as f:
                if _holds_an_id(f):
                    return
                f.seek(0)
                f.truncate()
                f.write(trace_id.encode("utf-8"))
        except (OSError, ValueError) as err:
            log.warning("layerlens: could not write trace id %s to %s=%s: %s", trace_id, TRACE_ID_FILE_ENV, path, err)


def _holds_an_id(f: BinaryIO) -> bool:
    """Whether the file holds anything but whitespace; read a chunk at a time, never whole."""
    while True:
        chunk = f.read(4096)
        if not chunk:
            return False
        if chunk.strip():
            return True
