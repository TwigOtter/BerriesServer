# Streamer.bot Setup Checklist

## One-Time Global Variables
Set these in Streamer.bot under **Globals** (persisted):

- [x] `Berries_IngestUrl` — base URL of the ingest server, e.g. `http://192.168.x.x:8000`
- [x] `Berries_IngestSecret` — must match `INGEST_SECRET` in the server `.env`

---

## Actions

### 1. Chat Message → `/event/chat`
**Trigger:** Twitch → Chat Message

**Sub-action:** Execute C# Code (`sb_code/SendToIngest.cs`)

**Arguments to set:**
- `requestUrl` → `http://%Berries_IngestUrl%/event/chat`
- `ingestString` → the JSON payload below

**Payload template:**
```json
{"userName": "%userName%", "displayName": "%user%", "userId": "%userId%", "msgId": "%msgId%", "message": "%message%", "messageStripped": "%messageStripped%", "emoteCount": "%emoteCount%", "role": "%role%", "bits": "%bits%", "firstMessage": "%firstMessage%", "isSubscribed": "%isSubscribed%", "subscriptionTier": "%subscriptionTier%", "monthsSubscribed": "%monthsSubscribed%", "isVip": "%isVip%", "isModerator": "%isModerator%"}
```

- [x] Action created
- [x] Trigger attached
- [x] C# code loaded
- [x] Arguments set
- [x] Tested locally (PowerShell → `{"status": "ok"}`)

---

### 2. Speech-to-Text → `/event/speech`
**Trigger:** whichever STT action fires in your setup

**Payload template:**
```json
{"speaker": "%broadcastUser%", "text": "%spokenText%"}
```
> `speaker` should be the streamer's display name. `text` is whatever variable your STT action exposes.

- [x] Action created
- [x] Trigger attached
- [x] Arguments set

---

### 3. Stream Metadata Update → `/event/stream-update`
**Trigger:** Twitch → Stream Update (title/category change)

**Payload template:**
```json
{"title": "%streamTitle%", "category": "%gameName%"}
```

- [x] Action created
- [x] Trigger attached
- [x] Arguments set

---

### 4. Generic Stream Events → `/event/stream`
**Trigger:** One action per event type, all pointing at the same endpoint.
Set `type` to a short slug and `text` to a human-readable pre-formatted string.

**Payload template (per event type):**

| Event | `type` | `text` example |
|---|---|---|
| New subscription | `subscription` | `%userName% just subscribed at Tier %subscriptionTier% for %monthsSubscribed% months!` |
| Resub | `resub` | `%userName% resubscribed for %monthsSubscribed% months!` |
| Gift sub | `giftsub` | `%userName% gifted a sub to %recipientName%!` |
| Raid | `raid` | `%raiderName% raided with %viewerCount% viewers!` |
| Prediction started | `prediction` | `Prediction started: '%title%'` |
| Prediction ended | `prediction` | `Prediction '%title%' ended. Winner: %winningTitle%` |
| Poll started | `poll` | `Poll started: '%title%'` |
| Poll ended | `poll` | `Poll '%title%' ended. Winner: %winningTitle%` |
| Hype Train | `hypetrain` | `Hype Train level %level% reached!` |
| Channel Point Redeem | `redeem` | `%userName% redeemed '%rewardTitle%'` |

- [x] Subscription action created
- [x] Resub action created
- [x] Gift sub action created
- [x] Raid action created
- [x] Prediction actions created
- [x] Poll actions created
- [x] Channel Point Redeem action(s) created

---

### 5. Going Live → `/event/going-live`
**Trigger:** Streamer.bot stream start / "On Connected" or a manual "Go Live" button

**Payload template:**
```json
{"title": "%streamTitle%", "category": "%gameName%"}
```

> This forwards to the Discord bot to post a go-live announcement.

- [x] Action created
- [x] Trigger attached
- [x] Arguments set

---

### 6. Mention / Berries Response → `/event/mention`
**Trigger:** Twitch → Chat Message (keyword filter: `@berries` or however you want to trigger him)

**Payload template:**
```json
{"text": "[%userName%] %message%", "respond": true, "TTS": false}
```

> Set `TTS` to `true` if you want the response read aloud via SpeakerBot.
> Berries' reply will be POSTed back to Streamer.bot at `STREAMERBOT_CALLBACK_URL` and sent to chat automatically.

- [ ] Action created
- [ ] Trigger attached (keyword / hotkey / channel point)
- [ ] Arguments set
- [ ] End-to-end tested (mention → reply appears in chat)
