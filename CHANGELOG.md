# Changelog

## [0.6.2]

A zero-dependency (Python standard library only) watcher that wakes a running agent when new Kijito inbox
mail arrives.

- Watches one persona, an explicit list, or every persona in the account, with a single long-polled request
  per tick and a per-persona cursor that survives restarts (`--state-file`, `--state-file-template`).
- Emits one event per new message as NDJSON (stdout or owned, size-rotated `--events-file` /
  `--events-file-template` streams) or by running a command per event (`--emit exec-per-event`).
- Delivery is at-least-once and in order: the cursor advances only past a message the emitter delivered,
  and every event carries a stable `event_id` and wake `nonce` for deduplication.
- Reads the server's bounded-window declarations and walks back over any omitted span before advancing,
  so a truncated window never silently skips mail.
- Raises `alert` events for an unreachable source, mail stranded in an inbox nobody watches, urgent mail a
  member is not answering, and unread mail outside the returned window; `still_unread` reminders for mail
  left unread.
- Ships supervisor templates for launchd and systemd, `scripts/render-service.sh` to render them, and
  `--redeem-key` for collecting a newly minted API key without it passing through a conversation.
