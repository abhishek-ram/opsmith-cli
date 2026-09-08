"""Tests for the event channel: what core code reports, and how each renderer shows it."""

import io
import json
from unittest.mock import MagicMock, patch

import typer
from rich.console import Console
from typer.testing import CliRunner

from opsmith.cli import app as app_module
from opsmith.cli.app import handle_errors
from opsmith.cli.output import JsonRenderer, TextRenderer
from opsmith.cli.state import CliState
from opsmith.core.events import (
    STATUS_FINISHED,
    STATUS_STARTED,
    STEP_BUILD,
    BufferingSink,
    Event,
    NullSink,
    resolve_sink,
)
from opsmith.models import MODEL_REGISTRY


def _text_renderer() -> tuple[TextRenderer, io.StringIO]:
    """
    Builds a text renderer writing into a buffer instead of a terminal.

    :return: The renderer and the buffer it writes to.
    """
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=200, no_color=True)
    return TextRenderer(console=console), buffer


def test_null_sink_discards_everything():
    """The default sink accepts every event and keeps none of it."""
    sink = NullSink()

    sink.log(STEP_BUILD, "something happened")
    sink.warning(STEP_BUILD, "something went wrong")
    with sink.waiting(STEP_BUILD, "waiting"):
        pass

    assert not hasattr(sink, "events")


def test_resolve_sink_substitutes_a_null_sink():
    """Code that takes an optional sink gets a usable one either way."""
    real = NullSink()

    assert resolve_sink(real) is real
    assert isinstance(resolve_sink(None), NullSink)


def test_waiting_emits_a_started_and_a_finished_step(events):
    """A wait is a pair of step events, which is what a renderer turns into a spinner."""
    with events.waiting(STEP_BUILD, "Waiting for the LLM"):
        pass

    assert [e.kind for e in events.events] == ["step", "step"]
    assert events.events[0].data == {"status": STATUS_STARTED}
    assert events.events[1].data == {"status": STATUS_FINISHED}
    assert {e.message for e in events.events} == {"Waiting for the LLM"}


def test_waiting_finishes_even_when_the_body_raises(events):
    """A failure inside a wait must not leave a spinner running forever."""
    try:
        with events.waiting(STEP_BUILD, "Waiting for the LLM"):
            raise ValueError("the model exploded")
    except ValueError:
        pass

    assert events.events[-1].data == {"status": STATUS_FINISHED}


def test_buffering_sink_replays_into_a_real_sink(events):
    """
    The plugin registries load before there is a renderer, so they buffer. Draining hands
    everything over in order and leaves the buffer empty.
    """
    buffered = BufferingSink()
    buffered.log(STEP_BUILD, "first")
    buffered.warning(STEP_BUILD, "second")

    buffered.drain_into(events)

    assert events.messages() == ["first", "second"]
    assert buffered.events == []
    buffered.drain_into(events)
    assert events.messages() == ["first", "second"]


def test_text_renderer_styles_each_kind():
    """
    Every kind of event reaches the terminal, with the step heading on its own line. Styling
    lives here rather than in the message, so the same event reads well as JSON.
    """
    renderer, buffer = _text_renderer()

    renderer.emit(Event(kind="step", step=STEP_BUILD, message="Building images"))
    renderer.emit(Event(kind="log", step=STEP_BUILD, message="pushed one image"))
    renderer.emit(Event(kind="warning", step=STEP_BUILD, message="no Dockerfile, skipping"))
    renderer.emit(Event(kind="output", step=STEP_BUILD, message="Step 1/4 : FROM python"))

    output = buffer.getvalue()
    assert "\nBuilding images" in output
    assert "pushed one image" in output
    assert "no Dockerfile, skipping" in output
    assert "Step 1/4 : FROM python" in output


def test_text_renderer_does_not_interpret_subprocess_output_as_markup():
    """
    Subprocess lines routinely contain square brackets. They are escaped, so a terraform plan
    cannot swallow itself by looking like rich markup.
    """
    renderer, buffer = _text_renderer()

    renderer.emit(Event(kind="output", step=STEP_BUILD, message="module.vm[0] will be created"))

    assert "module.vm[0] will be created" in buffer.getvalue()


def test_text_renderer_shows_a_spinner_for_a_step_pair():
    """The started event opens a status, and the finished event closes it."""
    renderer, _ = _text_renderer()
    status = MagicMock()
    with patch.object(renderer.console, "status", return_value=status) as console_status:
        renderer.emit(
            Event(
                kind="step",
                step=STEP_BUILD,
                message="Waiting for the LLM",
                data={"status": STATUS_STARTED},
            )
        )
        assert renderer._status is status
        status.start.assert_called_once()

        renderer.emit(
            Event(
                kind="step",
                step=STEP_BUILD,
                message="Waiting for the LLM",
                data={"status": STATUS_FINISHED},
            )
        )

    console_status.assert_called_once_with("Waiting for the LLM")
    status.stop.assert_called_once()
    assert renderer._status is None


def test_text_renderer_clears_the_spinner_before_reporting_an_outcome():
    """An error raised inside a wait must not be printed underneath a running spinner."""
    renderer, _ = _text_renderer()
    status = MagicMock()
    with patch.object(renderer.console, "status", return_value=status):
        renderer.emit(
            Event(
                kind="step",
                step=STEP_BUILD,
                message="Waiting",
                data={"status": STATUS_STARTED},
            )
        )
        renderer.render_aborted("deploy", 1)

    status.stop.assert_called_once()
    assert renderer._status is None


def test_json_renderer_streams_events_to_stderr_as_ndjson(capsys):
    """Events are NDJSON on stderr; stdout stays reserved for the single envelope."""
    renderer = JsonRenderer()

    renderer.emit(Event(kind="log", step=STEP_BUILD, message="pushed one image"))
    renderer.emit(Event(kind="output", step=STEP_BUILD, message="Step 1/4"))

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [json.loads(line) for line in captured.err.splitlines() if line.strip()]
    assert lines == [
        {"kind": "log", "step": STEP_BUILD, "message": "pushed one image", "data": {}},
        {"kind": "output", "step": STEP_BUILD, "message": "Step 1/4", "data": {}},
    ]


def test_json_mode_keeps_stdout_to_one_document(monkeypatch, tmp_project, runner: CliRunner):
    """
    The guarantee the CLI contract rests on: with core reporting through events, a run that
    emits progress, subprocess output and a warning still writes exactly one JSON document on
    stdout, and everything else arrives on stderr as NDJSON.
    """
    monkeypatch.setattr(app_module, "configure_agent", lambda *args, **kwargs: object())
    monkeypatch.chdir(tmp_project)

    def reports_progress(ctx: typer.Context):
        """A command that reports through the context the way a core module does."""
        state: CliState = ctx.obj
        state.context.events.step(STEP_BUILD, "Building images")
        state.context.events.log(STEP_BUILD, "pushed one image")
        state.context.events.warning(STEP_BUILD, "no Dockerfile for the worker")
        state.context.events.output(STEP_BUILD, "Step 1/4 : FROM python")

    probe = typer.Typer(pretty_exceptions_show_locals=False)
    probe.callback()(app_module.main)
    probe.command()(handle_errors(reports_progress))

    result = runner.invoke(
        probe,
        [
            "--model",
            MODEL_REGISTRY.model_names[0],
            "--api-key",
            "test-key",
            "--output",
            "json",
            "reports-progress",
        ],
    )

    assert result.exit_code == 0
    documents = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(documents) == 1
    assert json.loads(documents[0])["ok"] is True

    streamed = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
    assert [event["kind"] for event in streamed] == ["step", "log", "warning", "output"]
    assert streamed[-1]["message"] == "Step 1/4 : FROM python"
