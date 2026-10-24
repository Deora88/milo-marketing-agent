"""Runner hides model reasoning, streams deltas, and surfaces approvals."""
from google.adk.events import Event
from google.genai import types

from app.agent import runner


class FakeRunner:
    def __init__(self, events):
        self.events = events

    async def run_async(self, **kwargs):
        assert kwargs["run_config"].streaming_mode is not None
        for e in self.events:
            yield e


def ev(*parts, partial=False):
    return Event(author="milo", content=types.Content(role="model", parts=list(parts)), partial=partial)


async def test_thoughts_hidden_and_text_streamed(monkeypatch):
    events = [
        ev(types.Part(text="The user said hi. I should greet them.", thought=True), partial=True),
        ev(types.Part(text="Hi! I'm "), partial=True),
        ev(types.Part(text="Milo."), partial=True),
        ev(types.Part(text="The user said hi.", thought=True), types.Part(text="Hi! I'm Milo.")),
    ]
    monkeypatch.setattr(runner, "get_runner", lambda: FakeRunner(events))
    out = [e async for e in runner.chat_turn("s-test", "hi")]
    assert [e["type"] for e in out] == ["delta", "delta", "message"]
    assert all("greet" not in e.get("text", "") and "user said" not in e.get("text", "") for e in out)
    assert out[-1]["text"] == "Hi! I'm Milo."
