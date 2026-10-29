# cursaves Agent Guide

## Project

`cursaves` is a Python 3.10+ CLI that exports, synchronizes, and restores Cursor chat data. It reads and writes Cursor's SQLite databases and stores portable gzip-compressed JSON snapshots under `~/.cursaves/`.

The storage format is reverse-engineered and changes across Cursor releases. Preserve support for both legacy workspace metadata and the Cursor 3.0+ global `composer.composerHeaders` index unless a change explicitly removes compatibility.

## Structure

- `cursor_saves/cli.py`: argument parsing, command orchestration, and interactive flows.
- `cursor_saves/export.py`: conversation discovery and snapshot creation.
- `cursor_saves/importer.py`: snapshot inspection, database backup, and restore logic.
- `cursor_saves/db.py`: SQLite access helpers.
- `cursor_saves/paths.py`: Cursor and cursaves filesystem discovery.
- `cursor_saves/backends.py`: Git and S3 synchronization backends.
- `cursor_saves/interactive.py`: InquirerPy selectors.
- `tests/`: unittest suite using temporary SQLite databases.
- `docs/how-cursor-stores-chats.md`: current understanding of Cursor's storage schema.

## Development

Use the repository virtual environment when available:

```bash
.venv/bin/python -m unittest discover -v
.venv/bin/python -m compileall -q cursor_saves tests
.venv/bin/cursaves --help
```

Keep changes compatible with Python 3.10. Use the standard library unless a dependency clearly improves user-facing behavior. Add regression tests for storage-schema changes and for bugs that can affect destructive or stateful commands.

## Safety Invariants

- Treat Cursor databases and user snapshots as irreplaceable data.
- Back up every database before the first write in an operation.
- Apply selection and filtering before backups or writes.
- Never mutate snapshot source files during import.
- Preserve unknown JSON fields and SQLite keys when rewriting conversation data.
- Keep import idempotent: identical snapshots should be skipped and newer data should replace older data predictably.
- Exclude subagent conversations from top-level discovery.
- Include archived top-level conversations unless the command explicitly requests otherwise.
- Resolve workspaces through `paths.py`; do not hard-code macOS-only paths.
- Avoid live write tests against the user's Cursor profile. Use temporary databases or read-only validation.

## Snapshot Metadata

Snapshot sidecars are the fast path for listing and filtering. New metadata fields must also have a fallback for old snapshots, which may only contain the compressed snapshot. Conversation recency is `lastUpdatedAt`, then `createdAt`; legacy pull filtering may fall back to `exportedAt`.

## Releases

Version numbers are duplicated in `pyproject.toml` and `cursor_saves/__init__.py`; update both. Before publishing a release:

1. Run the full unit test suite and bytecode compilation.
2. Verify relevant command help and `git diff --check`.
3. Commit the release changes on `main` using the existing `Feature summary (vX.Y.Z)` style.
4. Create and push an annotated `vX.Y.Z` tag.
5. Publish a GitHub release with concise behavior and compatibility notes.

Do not commit Cursor databases, snapshots, credentials, local sync state, build output, or virtual environments.
