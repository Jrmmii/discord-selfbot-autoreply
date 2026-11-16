# Operating guide

Portable notes for running the `jr-indonesia-v1` reply runner. This is a sanitized
derivation of the private operations reference; it contains no account, channel or
message IDs. Fill in your own environment before use.

## Voice baseline

- Short, direct English adapted from the operator's Indonesian replies. Ordinary
  follow-ups, simple reactions, occasional context-based teasing.
- Usually 2–8 words, maximum 12. Natural casing, optional final full stop.
- Ask a concrete question when it fits; let comments stand alone. No formal
  wrap-up, motivational advice or forced closing question.
- No GM phase, standalone messages, emoji decoration, slang abbreviations or
  random fallback templates.
- Identity questions answer from operator-supplied facts only. Unknown numbers
  stay unknown — never fabricate a rank, points total or personal detail.

## Generation rules

- Every send carries `message_reference` to a specific member's message.
- Fail closed on provider error, empty content, truncation, invalid output or
  `SKIP`. Never substitute a template, and never parse reasoning text as the answer.
- Target age is checked before generation, after inference, and again immediately
  before sending (start window 30 s, final freshness limit 90 s).
- One response-length limit shared by prompt and code; overlong replies are
  rejected whole, never sliced into fragments.
- Deduplicate by target ID and by recent reply text so the same person and the
  same phrasing do not repeat back to back.

## Pacing and limits

- One serial sender per runtime directory; interval 24–26 s, poll 4 s, up to three
  drafts prepared ahead.
- Daily cap 250 sends with UTC day rollover, persisted across restarts. Counters
  and deduplication history are never reset on resume.
- Rate limits are honored via `Retry-After`. Terminal configuration, identity or
  delivery-auth failures exit with status 2 and are never auto-restarted by the
  example service.

## Maintenance checklist

1. Before starting, confirm the account and channel in `.env` are the intended ones.
2. Verify identity with `python3 -m jr_indonesia_v1 --dry-run` first; it never sends.
3. Run `python3 -m unittest discover -s tests` after any change to the runner.
4. After starting, check the log for `sent` events and confirm the replies appear
   in the channel with a visible reference. Process status alone is not delivery proof.
5. Before an intentional stop, disable any external restart watchdog, then stop the
   service. Keep the runtime directory so caps and dedup survive the resume.

## Cadence is not throughput

Settings define the ceiling, not a guarantee. Provider latency, skipped targets,
rate limits and the daily cap all reduce actual sends. If volume is too low,
measure provider success, skips, rejections, stale targets and verified sends
separately before changing anything.
