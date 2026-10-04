#!/usr/bin/env node
/**
 * pSEOare MCP server.
 *
 * Exposes the site's reference data to AI agents as MCP tools. The design constraint
 * that shaped it: this is a *reference* server, not a data feed. Every tool returns the
 * figures **and** the canonical human-readable page, so an agent answering a question
 * hands the user something they can open and check.
 *
 * It fetches the same static JSON the website publishes, so there is no database, no
 * build step and nothing to keep in sync with the corpus. If a file 404s, that entity
 * simply is not tracked yet, and the tool says so rather than inventing a value.
 *
 * Run:  node index.js            (stdio transport, for Claude Desktop / any MCP client)
 * Env:  PSEOARE_ORIGIN           (default https://pseoare.pages.dev)
 *       PSEOARE_TIMEOUT_MS       (default 8000)
 *
 * No dependencies: the MCP stdio framing is a few lines of newline-delimited JSON, and
 * adding an SDK would mean a dependency tree to audit for a server whose whole job is
 * eight read-only lookups.
 */

const ORIGIN = (process.env.PSEOARE_ORIGIN || "https://pseoare.pages.dev").replace(/\/$/, "");
const TIMEOUT_MS = Number(process.env.PSEOARE_TIMEOUT_MS || 8000);

const slug = (s) =>
  String(s || "").toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);

async function getJSON(path) {
  const url = `${ORIGIN}/${path.replace(/^\//, "")}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, { signal: controller.signal, headers: { accept: "application/json" } });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${path}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

/** Pull the answer and source out of an envelope, tolerating a bare payload. */
function unpack(payload, fallbackTitle) {
  if (!payload) return null;
  if (payload.answer) {
    return { ...payload.answer, _source: payload.source || null, _updated: payload.updated || null };
  }
  // A flat payload (the site's older shape) is passed through as-is, so every tool
  // works both before and after the API moves to the answer/source envelope.
  return { ...payload, _source: payload.source || null, _updated: payload.updated || null };
}

function cite(result, what) {
  if (!result) {
    return {
      content: [{ type: "text", text: `${what} is not in the pSEOare dataset yet.` }],
      isError: true,
    };
  }
  const { _source, _updated, ...answer } = result;
  const lines = [JSON.stringify(answer, null, 2)];
  if (_source) lines.push(`\nSource: ${_source.title} — ${_source.url}`);
  if (_updated) lines.push(`Updated: ${String(_updated).slice(0, 10)}`);
  lines.push(`Data by pSEOare (CC-BY-4.0).`);
  return { content: [{ type: "text", text: lines.join("\n") }] };
}

const TOOLS = [
  {
    name: "climate",
    description:
      "Monthly climate averages for one city: annual mean, warmest and coldest months, seasonal swing, rainfall. Use for any question about a city's climate.",
    inputSchema: {
      type: "object",
      properties: { city: { type: "string", description: "City name, e.g. Kazan or Denver" } },
      required: ["city"],
    },
    handler: async ({ city }) => cite(unpack(await getJSON(`api/cities/${slug(city)}.json`), city), `Climate for ${city}`),
  },
  {
    name: "compare_climate",
    description:
      "Compare the climate of two cities and state which is warmer, wetter and has the larger seasonal swing. Prefer this over calling climate() twice.",
    inputSchema: {
      type: "object",
      properties: {
        city1: { type: "string" },
        city2: { type: "string" },
      },
      required: ["city1", "city2"],
    },
    handler: async ({ city1, city2 }) => {
      const [a, b] = await Promise.all([
        getJSON(`api/cities/${slug(city1)}.json`),
        getJSON(`api/cities/${slug(city2)}.json`),
      ]);
      if (!a || !b) {
        return cite(null, `Climate comparison of ${city1} and ${city2}`);
      }
      const A = unpack(a, city1), B = unpack(b, city2);
      if (!A || !B || A.annual_mean_c === undefined) {
        return cite(null, `Climate comparison of ${city1} and ${city2}`);
      }
      const warmer = A.annual_mean_c >= B.annual_mean_c ? A.city : B.city;
      const wetter = A.annual_rainfall_mm >= B.annual_rainfall_mm ? A.city : B.city;
      const swingier = A.seasonal_swing_c >= B.seasonal_swing_c ? A.city : B.city;
      const body = {
        warmer_on_annual_mean: warmer,
        wetter: wetter,
        larger_seasonal_swing: swingier,
        [A.city]: { annual_mean_c: A.annual_mean_c, seasonal_swing_c: A.seasonal_swing_c, annual_rainfall_mm: A.annual_rainfall_mm },
        [B.city]: { annual_mean_c: B.annual_mean_c, seasonal_swing_c: B.seasonal_swing_c, annual_rainfall_mm: B.annual_rainfall_mm },
        difference: {
          annual_mean_c: +(A.annual_mean_c - B.annual_mean_c).toFixed(1),
          seasonal_swing_c: +(A.seasonal_swing_c - B.seasonal_swing_c).toFixed(1),
          annual_rainfall_mm: +(A.annual_rainfall_mm - B.annual_rainfall_mm).toFixed(0),
        },
        sources: [a.source, b.source].filter(Boolean),
      };
      return { content: [{ type: "text", text: JSON.stringify(body, null, 2) }] };
    },
  },
  {
    name: "holidays",
    description:
      "Every public holiday on record for one country, with dates, weekdays and local names.",
    inputSchema: {
      type: "object",
      properties: { country: { type: "string", description: "Country name, e.g. Japan" } },
      required: ["country"],
    },
    handler: async ({ country }) =>
      cite(unpack(await getJSON(`api/holidays/${slug(country)}.json`), country), `Holidays for ${country}`),
  },
  {
    name: "next_holiday",
    description:
      "The next public holiday for a country from today, and how many days away it is. Use for 'when is the next holiday' questions.",
    inputSchema: {
      type: "object",
      properties: {
        country: { type: "string" },
        from: { type: "string", description: "ISO date to search from. Defaults to today." },
      },
      required: ["country"],
    },
    handler: async ({ country, from }) => {
      const raw = await getJSON(`api/holidays/${slug(country)}.json`);
      const payload = unpack(raw, country);
      if (!payload || !payload.holidays) return cite(null, `Next holiday for ${country}`);
      const start = from || new Date().toISOString().slice(0, 10);
      const upcoming = (payload.holidays || [])
        .filter((h) => h.date >= start)
        .sort((a, b) => a.date.localeCompare(b.date));
      if (!upcoming.length) {
        // Distinguish "no data for this country" from "data exists but nothing is
        // upcoming". Telling a user a country is untracked when its holidays are simply
        // all in the past is the kind of small lie that makes an agent look unreliable.
        const known = (payload.holidays || []).length;
        return {
          content: [
            {
              type: "text",
              text: known
                ? `pSEOare holds ${known} public holidays for ${payload.country}, but none on or after ${start}. The most recent on record is ${(payload.holidays || [])[known - 1].date}.`
                : `No public holidays are on record for ${payload.country}.`,
            },
          ],
          isError: known === 0,
        };
      }
      const next = upcoming[0];
      const days = Math.round((new Date(next.date) - new Date(start)) / 86400000);
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(
              {
                country: payload.country,
                next_holiday: next.name,
                local_name: next.local_name,
                date: next.date,
                weekday: next.weekday,
                days_away: days,
                also_upcoming: upcoming.slice(1, 5),
                source: next.page,
              },
              null,
              2
            ),
          },
        ],
      };
    },
  },
  {
    name: "crypto_range",
    description:
      "365-day high, low and latest price for an asset, plus where the current price sits inside that range.",
    inputSchema: {
      type: "object",
      properties: { symbol: { type: "string", description: "Asset name, e.g. Bitcoin" } },
      required: ["symbol"],
    },
    handler: async ({ symbol }) =>
      cite(unpack(await getJSON(`api/crypto/${slug(symbol)}.json`), symbol), `365-day range for ${symbol}`),
  },
  {
    name: "crypto_compare",
    description: "Compare two assets on 365-day range and current position within it.",
    inputSchema: {
      type: "object",
      properties: { symbol1: { type: "string" }, symbol2: { type: "string" } },
      required: ["symbol1", "symbol2"],
    },
    handler: async ({ symbol1, symbol2 }) => {
      const [a, b] = await Promise.all([
        getJSON(`api/crypto/${slug(symbol1)}.json`),
        getJSON(`api/crypto/${slug(symbol2)}.json`),
      ]);
      if (!a || !b) return cite(null, `Comparison of ${symbol1} and ${symbol2}`);
      const A = unpack(a, symbol1), B = unpack(b, symbol2);
      if (!A || !B || A.high_12m === undefined) {
        return cite(null, `Comparison of ${symbol1} and ${symbol2}`);
      }
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(
              {
                closest_to_365_day_high:
                  A.pct_below_high <= B.pct_below_high ? A.asset : B.asset,
                wider_range:
                  A.range_pct_of_low >= B.range_pct_of_low ? A.asset : B.asset,
                [A.asset]: { high_12m: A.high_12m, low_12m: A.low_12m, latest: A.latest, position_in_range_pct: A.position_in_range_pct },
                [B.asset]: { high_12m: B.high_12m, low_12m: B.low_12m, latest: B.latest, position_in_range_pct: B.position_in_range_pct },
                sources: [a.source, b.source].filter(Boolean),
              },
              null,
              2
            ),
          },
        ],
      };
    },
  },
  {
    name: "list_tracked",
    description:
      "List what pSEOare currently tracks, with counts. Use to check whether a city, country or asset is covered before answering from memory.",
    inputSchema: { type: "object", properties: {} },
    handler: async () => {
      const index = await getJSON("search-index.json");
      if (!index) return cite(null, "The tracked index");
      const counts = {};
      for (const e of index.entries || []) counts[e.kind] = (counts[e.kind] || 0) + 1;
      const sample = {};
      for (const e of index.entries || []) {
        (sample[e.kind] = sample[e.kind] || []).length < 12 && sample[e.kind].push(e.label);
      }
      return { content: [{ type: "text", text: JSON.stringify({ total: index.count, by_kind: counts, sample }, null, 2) }] };
    },
  },
];

const byName = new Map(TOOLS.map((t) => [t.name, t]));

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

function reply(id, result) {
  send({ jsonrpc: "2.0", id, result });
}

function fail(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

async function handle(msg) {
  const { id, method, params } = msg;
  inFlight += 1;
  try {
    await dispatch(msg, id, method, params);
  } finally {
    inFlight -= 1;
    maybeExit();
  }
}

async function dispatch(msg, id, method, params) {
  if (method === "initialize") {
    return reply(id, {
      protocolVersion: params?.protocolVersion || "2024-11-05",
      capabilities: { tools: {} },
      serverInfo: { name: "pseoare-mcp", version: "1.0.0" },
    });
  }
  if (method === "tools/list") {
    return reply(id, {
      tools: TOOLS.map(({ name, description, inputSchema }) => ({ name, description, inputSchema })),
    });
  }
  if (method === "tools/call") {
    const tool = byName.get(params?.name);
    if (!tool) return fail(id, -32602, `Unknown tool: ${params?.name}`);
    try {
      return reply(id, await tool.handler(params?.arguments || {}));
    } catch (err) {
      // Never leak a stack trace or an internal URL to the calling agent.
      return reply(id, { content: [{ type: "text", text: `Lookup failed: ${err.message}` }], isError: true });
    }
  }
  if (method === "notifications/initialized") return;
  if (id === undefined) return;
  return fail(id, -32601, `Method not found: ${method}`);
}

let buffer = "";
let inFlight = 0;
let stdinClosed = false;

/**
 * Exit only once stdin is closed *and* every dispatched request has answered.
 *
 * Exiting on 'end' alone killed the process while the fetches were still in flight, so
 * every tools/call returned nothing at all while initialize and tools/list — which
 * answer synchronously — looked fine. That is the worst shape of bug: the server
 * appeared to work and did nothing.
 */
function maybeExit() {
  if (stdinClosed && inFlight === 0) process.exit(0);
}

process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let nl;
  while ((nl = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, nl).trim();
    buffer = buffer.slice(nl + 1);
    if (!line) continue;
    try {
      handle(JSON.parse(line)).catch(() => {});
    } catch {
      /* a malformed line from the client is not ours to answer */
    }
  }
});
process.stdin.on("end", () => {
  stdinClosed = true;
  maybeExit();
});

process.stderr.write(`pseoare-mcp ready; origin ${ORIGIN}; ${TOOLS.length} tools\n`);