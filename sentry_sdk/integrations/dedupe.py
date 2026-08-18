import weakref
from typing import TYPE_CHECKING

import sentry_sdk
from sentry_sdk.integrations import Integration
from sentry_sdk.scope import add_global_event_processor
from sentry_sdk.utils import ContextVar, logger

if TYPE_CHECKING:
    from typing import Any, Optional

    from sentry_sdk._types import Event, Hint


#: Attribute used to attach a weak-referenceable identity token to exceptions
#: that do not support weak references themselves.
_DEDUPE_TOKEN_ATTR = "_sentry_dedupe_token"


class _DedupeToken:
    """A weak-referenceable stand-in for an exception's identity.

    Builtin exceptions (``ValueError``, ``KeyError``, ...) cannot be weak
    referenced. Storing the exception itself keeps it, its traceback, and every
    frame local reachable from that traceback alive for as long as the
    enclosing ``ContextVar`` lives. Under asyncio that is the lifetime of the
    task, which is the leak reported in #6094.

    A token is attached to the exception and only weakly referenced from here,
    so it stays alive exactly as long as the exception does and dies with it.
    Because there is one token per exception object, comparing tokens is
    equivalent to comparing exception identity, so dedupe behaviour is
    unchanged.
    """

    __slots__ = ("__weakref__",)


def _identity_token(exc: BaseException) -> "Optional[_DedupeToken]":
    """Return the dedupe token for ``exc``, attaching one if necessary.

    Returns ``None`` when the exception cannot carry a token, or when it has no
    traceback. In both cases the caller keeps the previous behaviour: without a
    traceback there are no frames and no frame locals reachable through the
    exception, so holding it strongly cannot pin anything, and leaving such
    exceptions untouched keeps ``vars(exc)`` clean for the common case of an
    exception that was constructed but never raised.
    """
    try:
        if exc.__traceback__ is None:
            return None

        exc_dict = exc.__dict__
        token = exc_dict.get(_DEDUPE_TOKEN_ATTR)
        if isinstance(token, _DedupeToken):
            return token

        token = _DedupeToken()
        exc_dict[_DEDUPE_TOKEN_ATTR] = token
        return token
    except Exception:
        # Exceptions can define ``__dict__`` as a property returning anything,
        # or back it with a mapping that refuses mutation. None of that may
        # break event processing, so fall back to the previous behaviour.
        return None


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
                # last_seen is a weakref (to the exception or to its identity
                # token) or the original instance
                last_seen = (
                    last_seen() if isinstance(last_seen, weakref.ref) else last_seen
                )

            exc = exc_info[1]

            new_last_seen: "Any"
            try:
                # We can only weakref non builtin types.
                new_last_seen = weakref.ref(exc)
                is_duplicate = last_seen is exc
            except TypeError:
                # Builtin exception. Referencing it strongly here would pin its
                # traceback and every frame local in that traceback for the
                # lifetime of the ContextVar (#6094). Weakly reference an
                # identity token carried by the exception instead, which dies
                # with it and keeps dedupe keyed on exception identity.
                token = _identity_token(exc)
                if token is not None:
                    new_last_seen = weakref.ref(token)
                    is_duplicate = last_seen is token
                else:
                    new_last_seen = exc
                    is_duplicate = last_seen is exc

            if is_duplicate:
                logger.info("DedupeIntegration dropped duplicated error event %s", exc)
                return None

            integration._last_seen.set(new_last_seen)

            return event

    @staticmethod
    def reset_last_seen() -> None:
        integration = sentry_sdk.get_client().get_integration(DedupeIntegration)
        if integration is None:
            return

        integration._last_seen.set(None)
