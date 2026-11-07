import assert from "node:assert/strict";
import { test } from "node:test";
import { type Send, startBridge, toCompletion, toTurns } from "../src/bridge";

test("system and user merge into one user turn; tool results return as a user turn", () => {
  const turns = toTurns([
    { role: "system", content: "You are an analyst." },
    { role: "user", content: "Analyze TCS" },
    { role: "assistant", content: null, tool_calls: [{ id: "c1", function: { name: "get_stock_data", arguments: '{"start_date":"2026-11-01"}' } }] },
    { role: "tool", tool_call_id: "c1", content: "prices" },
  ]);
  assert.deepEqual(turns, [
    { role: "user", parts: [{ kind: "text", text: "You are an analyst." }, { kind: "text", text: "Analyze TCS" }] },
    { role: "assistant", parts: [{ kind: "call", id: "c1", name: "get_stock_data", input: { start_date: "2026-11-01" } }] },
    { role: "user", parts: [{ kind: "result", id: "c1", text: "prices" }] },
  ]);
});

test("a reply with tool calls finishes as tool_calls", () => {
  const done = toCompletion("m", { text: "", calls: [{ id: "c1", name: "t", input: { a: 1 } }] }) as any;
  assert.equal(done.choices[0].finish_reason, "tool_calls");
  assert.equal(done.choices[0].message.content, null);
  assert.deepEqual(done.choices[0].message.tool_calls[0].function, { name: "t", arguments: '{"a":1}' });
});

test("the bridge answers only with its token, refuses response_format, and forwards tools", async () => {
  const seen: Parameters<Send>[] = [];
  const bridge = await startBridge(async (...args) => {
    seen.push(args);
    return { text: "hello", calls: [] };
  }, async () => ["gpt-5-mini"]);
  const post = (body: object, token = bridge.token) => fetch(`${bridge.url}/chat/completions`, {
    method: "POST", headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  try {
    assert.equal((await post({}, "wrong")).status, 401);
    assert.equal((await post({ model: "m", messages: [], response_format: { type: "json_schema" } })).status, 400);
    const res = await post({
      model: "gpt-5-mini", messages: [{ role: "user", content: "hi" }], tool_choice: "required",
      tools: [{ type: "function", function: { name: "get_news", description: "news", parameters: { type: "object" } } }],
    });
    const body = await res.json() as any;
    assert.equal(body.choices[0].message.content, "hello");
    assert.equal(seen.length, 1);
    const [model, , tools, required] = seen[0];
    assert.equal(model, "gpt-5-mini");
    assert.deepEqual(tools, [{ name: "get_news", description: "news", inputSchema: { type: "object" } }]);
    assert.equal(required, true);
    const models = await fetch(`${bridge.url}/models`, { headers: { Authorization: `Bearer ${bridge.token}` } });
    assert.deepEqual((await models.json() as any).data, [{ id: "gpt-5-mini", object: "model" }]);
  } finally {
    bridge.close();
  }
});
