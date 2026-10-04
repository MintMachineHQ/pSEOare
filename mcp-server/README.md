# pseoare-mcp

An MCP server that gives AI agents structured access to the pSEOare reference data —
city climate averages, public holidays, and 365-day crypto price ranges — **with the
human-readable source page attached to every answer**.

## Why it returns a source

The point is to be a *reference*, not a feed. An agent asked "how does Kazan compare to
Aba?" gets the figures **and** a URL a person can open to check them. A bare number with
no provenance is worse than useless in an answer, because the agent cannot tell whether
it is quoting a measurement or recalling a training example.

## Tools

| Tool | What it answers |
| --- | --- |
| `climate(city)` | monthly averages, annual mean, warmest/coldest month, seasonal swing, rainfall |
| `compare_climate(city1, city2)` | which is warmer, wetter, has the larger swing — one call, not two |
| `holidays(country)` | every public holiday on record, with dates, weekdays, local names |
| `next_holiday(country, from?)` | the next holiday and how many days away it is |
| `crypto_range(symbol)` | 365-day high, low, latest, and position in range |
| `crypto_compare(a, b)` | closest to its 365-day high, wider range |
| `list_tracked()` | what is actually covered, with counts — check before answering from memory |

`list_tracked` exists because the honest failure mode for an agent is answering "Kigali's
climate" confidently from memory when Kigali is not in the dataset. This lets it check
first.

## Running it

```bash
node index.js
```

Speaks newline-delimited JSON-RPC on stdio, which every MCP client understands. No
dependencies, so there is no supply chain to audit for a server whose entire job is
eight read-only lookups.

```bash
PSEOARE_ORIGIN=https://pseoare.pages.dev   # default
PSEOARE_TIMEOUT_MS=8000                    # default
```

Client configuration:

```json
{
  "mcpServers": {
    "pseoare": { "command": "node", "args": ["/path/to/mcp-server/index.js"] }
  }
}
```

## How it works

It fetches the **same static JSON the website already publishes**
(`/api/cities/<name>.json`, `/api/holidays/<country>.json`, `/api/crypto/<name>.json`).
No database, no build step, nothing to keep in sync with the corpus.

That means coverage is exactly the coverage of the site: if a city has not been built
yet, the file 404s and the tool says the entity is not tracked rather than inventing a
figure. It also means the server can go stale for as long as the corpus takes to fill —
the tools report what exists, not what should exist.

The responses are read through one helper that accepts both the flat JSON shape and the
newer `{answer, source}` envelope, so the server works unchanged across a deploy that
moves the site between the two.

## Publishing

```bash
npm publish
```

Then register it in an MCP registry. The package is plain CommonJS-safe ESM with zero
dependencies, so there is nothing to build.

## Licence and attribution

Data CC-BY-4.0, attributed as "Data by pSEOare". Every response carries its source URL,
which is the entire point — please keep it if you fork this.