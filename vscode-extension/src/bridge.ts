// A local OpenAI-compatible endpoint the Python agents call; each request is
// answered by a Copilot model through the caller's `send` (vscode.lm in the extension).

import { randomBytes } from "node:crypto";
import * as http from "node:http";
import type { AddressInfo } from "node:net";

export type Part =
  | { kind: "text"; text: string }
  | { kind: "call"; id: string; name: string; input: object }
  | { kind: "result"; id: string; text: string };

export interface Turn {
  role: "user" | "assistant";
  parts: Part[];
}

export interface Tool {
  name: string;
  description: string;
  inputSchema?: object;
}

export interface Reply {
  text: string;
  calls: { id: string; name: string; input: object }[];
}

export type Send = (model: string, turns: Turn[], tools: Tool[], required: boolean) => Promise<Reply>;

interface OpenAIMessage {
  role: "system" | "user" | "assistant" | "tool";
  content?: string | { type: string; text?: string }[] | null;
  tool_calls?: { id: string; function: { name: string; arguments: string } }[];
  tool_call_id?: string;
}

function contentText(content: OpenAIMessage["content"]): string {
  if (typeof content === "string") return content;
  return (content ?? []).map((c) => c.text ?? "").join("");
}

/** OpenAI chat messages as alternating user/assistant turns; a system prompt and tool results are user turns. */
export function toTurns(messages: OpenAIMessage[]): Turn[] {
  const turns: Turn[] = [];
  const push = (role: Turn["role"], parts: Part[]) => {
    if (!parts.length) return;
    const last = turns[turns.length - 1];
    if (last && last.role === role) last.parts.push(...parts);
    else turns.push({ role, parts });
  };
  for (const m of messages) {
    const text = contentText(m.content);
    if (m.role === "tool") {
      push("user", [{ kind: "result", id: m.tool_call_id ?? "", text }]);
    } else if (m.role === "assistant") {
      const parts: Part[] = text ? [{ kind: "text", text }] : [];
      for (const call of m.tool_calls ?? []) {
        parts.push({ kind: "call", id: call.id, name: call.function.name, input: JSON.parse(call.function.arguments || "{}") });
      }
      push("assistant", parts);
    } else {
      push("user", text ? [{ kind: "text", text }] : []);
    }
  }
  return turns;
}

export function toTools(tools: { function: { name: string; description?: string; parameters?: object } }[] = []): Tool[] {
  return tools.map((t) => ({ name: t.function.name, description: t.function.description ?? "", inputSchema: t.function.parameters }));
}

export function toCompletion(model: string, reply: Reply): object {
  const toolCalls = reply.calls.map((c) => ({
    id: c.id,
    type: "function",
    function: { name: c.name, arguments: JSON.stringify(c.input) },
  }));
  return {
    id: `chatcmpl-${randomBytes(8).toString("hex")}`,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [{
      index: 0,
      message: { role: "assistant", content: reply.text || null, ...(toolCalls.length ? { tool_calls: toolCalls } : {}) },
      finish_reason: toolCalls.length ? "tool_calls" : "stop",
    }],
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}

export interface Bridge {
  url: string;
  token: string;
  close(): void;
}

/** Listen on 127.0.0.1 at a free port; every request must carry the session token. */
export function startBridge(send: Send, models: () => Promise<string[]>): Promise<Bridge> {
  const token = randomBytes(24).toString("hex");
  const server = http.createServer(async (req, res) => {
    const reply = (status: number, body: object) => {
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(body));
    };
    const error = (status: number, message: string) => reply(status, { error: { message, type: "invalid_request_error" } });
    if (req.headers.authorization !== `Bearer ${token}`) return error(401, "bad token");
    try {
      if (req.method === "GET" && req.url === "/v1/models") {
        return reply(200, { object: "list", data: (await models()).map((id) => ({ id, object: "model" })) });
      }
      if (req.method !== "POST" || req.url !== "/v1/chat/completions") return error(404, "not found");
      const chunks: Buffer[] = [];
      for await (const chunk of req) chunks.push(chunk as Buffer);
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      // No JSON-schema mode in vscode.lm: refused here, the agents fall back to free text.
      if (body.response_format) return error(400, "response_format is not supported");
      if (body.stream) return error(400, "streaming is not supported");
      const tools = toTools(body.tools);
      const required = body.tool_choice === "required" || typeof body.tool_choice === "object";
      const result = await send(body.model, toTurns(body.messages ?? []), tools, required && tools.length > 0);
      reply(200, toCompletion(body.model, result));
    } catch (e) {
      error(500, e instanceof Error ? e.message : String(e));
    }
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as AddressInfo;
      resolve({ url: `http://127.0.0.1:${port}/v1`, token, close: () => server.close() });
    });
  });
}
