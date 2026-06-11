"""
discord_bot/cogs/movies.py

/movie slash command group: suggestions (add/list/remove with voting UI)
and watch history (add/list/remove). Movie identity comes from OMDb.
"""

import json
import logging
import uuid

import discord
from discord import app_commands
from discord.ext import commands

from shared.movie_db import (
    add_suggestion,
    get_all_suggestions,
    get_all_watched,
    get_recent_watched,
    get_suggestion,
    mark_watched,
    remove_suggestion,
    remove_watched,
    toggle_vote,
)
from discord_bot.services import omdb_search, omdb_search_many

log = logging.getLogger("discord_bot.movies")


# ── Movie disambiguation UI ────────────────────────────────────────────────

class MovieSelectView(discord.ui.View):
    """Ephemeral dropdown for /movie suggest add disambiguation."""

    def __init__(self, results: list[dict], invoker: discord.User | discord.Member) -> None:
        super().__init__(timeout=60)
        self.selected: dict | None = None
        self.cancelled: bool = False
        self._results = results
        self._invoker = invoker

        options = [
            discord.SelectOption(label=f"{m['Title']} ({m['Year']})"[:100], value=str(i))
            for i, m in enumerate(results)
        ]
        options.append(discord.SelectOption(label="Cancel", value="cancel"))

        select = discord.ui.Select(placeholder="Pick a movie...", options=options)
        select.callback = self._callback
        self.add_item(select)

    async def _callback(self, interaction: discord.Interaction) -> None:
        if interaction.user != self._invoker:
            await interaction.response.send_message("This isn't your selection.", ephemeral=True)
            return
        value = interaction.data["values"][0]
        if value == "cancel":
            self.cancelled = True
        else:
            self.selected = self._results[int(value)]
        await interaction.response.defer()
        self.stop()


# ── Movie suggestion list UI ────────────────────────────────────────────────

_MOVIE_LIST_PAGE_SIZE = 20

_SORT_MODES = ["added", "votes", "title", "year"]
_SORT_LABELS = {
    "added": "Added ↑",
    "votes": "Top Voted",
    "title": "Title A–Z",
    "year": "Year",
}


def _sort_movies(movies: list[dict], mode: str) -> list[dict]:
    """Return a sorted copy of the suggestion list."""
    if mode == "votes":
        return sorted(movies, key=lambda m: (-len(json.loads(m.get("voters") or "[]")), m["suggested_at"]))
    if mode == "title":
        return sorted(movies, key=lambda m: m["title"].lower())
    if mode == "year":
        return sorted(movies, key=lambda m: (m.get("year") or "0", m["title"].lower()))
    # "added" — oldest first
    return sorted(movies, key=lambda m: m["suggested_at"])


def _build_movie_list_embed(
    movies: list[dict], page: int, total_pages: int, sort_mode: str, user_id: str
) -> discord.Embed:
    """Build an embed for one page of the movie suggestion list."""
    embed = discord.Embed(title="Movie Night Suggestions", color=discord.Color.dark_purple())
    start = page * _MOVIE_LIST_PAGE_SIZE
    page_movies = movies[start : start + _MOVIE_LIST_PAGE_SIZE]
    lines = []
    for i, m in enumerate(page_movies, 1):
        voters = json.loads(m.get("voters") or "[]")
        voted = user_id in voters
        vote_str = f" · 👍 {len(voters)}" if voters else ""
        check = "✅ " if voted else ""
        lines.append(
            f"{check}**{start + i}. {m['title']}** ({m['year']}){vote_str}\n"
            f"*suggested by {m['suggested_by']}*"
        )
    embed.description = "\n\n".join(lines) if lines else "*Nothing here.*"
    total = len(movies)
    embed.set_footer(
        text=(
            f"Page {page + 1} of {total_pages} · "
            f"{total} movie{'s' if total != 1 else ''} suggested · "
            f"Sort: {_SORT_LABELS[sort_mode]}"
        )
    )
    return embed


class _VoteSelectMenu(discord.ui.Select):
    """Dropdown for toggling a vote on one movie from the current page."""

    def __init__(
        self, page_movies: list[dict], page: int, user_id: str, view_id: str
    ) -> None:
        start = page * _MOVIE_LIST_PAGE_SIZE
        options = []
        for i, m in enumerate(page_movies):
            voters = json.loads(m.get("voters") or "[]")
            voted = user_id in voters
            vote_count = len(voters)
            position = start + i + 1
            prefix = "✅ " if voted else ""
            label = f"{prefix}#{position}. {m['title']} ({m['year']})"[:100]
            vote_word = "vote" if vote_count == 1 else "votes"
            desc = f"👍 {vote_count} {vote_word} · suggested by {m['suggested_by']}"[:100]
            options.append(
                discord.SelectOption(label=label, value=m["imdb_id"], description=desc)
            )
        super().__init__(
            placeholder="🗳️ Pick a movie to toggle your vote...",
            options=options,
            custom_id=f"mvotemenu::{view_id}",
            row=0,
            min_values=1,
            max_values=1,
        )
        self._page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        v = self.view  # type: MovieListView
        imdb_id = self.values[0]
        movie_title = next((m["title"] for m in v._movies if m["imdb_id"] == imdb_id), imdb_id)
        now_voted, total_votes = toggle_vote(imdb_id, str(interaction.user.id))

        movies = get_all_suggestions()
        sorted_movies = _sort_movies(movies, v._sort_mode)
        total_pages = max(1, (len(sorted_movies) + _MOVIE_LIST_PAGE_SIZE - 1) // _MOVIE_LIST_PAGE_SIZE)
        page = min(self._page, total_pages - 1)
        user_id = str(interaction.user.id)
        embed = _build_movie_list_embed(sorted_movies, page, total_pages, v._sort_mode, user_id)
        new_view = MovieListView(sorted_movies, page, v._sort_mode, user_id, view_id=v._view_id)
        action = "voted for" if now_voted else "removed your vote from"
        vote_word = "vote" if total_votes == 1 else "votes"
        log.info("Vote toggle: %s %s %r (%d %s)", interaction.user, action, movie_title, total_votes, vote_word)
        await interaction.response.edit_message(embed=embed, view=new_view)


class _PrevButton(discord.ui.Button):
    def __init__(self, current_page: int, view_id: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="◀ Prev",
            custom_id=f"mprev::{view_id}::{current_page}",
            disabled=current_page == 0,
            row=1,
        )
        self._page = current_page

    async def callback(self, interaction: discord.Interaction) -> None:
        v = self.view  # type: MovieListView
        movies = get_all_suggestions()
        sorted_movies = _sort_movies(movies, v._sort_mode)
        total_pages = max(1, (len(sorted_movies) + _MOVIE_LIST_PAGE_SIZE - 1) // _MOVIE_LIST_PAGE_SIZE)
        new_page = max(0, min(self._page - 1, total_pages - 1))
        user_id = str(interaction.user.id)
        embed = _build_movie_list_embed(sorted_movies, new_page, total_pages, v._sort_mode, user_id)
        new_view = MovieListView(sorted_movies, new_page, v._sort_mode, user_id, view_id=v._view_id)
        await interaction.response.edit_message(embed=embed, view=new_view)


class _PageLabelButton(discord.ui.Button):
    def __init__(self, page: int, total_pages: int, view_id: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=f"{page + 1} / {total_pages}",
            custom_id=f"plabel::{view_id}",
            disabled=True,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        pass  # Disabled; callback is never reached


class _NextButton(discord.ui.Button):
    def __init__(self, current_page: int, total_pages: int, view_id: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Next ▶",
            custom_id=f"mnext::{view_id}::{current_page}",
            disabled=current_page >= total_pages - 1,
            row=1,
        )
        self._page = current_page

    async def callback(self, interaction: discord.Interaction) -> None:
        v = self.view  # type: MovieListView
        movies = get_all_suggestions()
        sorted_movies = _sort_movies(movies, v._sort_mode)
        total_pages = max(1, (len(sorted_movies) + _MOVIE_LIST_PAGE_SIZE - 1) // _MOVIE_LIST_PAGE_SIZE)
        new_page = min(self._page + 1, total_pages - 1)
        user_id = str(interaction.user.id)
        embed = _build_movie_list_embed(sorted_movies, new_page, total_pages, v._sort_mode, user_id)
        new_view = MovieListView(sorted_movies, new_page, v._sort_mode, user_id, view_id=v._view_id)
        await interaction.response.edit_message(embed=embed, view=new_view)


class _SortButton(discord.ui.Button):
    def __init__(self, sort_mode: str, view_id: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=f"🔃 {_SORT_LABELS[sort_mode]}",
            custom_id=f"msort::{view_id}::{sort_mode}",
            row=1,
        )
        self._sort_mode = sort_mode

    async def callback(self, interaction: discord.Interaction) -> None:
        v = self.view  # type: MovieListView
        next_idx = (_SORT_MODES.index(self._sort_mode) + 1) % len(_SORT_MODES)
        next_mode = _SORT_MODES[next_idx]
        movies = get_all_suggestions()
        sorted_movies = _sort_movies(movies, next_mode)
        total_pages = max(1, (len(sorted_movies) + _MOVIE_LIST_PAGE_SIZE - 1) // _MOVIE_LIST_PAGE_SIZE)
        user_id = str(interaction.user.id)
        embed = _build_movie_list_embed(sorted_movies, 0, total_pages, next_mode, user_id)
        new_view = MovieListView(sorted_movies, 0, next_mode, user_id, view_id=v._view_id)
        await interaction.response.edit_message(embed=embed, view=new_view)


class MovieListView(discord.ui.View):
    """Private per-user paginated movie list with vote dropdown and sort cycling."""

    def __init__(
        self,
        movies: list[dict],
        page: int = 0,
        sort_mode: str = "added",
        user_id: str = "",
        view_id: str | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self._movies = movies
        self._page = page
        self._sort_mode = sort_mode
        self._user_id = user_id
        self._view_id = view_id or uuid.uuid4().hex[:8]

        total_pages = max(1, (len(movies) + _MOVIE_LIST_PAGE_SIZE - 1) // _MOVIE_LIST_PAGE_SIZE)
        start = page * _MOVIE_LIST_PAGE_SIZE
        page_movies = movies[start : start + _MOVIE_LIST_PAGE_SIZE]

        self.add_item(_VoteSelectMenu(page_movies, page, user_id, self._view_id))
        self.add_item(_PrevButton(page, self._view_id))
        self.add_item(_PageLabelButton(page, total_pages, self._view_id))
        self.add_item(_NextButton(page, total_pages, self._view_id))
        self.add_item(_SortButton(sort_mode, self._view_id))


# ── /movie subcommand group ─────────────────────────────────────────────────

movie_group = app_commands.Group(name="movie", description="Movie night commands")
suggest_group = app_commands.Group(name="suggest", description="Manage movie suggestions", parent=movie_group)
history_group = app_commands.Group(name="history", description="View and manage movie watch history", parent=movie_group)


@suggest_group.command(name="add", description="Suggest a movie for movie night")
@app_commands.describe(title="Movie title to search for")
async def movie_suggest_add(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer(ephemeral=True)

    results = await omdb_search_many(title, limit=5)
    if not results:
        await interaction.followup.send(
            f"*rustles in the shadows* ...I couldn't find **{title}** on OMDb. Try a more specific title?",
            ephemeral=True,
        )
        return

    # If multiple results, show an ephemeral dropdown — only the invoking user sees it
    if len(results) > 1:
        lines = [f"*Found a few matches for **{title}**. Which one did you mean?*\n"]
        for i, m in enumerate(results, 1):
            lines.append(f"{i}. **{m['Title']}** ({m['Year']})")

        view = MovieSelectView(results, interaction.user)
        await interaction.followup.send("\n".join(lines), view=view, ephemeral=True)
        await view.wait()

        if view.cancelled:
            await interaction.followup.send(
                "*retreats into the forest* ...alright, cancelled.", ephemeral=True
            )
            return
        if view.selected is None:
            await interaction.followup.send(
                "*the shadows grow quiet* ...selection timed out. Run `/movie suggest add` again if you'd like to try.",
                ephemeral=True,
            )
            return

        result = view.selected
    else:
        result = results[0]

    imdb_id = result["imdbID"]
    movie_title = result["Title"]
    year = result["Year"]

    existing = get_suggestion(imdb_id)
    if existing and existing["status"] == "suggested":
        await interaction.followup.send(
            f"**{movie_title} ({year})** is already on the suggestion list!", ephemeral=True
        )
        return

    recent = get_recent_watched(365)
    recently_watched = next((m for m in recent if m["imdb_id"] == imdb_id), None)
    if recently_watched:
        watched_date = recently_watched["watched_at"][:10]
        rejection = f"Hey, we just watched that one back on {watched_date}! Let's give something else a chance. :3"
        await interaction.followup.send(rejection, ephemeral=True)
        return

    add_suggestion(imdb_id, movie_title, year, interaction.user.display_name)
    log.info("Added suggestion %r (%s) by %s", movie_title, imdb_id, interaction.user)
    # Post public confirmation so the channel knows what was added
    await interaction.channel.send(
        f"**{interaction.user.display_name}** added **{movie_title} ({year})** to the movie night suggestion list!"
    )


@suggest_group.command(name="list", description="See the current movie night suggestion list")
async def movie_suggest_list(interaction: discord.Interaction) -> None:
    suggestions = get_all_suggestions()
    if not suggestions:
        await interaction.response.send_message(
            "*peers out from the forest* ...no movies have been suggested yet.",
            ephemeral=True,
        )
        return

    sorted_movies = _sort_movies(suggestions, "added")
    total_pages = max(1, (len(sorted_movies) + _MOVIE_LIST_PAGE_SIZE - 1) // _MOVIE_LIST_PAGE_SIZE)
    user_id = str(interaction.user.id)
    embed = _build_movie_list_embed(sorted_movies, 0, total_pages, "added", user_id)
    view = MovieListView(sorted_movies, 0, "added", user_id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@suggest_group.command(name="remove", description="Remove a movie from the suggestion list")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(title="Title of the movie to remove")
async def movie_suggest_remove(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer()

    result = await omdb_search(title)
    if not result:
        await interaction.followup.send(
            f"*rustles in the shadows* ...couldn't find **{title}** on OMDb. Try a more specific title?"
        )
        return

    imdb_id = result["imdbID"]
    movie_title = result["Title"]
    year = result["Year"]

    removed = remove_suggestion(imdb_id)
    if removed:
        log.info("Removed suggestion %r (%s) by %s", movie_title, imdb_id, interaction.user)
        await interaction.followup.send(f"Removed **{movie_title} ({year})** from the suggestion list.")
    else:
        await interaction.followup.send(
            f"**{movie_title} ({year})** isn't on the suggestion list."
        )


@history_group.command(name="add", description="Mark a movie as watched")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(title="Title of the movie to mark as watched")
async def movie_history_add(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer()

    result = await omdb_search(title)
    if not result:
        await interaction.followup.send(f"Couldn't find **{title}** on OMDb.")
        return

    imdb_id = result["imdbID"]
    movie_title = result["Title"]
    year = result["Year"]

    # Ensure the movie exists in the DB before marking watched
    if not get_suggestion(imdb_id):
        add_suggestion(imdb_id, movie_title, year, interaction.user.display_name)
    mark_watched(imdb_id)

    log.info("Marked %r (%s) watched by %s", movie_title, imdb_id, interaction.user)
    await interaction.followup.send(f"Marked **{movie_title} ({year})** as watched.")


@history_group.command(name="list", description="See movies we've already watched")
async def movie_history_list(interaction: discord.Interaction) -> None:
    watched = get_all_watched()
    if not watched:
        await interaction.response.send_message(
            "*tilts head* ...we haven't watched anything yet. Time to fix that."
        )
        return

    lines = ["**Movies We've Watched**\n"]
    for m in watched[:20]:
        date = m["watched_at"][:10] if m["watched_at"] else "?"
        lines.append(f"• **{m['title']}** ({m['year']}) — {date}")
    if len(watched) > 20:
        lines.append(f"\n*...and {len(watched) - 20} more*")
    await interaction.response.send_message("\n".join(lines))


@history_group.command(name="remove", description="Remove a movie from the watch history")
@app_commands.default_permissions(manage_messages=True)
@app_commands.describe(title="Title of the movie to remove from history")
async def movie_history_remove(interaction: discord.Interaction, title: str) -> None:
    await interaction.response.defer()

    result = await omdb_search(title)
    if not result:
        await interaction.followup.send(
            f"*rustles in the shadows* ...couldn't find **{title}** on OMDb. Try a more specific title?"
        )
        return

    imdb_id = result["imdbID"]
    movie_title = result["Title"]
    year = result["Year"]

    removed = remove_watched(imdb_id)
    if removed:
        log.info("Removed %r (%s) from watch history by %s", movie_title, imdb_id, interaction.user)
        await interaction.followup.send(f"Removed **{movie_title} ({year})** from the watch history.")
    else:
        await interaction.followup.send(
            f"**{movie_title} ({year})** isn't in the watch history."
        )


async def setup(bot: commands.Bot) -> None:
    bot.tree.add_command(movie_group)
