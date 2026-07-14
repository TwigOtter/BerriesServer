# Agent tools for Discord mentions (experimental)

`AGENT_TOOLS_ENABLED=true` switches Discord @mention responses from a single
LLM call to a bounded tool-use loop (`shared/agent.py` + `shared/tools.py`):
the responder model can call tools mid-response, with tool results fed back
in, for up to `AGENT_MAX_TOOL_ITERATIONS` (default 3) rounds.

The flag is **off by default** — the loop works, but several product
decisions below need Twig's input before it should run in production.

## Design choice

We deliberately did *not* build a meta-agent that assembles a custom system
prompt for a second model. The personality stays static (good for prompt
caching, debuggable) and dynamic knowledge arrives through tool results.
Character facts that don't need a tool call (food preferences, lore) are
handled more cheaply by `berries_bot/lore/` + retrieval — see
`berries_bot/lore/README.md`.

Twitch mentions intentionally stay on the plain pipeline: the redeem flow
expects a response within a few seconds and each tool round adds an LLM
round trip. Discord tolerates the latency (typing indicator shows).

## Current tools

| Tool | Effect | Status |
|------|--------|--------|
| `search_memories(query)` | Reranked ChromaDB search | Working |
| `get_server_rules()` | Reads `berries_bot/lore/server-rules.md` | Needs the rules file written |
| `get_user_profile(name)` | user_db lookup by login/nickname | Working |
| `ping_moderators(reason)` | Posts to `DISCORD_MOD_PING_CHANNEL_ID` | Needs channel ID configured |

## Open decisions for Twig

1. **Mod ping policy.** Currently: rate-limited to one ping per
   `MOD_PING_COOLDOWN_SEC` (default 600s), every call logged, and the tool
   description instructs the model to never ping because a user asked it to.
   Is that enough, or should pings also require the *triggering user* to have
   a role / be restricted to specific channels? Prompt injection is the
   threat model: anyone who can mention Berries can try to talk him into it.
2. **Which channel is the mod debug channel?** Set
   `DISCORD_MOD_PING_CHANNEL_ID` in `.env`.
3. **Server rules source.** Write `berries_bot/lore/server-rules.md` (it
   doubles as a lore file, so rules also become passively retrievable), or
   should the tool read the actual #rules channel instead?
4. **Tool budget.** 3 rounds max keeps worst-case latency ~4 LLM calls. Feels
   right for Discord; tune `AGENT_MAX_TOOL_ITERATIONS` if responses feel slow.
5. **More tools?** Candidates: movie suggestion list lookup, stream schedule,
   "remember this about me" (writes a user note — needs a write-gating
   discussion first).

## Rollout suggestion

Enable in a test server / whitelist channel first, watch the
`discord_bot.log` tool-call lines, then enable broadly.
