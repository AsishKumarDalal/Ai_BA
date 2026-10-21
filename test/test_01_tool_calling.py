"""End-to-end: does the endpoint really do tool calling, and does the agent
use it to get the right answer?

    python test/test_01_tool_calling.py

Two checks:

1. The endpoint returns a usable ``tool_calls`` payload. This is the hard
   requirement for the agent loop — without it nothing else matters.
2. A real question goes through the whole harness, and the number in the
   answer is the number in the database.
"""

from __future__ import annotations

import sys
import unittest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import helpers  # noqa: E402
from helpers import bad, heading, info, ok, skip  # noqa: E402


class ToolCallingTest(unittest.TestCase):
    def test_the_endpoint_returns_tool_calls(self):
        """The endpoint must return tool calls the agent can act on."""
        heading("the endpoint returns tool calls")
        if helpers.OFFLINE:
            skip("TSQL_TEST_OFFLINE=1")
            return

        from openai import OpenAI

        cfg = helpers.config()
        client = OpenAI(
            base_url=cfg.model.base_url,
            api_key=cfg.model.api_key or "not-required",
            timeout=cfg.model.timeout_seconds,
        )
        tool = {
            "type": "function",
            "function": {
                "name": "run_sql",
                "description": "Run a read-only SQL query.",
                "parameters": {
                    "type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"],
                },
            },
        }
        response = client.chat.completions.create(
            model=cfg.model.model,
            messages=[{"role": "user", "content": "Count the rows in the orders table."}],
            tools=[tool],
            max_tokens=cfg.model.max_tokens,
        )
        message = response.choices[0].message
        calls = list(message.tool_calls or [])

        self.assertTrue(calls, "the endpoint returned no tool calls")
        first = calls[0]
        self.assertEqual(first.function.name, "run_sql")
        self.assertTrue(first.id, "the tool call has no id, so the result cannot be matched")
        self.assertIn("sql", first.function.arguments)
        ok(f"tool_calls: {first.function.name}({first.function.arguments})")
        ok(f"finish_reason: {response.choices[0].finish_reason}")
        info(f"usage: {response.usage.prompt_tokens} prompt / "
             f"{response.usage.completion_tokens} completion tokens")

    def test_the_agent_answers_with_the_number_in_the_database(self):
        """The whole loop: question -> run_sql -> answer, checked against SQLite."""
        heading("the agent runs SQL and the answer matches the database")
        if helpers.OFFLINE:
            skip("TSQL_TEST_OFFLINE=1")
            return

        helpers.ensure_demo_db()
        expected = helpers.total_revenue()
        info(f"the database says total paid revenue is ${expected:,.2f}")

        harness = helpers.make_harness()
        try:
            result = harness.agent.run_conversation(
                "What is the total revenue from paid orders? Give me the number."
            )
        finally:
            harness.close()

        self.assertEqual(result.stop_reason, "completed", result.error or "")
        self.assertTrue(result.tool_calls, "the agent never called a tool")
        self.assertTrue(
            any(call["name"] == "run_sql" for call in result.tool_calls),
            f"the agent called {[c['name'] for c in result.tool_calls]}, not run_sql",
        )
        ok(f"tool calls: {[c['name'] for c in result.tool_calls]}")

        found = helpers.amounts(result.final_response)
        self.assertTrue(found, f"the answer has no number in it: {result.final_response!r}")
        matched = [f for f in found if abs(f - expected) < 0.01]
        self.assertTrue(
            matched,
            f"the answer has {found} but the database says {expected}",
        )
        ok(f"the answer states ${matched[0]:,.2f}, which is the number in the database")


if __name__ == "__main__":
    unittest.main(verbosity=2)
