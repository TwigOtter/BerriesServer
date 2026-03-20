# TODO

This document serves as a place to store ideas, stubs, to do items, and other "future work" related entries. 
If an item from this file is completed, it should be removed from the document.

## TODO Items

- Set Pronouns method
- Consolidate user profile upserts to a single shared file so that both Twitch and Discord can share methods to set user attributes like nickname pronouns, time zones, region, birthdays, and more.
- Ensure records are merged when linking Twitch and Discord.
- The OMDB movie lookup by string is a bit imprecise and sometimes people remove the wrong movie or Twig risks announcing the wrong movie for movie night.
  - Allow users to remove movies by specifiying their integer value, not by searching (which is kinda unruly)
  - Allow Twig to specify movie to watch by integer from the list, not by searching by name.
- Allow Twig/mods to blacklist movies and provide a reason
- Create a `/event/conversation` `berries_ingest` endpoint that allows Twig to have natural conversations with the Bot.
- Find out why prosody didn't work with TTS
- Discord region/city method
- Berries weather report
- Discord time zone method (tied to location/city?)
- **`/temp <value> <unit> to <unit>`** — convert between F, C, and K; pure math, no external deps.

## Questions/Considerations

- Should Discord/Twitch IDs be the unique qualifiers, not the usernames themselves?
- Should we inject usernames into Discord messages when people tag others via `@username`?
- Can we teach Berries what emotes he's able to use?
- Should the assistant distill relevant information from chatlogs after retrieving and store it back to the ChromaDB as source: "distilled" or something to that effect?
- Should we clear items from the ChromaDB that haven't gotten hits in some amount of time?
