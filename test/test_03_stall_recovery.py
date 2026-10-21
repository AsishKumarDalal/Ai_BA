"""End-to-end: the "Let me take a look…" stall.

    python test/test_03_stall_recovery.py

This is the bug from the UI session: the model answered "Let me take a quick
look at the data so I can explain it accurately." and the turn ended there. The
user got a sentence and no work.

The first two checks are offline and deterministic — they drive the loop with a
scripted provider. The third one is live: the same question through the real
endpoint must come back with an answer and with SQL behind it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))

import helpers  # noqa: E402
from helpers import heading, info, ok, skip  # noqa: E402

from tsql.agent.loop import Agent  # noqa: E402
from tsql.config import Config  # noqa: E402
from tsql.providers.base import Provider, ProviderRequest, ProviderResponse, Usage  # noqa: E402
from tsql.tools.registry import ToolRegistry  # noqa: E402


class ScriptedProvider(Provider):
    """Replays a fixed list of responses."""

    name = "scripted"

    def __init__(self, responses: list[ProviderResponse]):
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    @property
    def context_length(self) -> int:
        return 100_000

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("the scripted provider ran out of responses")
        return self.responses.pop(0)


def text(text: str, **kwargs) -> ProviderResponse:
    return ProviderResponse(text=text, usage=Usage(input_tokens=10, output_tokens=5), **kwargs)


def agent_for(provider: ScriptedProvider) -> Agent:
    config = Config()
    config.model.model = "scripted"
    return Agent(config=config, provider=provider, registry=ToolRegistry())


class StallTest(unittest.TestCase):
    def test_a_promise_is_nudged_and_the_turn_continues(self):
        heading("a promise is put back to the model")
        provider = ScriptedProvider([
            text("Let me take a quick look at the data so I can explain it accurately."),
            text("There are four tables: customers, products, orders and a view."),
        ])
        agent = agent_for(provider)

        events: list[str] = []
        agent.emitter.add_listener(lambda e: events.append(e.type))

        result = agent.run_conversation("explain the data to me in simple")

        self.assertEqual(result.final_response,
                         "There are four tables: customers, products, orders and a view.")
        self.assertIn("nudge", events)
        self.assertEqual(result.iterations, 2)
        ok("the model was nudged and answered on the second attempt")

        # the nudge must actually reach the model
        follow_up = provider.requests[1].messages[-1]
        self.assertEqual(follow_up["role"], "user")
        self.assertIn("Call the tool now", follow_up["content"])
        ok(f"the follow-up said: {follow_up['content'][:60]}…")

    def test_the_promise_is_never_the_answer(self):
        heading("the promise is not shown to the user")
        provider = ScriptedProvider([
            text("Let me take a quick look at the data so I can explain it accurately."),
            text("The data is a small sales dataset."),
        ])
        agent = agent_for(provider)

        shown: list[str] = []
        agent.emitter.add_listener(
            lambda e: shown.append(e.data["text"])
            if e.type == "assistant_text"
            else None
        )
        agent.run_conversation("explain the data to me in simple")

        self.assertEqual(shown, ["The data is a small sales dataset."])
        ok("the user saw the answer, not the promise")

    def test_a_permanent_stall_is_reported(self):
        heading("a turn that never recovers says so")
        provider = ScriptedProvider([
            text("Let me take a look."),
            text("Let me take a look."),
            text("Let me take a look."),
        ])
        agent = agent_for(provider)

        result = agent.run_conversation("explain the data to me in simple")

        self.assertEqual(result.stop_reason, "stalled_turn")
        self.assertEqual(result.final_response, "")
        self.assertIn("describing what it was about to do", result.error or "")
        ok(f"the turn ended with: {result.error}")

    def test_a_truncated_answer_is_not_an_answer(self):
        heading("a reply cut off mid-sentence is an error")
        provider = ScriptedProvider([
            text("Let me take a quick look at the data so I can explain it",
                 finish_reason="length"),
        ])
        agent = agent_for(provider)

        result = agent.run_conversation("explain the data to me in simple")

        self.assertEqual(result.stop_reason, "truncated_response")
        self.assertEqual(result.final_response, "")
        ok(f"the turn ended with: {result.error}")

    def test_live_the_question_comes_back_answered(self):
        heading("live: 'explain the data to me in simple'")
        if helpers.OFFLINE:
            skip("TSQL_TEST_OFFLINE=1")
            return

        state_db = Path("/tmp/demo-e2e-stall.db")
        helpers.reset_state_db(state_db)
        harness = helpers.make_harness(state_db)
        try:
            result = harness.agent.run_conversation("explain the data to me in simple")
        finally:
            harness.close()

        self.assertEqual(result.stop_reason, "completed", result.error or "")
        self.assertTrue(result.tool_calls, "the model did not call any tool")
        self.assertTrue(
            any(call["name"] == "run_sql" for call in result.tool_calls),
            f"the agent called {[c['name'] for c in result.tool_calls]}, not run_sql",
        )
        ok(f"tool calls: {[c['name'] for c in result.tool_calls]}")

        answer = result.final_response.strip()
        self.assertFalse(
            answer.lower().startswith("let me"),
            f"the answer is still a promise: {answer!r}",
        )
        self.assertGreater(len(answer), 200, f"the answer is too short: {answer!r}")
        ok(f"the answer is {len(answer)} characters and starts: {answer[:70]}…")

        # and the numbers in it must be real
        known = helpers.known_figures()
        for figure in helpers.amounts(answer):
            self.assertTrue(
                any(abs(figure - k) < 0.01 for k in known),
                f"the answer says ${figure:,.2f}, which is not in the database",
            )
        ok("every figure in the answer is in the database")


if __name__ == "__main__":
    unittest.main(verbosity=2)
