# Changelog

All notable changes to the Delta Chat WebPreview Bot will be documented in this file.

## [Unreleased]

## [1.0.2] - 2026-05-22

### Fixed
- Resolved `Method not found` error (`-32601`) during private chat greeting checks by migrating contact `greeted` status tracking to the local SQLite database, completely bypassing missing JSON-RPC `get_contact_config` and `set_contact_config` core methods.


## [1.0.1] - 2026-05-22

### Added
- Added custom bot icon `icon.png` which is automatically set as the bot's avatar on startup.
- Auto-detection and auto-parsing of URLs sent directly in 1-on-1 private chats without needing the `/preview` command.
- Auto-welcoming message for new users starting a 1-on-1 private chat with the bot.
- Automatic upgrading of the bot administrator's fingerprint in the SQLite configuration when it becomes available via PGP key exchange.
- Log printing of the bot's SecureJoin QR code URL at startup for easy administration onboarding.

### Fixed
- Fixed `Method not found` JSON-RPC error (`-32601`) on hosts running older versions of `deltachat-rpc-server` by implementing a robust `_is_private_chat` checker with sequential fallbacks (`get_basic_chat_info` -> `get_full_chat_by_id` -> `get_chat_contacts`).
- Restored user custom modifications for caption formatting (double newline before the link: `\n\n🔗`) and `selfstatus` description to preserve them across updates.
- Fixed private chat detection logic to correctly parse both snake_case (`chat_type`) and camelCase (`chatType`) formats returned by different JSON-RPC server releases.
- Fixed build failures in the Docker environment by adding `make` utility package to the builder stage in the `Dockerfile` (needed for compiling Rust dependency crates for `monolith`).

### Changed
- Decoupled nested volume structures in `docker-compose.yml` to store SQLite data in `./data` and Delta Chat credentials in `./webpreview` to prevent file permission and access conflicts.
- Renamed the `/code` command to `/source` to provide a much more logical and intuitive name for getting the bot source repository URLs.
- Standardized file naming for offline HTML previews using the cleaned domain name and unix timestamp (`webpreview_[domain]_[timestamp].html`).

---

## [1.0.0] - 2026-05-21

### Added
- Initial release of Delta Chat WebPreview Bot.
- Multi-transport SMTP/IMAP relays support (same as Delta Chat Bouncer and YT bots).
- HTML single-file offline compilation using Rust-based `monolith` engine.
- Interactive user commands: `/preview`, `/previewjs`, `/help`, `/source`, `/stats`, `/initadmin`, `/remove_transport`, `/add_transport`.
- Robust SQLite persistent database for tracking logs, stats, and configurations.
- Admin commands restricted to verified admin fingerprints.
- Rate-limiting rules (15-second cooldown per user) and cache pruning routines for safe disk storage management.
