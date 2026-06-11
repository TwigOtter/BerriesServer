"""
discord_bot/services.py

Thin async clients for the external HTTP APIs the bot talks to (OMDb, Giphy).
Stateless module functions — no Discord coupling, safe to import anywhere.
"""

import logging
import random

import httpx

from shared.config import GIPHY_API_KEY, OMDB_API_KEY

log = logging.getLogger("discord_bot.services")


async def omdb_search(title: str) -> dict | None:
    """Search OMDb for a movie by title. Returns the first result dict or None."""
    results = await omdb_search_many(title, limit=1)
    return results[0] if results else None


async def omdb_search_many(title: str, limit: int = 5) -> list[dict]:
    """Search OMDb for a movie by title. Returns up to `limit` result dicts."""
    if not OMDB_API_KEY:
        log.debug("OMDB_API_KEY not set; skipping OMDb search")
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.omdbapi.com/",
                params={"s": title, "type": "movie", "apikey": OMDB_API_KEY},
                timeout=5.0,
            )
        data = resp.json()
        if data.get("Response") == "True" and data.get("Search"):
            results = data["Search"][:limit]
            log.debug("OMDb found %d result(s) for %r (returning %d)", len(data["Search"]), title, len(results))
            return results
        log.debug("OMDb returned no results for %r: %s", title, data.get("Error", "unknown"))
    except Exception:
        log.exception("OMDb search failed for %r", title)
    return []


async def fetch_gif(query: str) -> str | None:
    """Search Giphy and return a random GIF URL from the top results."""
    log.debug("Fetching GIF for query: %.80r", query)
    if not GIPHY_API_KEY:
        log.debug("GIPHY_API_KEY not set; skipping GIF search")
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.giphy.com/v1/gifs/search",
                params={"q": query, "api_key": GIPHY_API_KEY, "limit": 8, "rating": "pg-13"},
                timeout=5.0,
            )
        results = resp.json().get("data", [])
        if not results:
            log.debug("Giphy returned no results for %r", query)
            return None
        pick = random.choice(results[:5])
        return pick.get("images", {}).get("original", {}).get("url")
    except Exception:
        log.exception("Giphy search failed for %r", query)
        return None
