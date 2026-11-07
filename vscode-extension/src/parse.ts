// Chat input to a run request, by plain rules: no model call just to read a ticker.

export const ANALYSTS = ["market", "sentiment", "news", "fundamentals"] as const;

export interface RunRequest {
  ticker: string;
  date: string;
  analysts: string[];
}

const DATE = /^\d{4}-\d{2}-\d{2}$/;
// Also a path component of the report folder: no slash, and not dots alone.
const TICKER = /^(?=.*[A-Za-z0-9])[A-Za-z0-9.^=+-]{1,32}$/;

export function today(now = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** `RELIANCE [YYYY-MM-DD] [market,news]`; an error message when it is not that. */
export function parseRequest(prompt: string, defaultAnalysts: string[], now = new Date()): RunRequest | string {
  const usage = "Usage: `@tradingagents /analyze TICKER [YYYY-MM-DD] [market,sentiment,news,fundamentals]`";
  let ticker = "";
  let date = today(now);
  let analysts = defaultAnalysts;
  for (const token of prompt.trim().split(/\s+/).filter(Boolean)) {
    const names = token.toLowerCase().split(",").filter(Boolean);
    if (DATE.test(token)) {
      date = token;
    } else if (names.length && names.every((n) => (ANALYSTS as readonly string[]).includes(n))) {
      analysts = names;
    } else if (!ticker && TICKER.test(token)) {
      ticker = token.toUpperCase();
    } else {
      return `Could not read \`${token}\`. ${usage}`;
    }
  }
  if (!ticker) return `Name a ticker. ${usage}`;
  if (date > today(now)) return `${date} is in the future.`;
  return { ticker, date, analysts };
}
