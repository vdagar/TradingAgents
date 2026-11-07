import assert from "node:assert/strict";
import { test } from "node:test";
import { parseRequest } from "../src/parse";

const ALL = ["market", "sentiment", "news", "fundamentals"];
const NOW = new Date(2026, 10, 7);

test("a bare ticker runs every default analyst today", () => {
  assert.deepEqual(parseRequest("reliance", ALL, NOW), { ticker: "RELIANCE", date: "2026-11-07", analysts: ALL });
});

test("date and analysts in any order", () => {
  assert.deepEqual(parseRequest(" market,News 2026-11-03 TCS.NS ", ALL, NOW),
    { ticker: "TCS.NS", date: "2026-11-03", analysts: ["market", "news"] });
});

test("a future date, a missing ticker, a path, or an extra word is refused", () => {
  assert.match(parseRequest("TCS 2026-12-01", ALL, NOW) as string, /future/);
  assert.match(parseRequest("2026-11-01", ALL, NOW) as string, /Name a ticker/);
  assert.match(parseRequest("..", ALL, NOW) as string, /Could not read/);
  assert.match(parseRequest("../etc", ALL, NOW) as string, /Could not read/);
  assert.match(parseRequest("TCS INFY", ALL, NOW) as string, /Could not read `INFY`/);
});
