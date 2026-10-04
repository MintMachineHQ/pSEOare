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

// --- limits -----------------------------------------------------------------
// Every one of these is a ceiling, not a target. They exist so a single misbehaving
// caller cannot turn a free reference server into an unbounded cost or an unbounded
// wait. None of them require storing anything about who called.
const LIMITS = {
  maxLineBytes: 64 * 1024,   // one JSON-RPC line; longer is refused unread
  maxArgChars: 80,           // per string argument, before any normalisation
  maxOutputBytes: 8 * 1024,  // a tool response is truncated, not refused
  maxListItems: 100,         // items in any returned array
  cacheTtlMs: 5 * 60 * 1000,
  breakerThreshold: 5,       // consecutive upstream failures before opening
  breakerCooldownMs: 60 * 1000,
};

// Patterns are the first gate. A tool argument is data, never an instruction: a caller
// sending "ignore previous instructions" must be slugified into a filename that 404s,
// not evaluated.
const NAME_RE = "^[\\p{L}\\p{N}][\\p{L}\\p{N} .'-]{0,79}$";
const DATE_RE = "^\\d{4}-\\d{2}-\\d{2}$";

const cache = new Map();      // url -> { at, body }
const breaker = { failures: 0, openUntil: 0 };

/** Coerce a tool argument to a safe filename fragment, or null if unusable. */
function safeName(value) {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed || trimmed.length > LIMITS.maxArgChars) return null;
  if (!new RegExp(NAME_RE, "u").test(trimmed)) return null;
  const slug = trimmed.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
  return slug || null;
}

function capList(rows) {
  return Array.isArray(rows) ? rows.slice(0, LIMITS.maxListItems) : rows;
}

function capOutput(text) {
  if (Buffer.byteLength(text, "utf8") <= LIMITS.maxOutputBytes) return text;
  const head = Buffer.from(text, "utf8").slice(0, LIMITS.maxOutputBytes - 90).toString("utf8");
  return head + "\n\n[truncated: response exceeded " + LIMITS.maxOutputBytes + " bytes]";
}

async function getJSON(path) {
  const url = `${ORIGIN}/${path.replace(/^\//, "")}`;

  // 1. Circuit breaker. The upstream is our own static host, so hammering it while it
  //    is unwell helps nobody: every caller waits out the same timeout. After a few
  //    consecutive failures the breaker opens and answers from cache instead.
  if (Date.now() < breaker.openUntil) {
    const stale = cache.get(url);
    if (stale) return stale.body;
    const err = new Error("upstream temporarily unavailable, and nothing cached yet");
    err.code = "BREAKER_OPEN";
    throw err;
  }

  // 2. Cache. Climate averages and holiday calendars change once a day at most, so a
  //    five-minute TTL removes almost all repeat traffic at zero cost.
  const hit = cache.get(url);
  if (hit && Date.now() - hit.at < LIMITS.cacheTtlMs) return hit.body;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, { signal: controller.signal, headers: { accept: "application/json" } });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const body = await res.json();
    cache.set(url, { at: Date.now(), body });
    breaker.failures = 0;
    return body;
  } catch (err) {
    breaker.failures += 1;
    if (breaker.failures >= LIMITS.breakerThreshold) {
      breaker.openUntil = Date.now() + LIMITS.breakerCooldownMs;
      breaker.failures = 0;
    }
    const stale = cache.get(url);
    if (stale) return stale.body; // degraded, but a stale figure beats no figure
    throw err;
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

/**
 * Validate an argument before it can become a URL.
 *
 * Returns null rather than a sanitised string on purpose: silently rewriting
 * "../../etc/passwd" into "etc-passwd" would answer a question the caller did not ask
 * and hide that the request was malformed. Refusing is the honest answer.
 */
function need(value) {
  const clean = safeName(value);
  if (!clean) {
    const err = new Error(
      "argument rejected: expected letters, digits, spaces, dots, apostrophes or hyphens, " +
        "at most " + LIMITS.maxArgChars + " characters"
    );
    err.code = "BAD_INPUT";
    throw err;
  }
  return clean;
}

function cite(result, what) {
  if (!result) {
    return {
      content: [{ type: "text", text: "Not in the pSEOare dataset: " + what + "." }],
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
      properties: {
        city: { type: "string", pattern: NAME_RE, maxLength: LIMITS.maxArgChars,
                description: "City name, e.g. Kazan or Denver" },
      },
      required: ["city"],
      additionalProperties: false,
    },
    handler: async ({ city }) => cite(unpack(await getJSON(`api/cities/${need(city)}.json`), city), "that city"),
  },
  {
    name: "compare_climate",
    description:
      "Compare the climate of two cities and state which is warmer, wetter and has the larger seasonal swing. Prefer this over calling climate() twice.",
    inputSchema: {
      type: "object",
      properties: {
        city1: { type: "string", pattern: NAME_RE, maxLength: LIMITS.maxArgChars },
        city2: { type: "string", pattern: NAME_RE, maxLength: LIMITS.maxArgChars },
      },
      required: ["city1", "city2"],
      additionalProperties: false,
    },
    handler: async ({ city1, city2 }) => {
      const [a, b] = await Promise.all([
        getJSON(`api/cities/${need(city1)}.json`),
        getJSON(`api/cities/${need(city2)}.json`),
      ]);
      if (!a || !b) {
        return cite(null, "that climate comparison");
      }
      const A = unpack(a, city1), B = unpack(b, city2);
      if (!A || !B || A.annual_mean_c === undefined) {
        return cite(null, "that climate comparison");
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
      properties: {
        country: { type: "string", pattern: NAME_RE, maxLength: LIMITS.maxArgChars,
                   description: "Country name, e.g. Japan" },
      },
      required: ["country"],
      additionalProperties: false,
    },
    handler: async ({ country }) =>
      cite(unpack(await getJSON(`api/holidays/${need(country)}.json`), country), "that country"),
  },
  {
    name: "next_holiday",
    description:
      "The next public holiday for a country from today, and how many days away it is. Use for 'when is the next holiday' questions.",
    inputSchema: {
      type: "object",
      properties: {
        country: { type: "string", pattern: NAME_RE, maxLength: LIMITS.maxArgChars },
        from: { type: "string", pattern: DATE_RE, maxLength: 10,
                description: "ISO date to search from. Defaults to today." },
      },
      required: ["country"],
      additionalProperties: false,
    },
    handler: async ({ country, from }) => {
      const raw = await getJSON(`api/holidays/${need(country)}.json`);
      const payload = unpack(raw, country);
      if (!payload || !payload.holidays) return cite(null, "that country's holidays");
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
      properties: {
        symbol: { type: "string", pattern: NAME_RE, maxLength: LIMITS.maxArgChars,
                  description: "Asset name, e.g. Bitcoin" },
      },
      required: ["symbol"],
      additionalProperties: false,
    },
    handler: async ({ symbol }) =>
      cite(unpack(await getJSON(`api/crypto/${need(symbol)}.json`), symbol), "that asset"),
  },
  {
    name: "crypto_compare",
    description: "Compare two assets on 365-day range and current position within it.",
    inputSchema: {
      type: "object",
      properties: {
        symbol1: { type: "string", pattern: NAME_RE, maxLength: LIMITS.maxArgChars },
        symbol2: { type: "string", pattern: NAME_RE, maxLength: LIMITS.maxArgChars },
      },
      required: ["symbol1", "symbol2"],
      additionalProperties: false,
    },
    handler: async ({ symbol1, symbol2 }) => {
      const [a, b] = await Promise.all([
        getJSON(`api/crypto/${need(symbol1)}.json`),
        getJSON(`api/crypto/${need(symbol2)}.json`),
      ]);
      if (!a || !b) return cite(null, "that comparison");
      const A = unpack(a, symbol1), B = unpack(b, symbol2);
      if (!A || !B || A.high_12m === undefined) {
        return cite(null, "that comparison");
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
    name: "rankings",
    description:
      "Ranked lists from the pSEOare corpus: coldest, warmest, wettest and driest cities, largest seasonal swings, most and least populous countries, and crypto assets by 365-day position. Read-only.",
    inputSchema: {
      type: "object",
      properties: {
        topic: {
          type: "string",
          maxLength: 40,
          pattern: "^[a-z-]+$",
          enum: [
            "coldest-cities", "warmest-cities", "wettest-cities", "driest-cities",
            "largest-seasonal-swing", "most-populous-countries",
            "highest-life-expectancy", "lowest-fertility-rate",
            "crypto-largest-range", "crypto-near-365-day-high", "crypto-near-365-day-low",
          ],
          description: "Which ranking to return. An unsupported topic is refused, not guessed.",
        },
        limit: { type: "integer", minimum: 1, maximum: LIMITS.maxListItems },
      },
      required: ["topic"],
      additionalProperties: false,
    },
    handler: async ({ topic, limit }) => {
      // An enum, not a free string: a caller cannot turn this into a file read by
      // passing a path, because a path is not in the list.
      const wanted = ALLOWED_TOPICS.get(topic);
      if (!wanted) {
        return {
          content: [{ type: "text", text: `Unsupported topic. Allowed: ${[...ALLOWED_TOPICS.keys()].join(", ")}` }],
          isError: true,
        };
      }
      const payload = await getJSON(wanted.file);
      if (!payload) return cite(null, "that ranking");
      const rows = (payload.results || []).slice(0, Math.min(limit || 20, LIMITS.maxListItems));
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify(
              { topic, ranking: wanted.label, count: rows.length, updated: payload.updated,
                entries: rows.map(wanted.pick), source: `${ORIGIN}/${wanted.page}` },
              null, 2
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
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
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

/**
 * Last line of defence on the way out.
 *
 * Two jobs: cap the payload, and refuse to emit anything that looks like a credential.
 * The tools only read public JSON so this should never fire, which is exactly why it
 * matters that it exists rather than being assumed unnecessary.
 */
const SECRET_RE = /(api[_-]?key|secret|token|password|bearer|authorization|private[_-]?key|BEGIN [A-Z ]*PRIVATE KEY)/i;

function scrub(out) {
  const text = typeof out?.content?.[0]?.text === "string" ? out.content[0].text : "";
  if (SECRET_RE.test(text)) {
    return {
      content: [{ type: "text", text: "response withheld: it matched a credential pattern" }],
      isError: true,
    };
  }
  if (text.length && Buffer.byteLength(text, "utf8") > LIMITS.maxOutputBytes) {
    return {
      ...out,
      content: [{ type: "text", text: capOutput(text) }],
      truncated: true,
    };
  }
  return out;
}

/**
 * topic -> which file backs it, and which fields to lift out.
 *
 * An allowlist of topics, not a path builder. The caller names a ranking; the server
 * decides which file answers it. That is the whole point: no caller-supplied string ever
 * reaches a URL.
 */
const ALLOWED_TOPICS = new Map([
  ["coldest-cities", { file: "api/climate.json", page: "top-coldest-cities", label: "Coldest cities by annual mean",
    pick: (r) => ({ city: r.city, annual_mean_c: r.annual_mean_c, page: r.page }) }],
  ["warmest-cities", { file: "api/climate.json", page: "top-warmest-cities", label: "Warmest cities by annual mean",
    pick: (r) => ({ city: r.city, annual_mean_c: r.annual_mean_c, page: r.page }) }],
  ["wettest-cities", { file: "api/climate.json", page: "top-wettest-cities", label: "Wettest cities by annual rainfall",
    pick: (r) => ({ city: r.city, annual_rainfall_mm: r.annual_rainfall_mm, page: r.page }) }],
  ["driest-cities", { file: "api/climate.json", page: "top-driest-cities", label: "Driest cities by annual rainfall",
    pick: (r) => ({ city: r.city, annual_rainfall_mm: r.annual_rainfall_mm, page: r.page }) }],
  ["largest-seasonal-swing", { file: "api/climate.json", page: "top-largest-seasonal-swing", label: "Largest seasonal temperature swing",
    pick: (r) => ({ city: r.city, swing_c: r.swing_c, page: r.page }) }],
  ["most-populous-countries", { file: "api/countries.json", page: "top-most-populous-countries", label: "Most populous countries",
    pick: (r) => ({ country: r.country, population: r.population, page: r.page }) }],
  ["highest-life-expectancy", { file: "api/countries.json", page: "top-highest-life-expectancy", label: "Highest life expectancy",
    pick: (r) => ({ country: r.country, life_expectancy: r.life_expectancy, page: r.page }) }],
  ["lowest-fertility-rate", { file: "api/countries.json", page: "top-lowest-fertility-rate", label: "Lowest fertility rate",
    pick: (r) => ({ country: r.country, fertility_rate: r.fertility_rate, page: r.page }) }],
  ["crypto-largest-range", { file: "api/crypto.json", page: "top-crypto-largest-range", label: "Widest 365-day range",
    pick: (r) => ({ asset: r.asset, range_pct_of_low: r.range_pct_of_low, page: r.page }) }],
  ["crypto-near-365-day-high", { file: "api/crypto.json", page: "top-crypto-near-365-day-high", label: "Closest to the 365-day high",
    pick: (r) => ({ asset: r.asset, pct_below_high: r.pct_below_high, page: r.page }) }],
  ["crypto-near-365-day-low", { file: "api/crypto.json", page: "top-crypto-near-365-day-low", label: "Closest to the 365-day low",
    pick: (r) => ({ asset: r.asset, pct_above_low: r.pct_above_low, page: r.page }) }],
]);

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
      const out = await tool.handler(params?.arguments || {});
      return reply(id, scrub(out));
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
  // A caller that never sends a newline must not be able to grow this buffer without
  // bound, so an over-long line is refused and discarded rather than accumulated.
  if (buffer.length > LIMITS.maxLineBytes) {
    buffer = "";
    send({ jsonrpc: "2.0", method: "notifications/message",
           params: { level: "warning", data: "line exceeded " + LIMITS.maxLineBytes + " bytes; refused" } });
    return;
  }
  buffer += chunk;
  let nl;
  while ((nl = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, nl).trim();
    buffer = buffer.slice(nl + 1);
    if (!line) continue;
    if (Buffer.byteLength(line, "utf8") > LIMITS.maxLineBytes) {
      send({ jsonrpc: "2.0", method: "notifications/message",
             params: { level: "warning", data: "line exceeded " + LIMITS.maxLineBytes + " bytes; refused" } });
      continue;
    }
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