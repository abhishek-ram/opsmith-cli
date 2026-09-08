"""Progress reporting for code that must never touch a terminal.

Core modules do not print. They describe what is happening as an :class:`Event` and hand it to
an :class:`EventSink`, and only the sink knows whether that becomes styled text on a terminal,
a line of NDJSON on stderr, or nothing at all.

The sink has exactly one method an implementation must provide, :meth:`EventSink.emit`. The
helpers built on top of it - :meth:`EventSink.log` and friends - exist because there are
roughly a hundred and fifty call sites, and ``ctx.events.log(STEP_BUILD, "...")`` reads better
at every one of them than constructing an ``Event`` by hand.
"""

import abc
from contextlib import contextmanager
from typing import Iterator, Literal, Optional

from pydantic import BaseModel, Field

#: The step names in use. A step groups the events of one phase of a run, so a renderer or a
#: harness can tell "this line is part of building images" from "this line is part of DNS".
STEP_REGISTRY = "registry"
STEP_DETECT = "detect"
STEP_SETUP = "setup"
STEP_BUILD = "build"
STEP_VM = "vm"
STEP_COMPOSE = "compose"
STEP_DNS = "dns"
STEP_FRONTEND = "frontend"
STEP_DESTROY = "destroy"
STEP_RUN = "run"
STEP_PROVISION = "provision"

#: Values of ``Event.data["status"]`` on the pair of step events that bracket a wait. A renderer
#: shows a spinner between them; everything else treats them as two ordinary step events.
STATUS_STARTED = "started"
STATUS_FINISHED = "finished"


class Event(BaseModel):
    """One thing that happened during a run, described for any audience.

    ``message`` is plain text. It carries no markup, because the same event has to render as a
    styled terminal line and as a JSON field, and only the renderer knows which.
    """

    kind: Literal["step", "log", "warning", "output"] = Field(
        ...,
        description=(
            "step: a phase of the run is beginning. log: ordinary progress. warning: something"
            " went wrong but the run continues. output: one raw line from a subprocess."
        ),
    )
    step: str = Field(..., description="The phase of the run this event belongs to.")
    message: str = Field(..., description="What happened, in plain text with no markup.")
    data: dict = Field(
        default_factory=dict, description="Machine-readable context for a harness reading events."
    )


class EventSink(abc.ABC):
    """Receives the events a run produces.

    Implementations provide :meth:`emit`. Everything else on this class is a convenience that
    funnels into it.
    """

    @abc.abstractmethod
    def emit(self, event: Event):
        """
        Reports one event.

        :param event: The event to report.
        """

    def step(self, step: str, message: str, **data):
        """
        Announces that a phase of the run is beginning.

        :param step: The step name, one of the module's ``STEP_*`` constants.
        :param message: The heading, in plain text.
        :param data: Machine-readable context for the event.
        """
        self.emit(Event(kind="step", step=step, message=message, data=data))

    def log(self, step: str, message: str, **data):
        """
        Reports ordinary progress.

        :param step: The step name this belongs to.
        :param message: The message, in plain text.
        :param data: Machine-readable context for the event.
        """
        self.emit(Event(kind="log", step=step, message=message, data=data))

    def warning(self, step: str, message: str, **data):
        """
        Reports something that went wrong without stopping the run.

        :param step: The step name this belongs to.
        :param message: The warning, in plain text.
        :param data: Machine-readable context for the event.
        """
        self.emit(Event(kind="warning", step=step, message=message, data=data))

    def output(self, step: str, line: str):
        """
        Reports one raw line of output from a subprocess.

        :param step: The step name this belongs to.
        :param line: The line, exactly as the subprocess wrote it.
        """
        self.emit(Event(kind="output", step=step, message=line))

    @contextmanager
    def waiting(self, step: str, message: str) -> Iterator[None]:
        """
        Brackets a slow operation with a started and a finished step event.

        A terminal renderer shows a spinner for the duration; every other sink sees two events.
        The finished event is emitted even when the body raises, so a failure never leaves a
        spinner running.

        :param step: The step name this belongs to.
        :param message: What is being waited for, in plain text.
        """
        self.emit(Event(kind="step", step=step, message=message, data={"status": STATUS_STARTED}))
        try:
            yield
        finally:
            self.emit(
                Event(kind="step", step=step, message=message, data={"status": STATUS_FINISHED})
            )


class NullSink(EventSink):
    """Discards every event. The default wherever a sink is optional, and in tests."""

    def emit(self, event: Event):
        """Does nothing."""


class BufferingSink(EventSink):
    """Holds events until something with a real sink can take them.

    The plugin registries load at import time, long before the CLI has built a renderer, so
    they report into one of these and the CLI drains it once it can render.
    """

    def __init__(self):
        self.events: list[Event] = []

    def emit(self, event: Event):
        """Appends the event to the buffer."""
        self.events.append(event)

    def drain_into(self, sink: EventSink):
        """
        Hands every buffered event to a real sink and empties the buffer.

        :param sink: The sink to replay the events into.
        """
        buffered = self.events
        self.events = []
        for event in buffered:
            sink.emit(event)


def resolve_sink(events: Optional[EventSink]) -> EventSink:
    """
    Returns the sink to use, substituting a :class:`NullSink` when none was supplied.

    :param events: A sink, or None.
    :return: A usable sink.
    """
    return events if events is not None else NullSink()
