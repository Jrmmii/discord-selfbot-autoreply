# Discord Auto-Reply

Reply runner for a user account in a single Discord channel, using the approved
`jr-indonesia-v1` voice: short English replies, ordinary follow-ups and occasional
teasing grounded in the conversation. Generation goes through a local Hermes CLI;
no message is ever sent without a reply reference, and failed generation stays
silent instead of falling back to a template.

## Layout

- `jr_indonesia_v1/` — the runner. `python3 -m jr_indonesia_v1` starts it; the
  package is self-contained and importable.
- `docs/operations.md` — voice baseline, generation rules, pacing limits and the
  maintenance checklist.
- `deploy/` — example systemd user service.
- `tests/` — offline tests with mocked Discord and Hermes transports.

## Behavior

- Reads recent messages with their quoted parents and passes the context to the model.
- Usually responds in 2–8 words, maximum 12. Short readable phrases and natural casing are allowed.
- Sends only replies with `message_reference`; no standalone GM phase or mass greetings.
- One serial sender, 24–26 s interval, a worker preparing up to three drafts, polling every 4 s.
- Starts generation only on targets at most 30 s old and rejects drafts older than 90 s. Refreshes the exact target before sending.
- Honors rate limits, stops on Discord authentication/permission errors, and sends nothing when generation fails. Terminal configuration, identity or delivery-auth failures exit with status 2, which the example service is configured never to auto-restart.
- Preserves state and a 250-send cap per UTC day across restarts.
- Verifies configured user ID and username against `/users/@me` before enabling sends.

Cadence settings are not a throughput guarantee. Provider delays, skipped messages, rate limits and the cap affect actual delivery. A reply reference does not prove server-side reward credit.

## Requirements

- Python 3.9+ on a POSIX system. Live mode uses `fcntl` for a local sender lock.
- A working [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) CLI with `chat --oneshot --query-file - --format stream-json` and the flags in `jr_indonesia_v1/hermes_llm.py`.
- An authorized provider configured in Hermes. Defaults preserve the selected deployment: `gpt-6-astra` through `custom:api.xingchiapi.com`. Access is not included; use your own configured provider and explicitly set the model/provider variables if different.

The runner uses Python's standard library. It does not require `requests` or the OpenAI SDK. It uses the supported Hermes CLI rather than impersonating an allowed API client. Hermes can retain prompts in its own local session storage; protect that storage and review the configured provider's privacy terms.

## Configure

Copy `.env.example` to `.env` and fill it locally. Never commit credentials. Required:

- `DISCORD_TOKEN`
- `DISCORD_USER_ID`
- `DISCORD_USERNAME`
- `DISCORD_GUILD_ID`
- `DISCORD_CHANNEL_ID`

Optional:

- `DISCORD_AUTOREPLY_DATA_DIR`: local state directory, default `.runtime` relative to the working directory. Use one per account/channel; preserve it when restarting.
- `HERMES_MODEL` and `HERMES_PROVIDER`: explicit model/provider selection.
- `DISCORD_COUNTRY_OF_ORIGIN`: an operator-confirmed fact. Empty means unknown; no nationality is assumed.

The runner does not auto-load `.env`. Load your own trusted file before starting:

```bash
set -a
. ./.env
set +a
```

The public profile contains no original account, channel or message IDs. State, logs, `.env` and rank caches are gitignored. Keep custom data directories outside tracked source too.

## Test and preview

Mocked tests need no credentials, Discord writes or live model calls:

```bash
python3 -m unittest discover -s tests -v
python3 -m jr_indonesia_v1 --help
```

Once the environment and Hermes provider are configured:

```bash
python3 -m jr_indonesia_v1 --dry-run
```

Dry-run verifies identity, reads channel history and generates drafts. It never enables Discord writes, enters the sender or changes reply state. Output contains drafts and target IDs, not source chat text or credentials. Model requests can consume provider quota. Review drafts against their channel context; passing tests cannot establish tone quality.

## Run and stop

```bash
python3 -m jr_indonesia_v1
```

Check the intended account and channel first. Do not run the legacy script or another sender on the same account simultaneously. A file lock prevents duplicate instances sharing one runtime directory; it does not coordinate across different directories or machines.

Stop with Ctrl+C or SIGTERM. The sender checks the stop flag immediately before posting and cancels an in-flight Hermes subprocess. Stopping cannot retract a request already accepted by Discord.

Logs contain send IDs, target IDs and counters rather than message bodies. State stores recent reply text for deduplication; keep it private. Invalid state fails closed instead of resetting counters. An optional `rank.json` in the data directory is a dated snapshot; absent rank data stays unknown.

## Optional user service

`deploy/discord-autoreply.service` assumes this checkout is at `%h/discord-selfbot-autoreply` and Python is `/usr/bin/python3`. Review both paths and make Hermes available on the service's PATH before installing it. The unit reads your local `.env` and uses `Restart=on-failure`.

```bash
mkdir -p "$HOME/.config/systemd/user"
cp deploy/discord-autoreply.service "$HOME/.config/systemd/user/discord-autoreply.service"
systemctl --user daemon-reload
systemctl --user enable --now discord-autoreply.service
systemctl --user status discord-autoreply.service
```

For an intentional stop, disable any separate restart watchdog, then:

```bash
systemctl --user disable --now discord-autoreply.service
```

Keep the state and voice profile for the next resume. Verify messages in Discord after deployment; process status alone is not proof of delivery.

## Platform policy

Automated user accounts can violate Discord's Terms of Service. Review the platform and server rules. This repository does not include credentials or server access.
