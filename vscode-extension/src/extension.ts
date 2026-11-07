import { spawn, type ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import * as readline from "node:readline";
import * as vscode from "vscode";
import { type Bridge, type Send, startBridge, type Turn } from "./bridge";
import { parseRequest } from "./parse";
import { ensureRuntime } from "./runtime";

const SECRETS: Record<string, string> = {
  FRED_API_KEY: "FRED (US and India macro series)",
  ALPHA_VANTAGE_API_KEY: "Alpha Vantage (optional US data vendor)",
};
const DISCLAIMER = "_Not investment advice. NSE data is for personal, non-commercial use._\n\n";
let running = false;

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.chat.createChatParticipant("tradingagents.analyst", (request, _ctx, stream, token) =>
      handle(context, request, stream, token)),
    vscode.commands.registerCommand("tradingagents.analyze", () =>
      vscode.commands.executeCommand("workbench.action.chat.open", { query: "@tradingagents /analyze " })),
    vscode.commands.registerCommand("tradingagents.setApiKey", () => setApiKey(context)),
  );
}

export function deactivate(): void {}

async function setApiKey(context: vscode.ExtensionContext): Promise<void> {
  const pick = await vscode.window.showQuickPick(
    Object.entries(SECRETS).map(([key, label]) => ({ label, key })), { placeHolder: "Which key?" });
  if (!pick) return;
  const value = await vscode.window.showInputBox({ prompt: `${pick.label} API key (empty removes it)`, password: true });
  if (value === undefined) return;
  if (value) await context.secrets.store(pick.key, value.trim());
  else await context.secrets.delete(pick.key);
}

function toLm(turns: Turn[]): vscode.LanguageModelChatMessage[] {
  return turns.map((turn) => {
    const parts = turn.parts.map((p) =>
      p.kind === "text" ? new vscode.LanguageModelTextPart(p.text)
        : p.kind === "call" ? new vscode.LanguageModelToolCallPart(p.id, p.name, p.input)
          : new vscode.LanguageModelToolResultPart(p.id, [new vscode.LanguageModelTextPart(p.text)]));
    return turn.role === "user"
      ? vscode.LanguageModelChatMessage.User(parts as (vscode.LanguageModelTextPart | vscode.LanguageModelToolResultPart)[])
      : vscode.LanguageModelChatMessage.Assistant(parts as (vscode.LanguageModelTextPart | vscode.LanguageModelToolCallPart)[]);
  });
}

async function copilotModel(id: string): Promise<vscode.LanguageModelChat | undefined> {
  return (await vscode.lm.selectChatModels({ vendor: "copilot", id }))[0];
}

function reportDir(storage: string, ticker: string, date: string): string {
  const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? storage;
  return path.join(root, "tradingagents-reports", ticker, date);
}

async function handle(context: vscode.ExtensionContext, request: vscode.ChatRequest,
  stream: vscode.ChatResponseStream, token: vscode.CancellationToken): Promise<void> {
  stream.markdown(DISCLAIMER);
  if (running) {
    stream.markdown("A run is in progress; wait for it to finish or stop it.");
    return;
  }
  const cfg = vscode.workspace.getConfiguration("tradingagents");
  const parsed = parseRequest(request.prompt, cfg.get<string[]>("analysts") ?? []);
  if (typeof parsed === "string") {
    stream.markdown(parsed);
    return;
  }

  running = true;
  let bridge: Bridge | undefined;
  let child: ChildProcess | undefined;
  try {
    const storage = context.globalStorageUri.fsPath;
    fs.mkdirSync(storage, { recursive: true });
    const runtime = await ensureRuntime(storage, context.extensionPath, context.extension.packageJSON.version,
      (m) => stream.progress(m));
    if (token.isCancellationRequested) return;

    const deep = request.model;
    const quickId = cfg.get<string>("quickModel") || deep.id;
    const quick = quickId === deep.id ? deep : await copilotModel(quickId);
    if (!quick) stream.markdown(`Quick model \`${quickId}\` is not available; using \`${deep.id}\` for every agent.\n\n`);

    const send: Send = async (modelId, turns, tools, required) => {
      const model = modelId === (quick ?? deep).id ? (quick ?? deep) : (await copilotModel(modelId)) ?? deep;
      const response = await model.sendRequest(toLm(turns), {
        justification: "TradingAgents runs its analyst agents on your Copilot models.",
        tools: tools.length ? tools : undefined,
        toolMode: required && tools.length === 1 ? vscode.LanguageModelChatToolMode.Required : vscode.LanguageModelChatToolMode.Auto,
      }, token);
      let text = "";
      const calls: { id: string; name: string; input: object }[] = [];
      for await (const part of response.stream) {
        if (part instanceof vscode.LanguageModelTextPart) text += part.value;
        else if (part instanceof vscode.LanguageModelToolCallPart) calls.push({ id: part.callId, name: part.name, input: part.input });
      }
      return { text, calls };
    };
    bridge = await startBridge(send, async () => (await vscode.lm.selectChatModels({ vendor: "copilot" })).map((m) => m.id));

    const env: NodeJS.ProcessEnv = {
      ...runtime.env,
      PATH: `${runtime.uvDir}${path.delimiter}${process.env.PATH ?? ""}`, // screener.in runs through uv
      TRADINGAGENTS_LLM_PROVIDER: "openai_compatible",
      TRADINGAGENTS_LLM_BACKEND_URL: bridge.url,
      OPENAI_COMPATIBLE_API_KEY: bridge.token,
      TRADINGAGENTS_QUICK_THINK_LLM: (quick ?? deep).id,
      TRADINGAGENTS_DEEP_THINK_LLM: deep.id,
    };
    for (const key of Object.keys(SECRETS)) {
      const value = await context.secrets.get(key);
      if (value) env[key] = value;
    }

    const out = reportDir(storage, parsed.ticker, parsed.date);
    stream.markdown(`Models: quick \`${(quick ?? deep).id}\`, deep \`${deep.id}\`.\n\n`);
    child = spawn(runtime.python, [
      "-m", "tradingagents.vscode_run", "--ticker", parsed.ticker, "--date", parsed.date,
      "--analysts", parsed.analysts.join(","), "--market", cfg.get<string>("market") ?? "IN",
      "--depth", String(cfg.get<number>("researchDepth") ?? 1),
      "--language", cfg.get<string>("outputLanguage") ?? "English", "--out", out,
    ], { cwd: storage, env, stdio: ["ignore", "pipe", "pipe"] });
    const proc = child;
    token.onCancellationRequested(() => proc.kill());
    let stderr = "";
    proc.stderr?.on("data", (d) => (stderr = (stderr + d).slice(-3000)));
    const exited = new Promise<number | null>((resolve) => proc.on("close", resolve));

    let finished = false;
    for await (const line of readline.createInterface({ input: proc.stdout! })) {
      let event: Record<string, string>;
      try {
        event = JSON.parse(line);
      } catch {
        continue;
      }
      if (event.event === "start") {
        stream.progress(`Analyzing ${event.ticker} on ${event.date} (${event.market})…`);
      } else if (event.event === "tool") {
        stream.progress(`Fetching data: ${event.name}`);
      } else if (event.event === "section") {
        stream.markdown(`- ${event.agent}: report ready\n`);
      } else if (event.event === "done") {
        finished = true;
        stream.markdown(`\n## ${parsed.ticker}: ${event.rating}\n\n${event.decision}\n\n`);
        stream.anchor(vscode.Uri.file(path.join(event.report, "complete_report.md")), "Full report (Markdown)");
        stream.markdown("  ");
        stream.anchor(vscode.Uri.file(path.join(event.report, "complete_report.html")), "Full report (HTML)");
      } else if (event.event === "error") {
        finished = true;
        stream.markdown(`**Run failed:** ${event.message}`);
      }
    }
    const code = await exited;
    if (!finished && !token.isCancellationRequested) {
      stream.markdown(`**Run failed** (exit ${code}).\n\n\`\`\`\n${stderr.trim().slice(-1500)}\n\`\`\``);
    }
  } catch (e) {
    if (!token.isCancellationRequested) stream.markdown(`**Run failed:** ${e instanceof Error ? e.message : String(e)}`);
  } finally {
    child?.kill();
    bridge?.close();
    running = false;
  }
}
