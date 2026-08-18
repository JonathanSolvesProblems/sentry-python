import weakref
from typing import TYPE_CHECKING

import sentry_sdk
from sentry_sdk.integrations import Integration
from sentry_sdk.scope import add_global_event_processor
from sentry_sdk.utils import ContextVar, logger

if TYPE_CHECKING:
    from typing import Optional

    from sentry_sdk._types import Event, Hint


class DedupeIntegration(Integration):
    identifier = "dedupe"

    def __init__(self) -> None:
        self._last_seen = ContextVar("last-seen")

    @staticmethod
    def setup_once() -> None:
        @add_global_event_processor
        def processor(event: "Event", hint: "Optional[Hint]") -> "Optional[Event]":
            if hint is None:
                return event

            integration = sentry_sdk.get_client().get_integration(DedupeIntegration)
            if integration is None:
                return event

            exc_info = hint.get("exc_info", None)
            if exc_info is None:
                return event

            last_seen = integration._last_seen.get(None)
            if last_seen is not None:
                # last_seen is a weakref; dereference it to get the original exception.
                last_seen = last_seen() if isinstance(last_seen, weakref.ref) else None

            exc = exc_info[1]
            if last_seen is exc:
                logger.info("DedupeIntegration dropped duplicated error event %s", exc)
                return None

            # Store a weakref so we don't hold a strong reference to the exception.
            # A strong reference would retain the traceback and all frame locals for
            # the lifetime of the ContextVar (i.e. the asyncio task), causing a
            # memory leak proportional to the number of live long-running tasks.
            # Builtin exception types don't support weakrefs; for those we skip
            # storing rather than falling back to a strong reference.
            try:
                integration._last_seen.set(weakref.ref(exc))
            except TypeError:
                pass

            return event

    @staticmethod
    def reset_last_seen() -> None:
        integration = sentry_sdk.get_client().get_integration(DedupeIntegration)
        if integration is None:
            return

        integration._last_seen.set(None)
