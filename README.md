# DiscordAnkiActivity

An Anki add-on that publishes your currently selected deck and due-card counts to Discord Rich Presence.

## Features

- Shows the deck you are currently working on.
- Shows queue counts as `New | Learn | Review`.
- Uses a built-in Discord IPC client implementation (no `pypresence` dependency).
- Updates automatically on profile open, state changes, reviewer events, and a periodic timer.

## Install

1. Copy `discord_anki_activity` into your Anki `addons21` folder.
2. Restart Anki and start studying.

## Configuration

Edit `discord_anki_activity/config.json`:

- `discord_client_id`: Discord application client ID.
- `update_interval_seconds`: Periodic update interval (minimum 5 seconds).
- `large_image`: Rich Presence large image key.
- `large_text`: Hover text for large image.

## Notes

- Discord desktop must be running for IPC connection.
- The add-on uses the local Discord IPC socket (`discord-ipc-*`) directly.

## Commits

- Use conventional commits
