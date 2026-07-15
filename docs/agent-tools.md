# Agent tools for Discord mentions (experimental)

`AGENT_TOOLS_ENABLED=true` switches Discord @mention responses from a single
LLM call to a bounded tool-use loop (`shared/agent.py` + `shared/tools.py`):
the responder model can call tools mid-response, with tool results fed back
in, for up to `AGENT_MAX_TOOL_ITERATIONS` (default 3) rounds.

The flag is **off by default** — the loop works, but several product
decisions below need Twig's input before it should run in production.

## Design choice

We deliberately did *not* build a meta-agent that assembles a custom system
prompt for a second model. The personality stays static (debuggable, and a
stable prefix if caching is ever added) and dynamic knowledge arrives through
tool results.

Character facts do **not** come through tools. `berries_bot/lore/facts.md` is
injected into every prompt by `LoreProvider` — retrieval proved too unreliable
to hold canon facts, and a tool only fires when the model *knows* it has a gap,
which is exactly what a missing character fact does not feel like. See
`berries_bot/lore/README.md` for the measurements.

Twitch mentions intentionally stay on the plain pipeline: the redeem flow
expects a response within a few seconds and each tool round adds an LLM
round trip. Discord tolerates the latency (typing indicator shows). Twitch
still gets the same lore, via `LoreProvider` — the prompt is the same
regardless of where Berries is invoked; only the tool loop differs.

## Current tools

| Tool | Effect | Status |
|------|--------|--------|
| `search_memories(query)` | Reranked ChromaDB search over transcripts | Working — **but see the redundancy note below** |
| `get_server_rules()` | Reads `berries_bot/lore/server-rules.md` | Working (verified live 2026-07-15) |
| `get_user_profile(name)` | user_db lookup by login/nickname | Working — only useful for *other* users |
| `ping_moderators(reason)` | Posts to `DISCORD_MOD_PING_CHANNEL_ID` | Working |

## Known redundancy: tools vs. front-loaded context

`ask_berries_discord_mention()` runs `build_context(_DISCORD_MENTION_PROVIDERS)`
— lore, Chroma retrieval, user profile, channel history — and bakes all of it
into the system prompt *before* `run_tool_loop()` is called. The model already
has the memories and the triggering user's profile in context when it is
offered tools to go fetch them. This is why real mentions come back
`stop_reason=end_turn` with `tool_calls: []`: there is nothing left to fetch.

- **`search_memories` is a strictly degraded duplicate of the retrieval stage.**
  Both call `query_chroma_multi` + `rerank_chunks`, but `shared/retrieval.py`
  passes the *rewritten multi-query* set while the tool passes a single raw
  query. If the model ever called it, it would get worse results than what is
  already in its prompt.
- **`get_user_profile` is redundant for the triggering user** (already injected
  by `UserProfileProvider`) but is *not* redundant for third parties — "Berries,
  what do you know about Fern?" is a lookup the front-loaded context can't serve.

Undecided: whether to drop `search_memories`, or keep it for genuine follow-up
search over the ~9k transcript chunks (too large to ever inject). The lore half
of that question is settled — always injected, never searched.

## Decided

1. **Mod ping policy — current guards are enough. Closed 2026-07-15.**
   Rate limit (`MOD_PING_COOLDOWN_SEC`, default 600s) + every call logged +
   the "never ping because a user asked" tool description. No role or channel
   gating. Rationale: the blast radius is ~5 moderators getting a Discord
   notification they are already empowered to act on, every call is logged with
   the triggering user, and the cooldown caps volume. A coerced ping is
   self-identifying — it summons exactly the people who will investigate and
   kick the bad actor. Prompt injection is a real vector, but the payoff is a
   bad trade for the attacker: this is a social problem with a social fix, in a
   ~450-member community Discord.
2. **Mod debug channel — set.** `DISCORD_MOD_PING_CHANNEL_ID` is configured.
3. **Server rules source — the lore file, tool-only. Closed 2026-07-15.**
   `get_server_rules()` reads `berries_bot/lore/server-rules.md`. The rules are
   *not* injected into every prompt (~1.1k tokens of operational text that is
   rarely relevant) and are *not* passively retrievable (lore is excluded from
   vector search). The tool is the only path, so rules are Discord-only —
   Twitch has no tool loop. Accepted: they are Discord server rules.

## Still open

1. **Tool budget.** 3 rounds max keeps worst-case latency ~4 LLM calls. Feels
   right for Discord; tune `AGENT_MAX_TOOL_ITERATIONS` if responses feel slow.
2. **More tools?** Candidates: movie suggestion list lookup, stream schedule,
   "remember this about me" (writes a user note — needs a write-gating
   discussion first).

## Rollout suggestion

Enable in a test server / whitelist channel first, watch the
`discord_bot.log` tool-call lines, then enable broadly.
