# Working notes for Claude Code

Read `HANDOFF.md` first; it is the design document and is authoritative. The first build of every stage landed on 2026-11-02; the order of work below is kept as the map of the modules. The name is decided: package, CLI and record directory are `vidfunnel`.

## Conventions

- Versions are date-based: `VERSION = "20261101"`, same-day patch `20261101.1`. Every module declares `VERSION` at the top; `--version` prints the package version and each module's. No semantic versioning.
- Every script and source file ends with a blank line and `# End of File #` (language-appropriate comment syntax). Markdown docs in this repo end the same way.
- Python ≥ 3.9, standard library only. No third-party packages, no `requirements.txt`, no virtualenv needed. ffmpeg, ffprobe, par2 and other tools are external and discovered on PATH.
- Imports are absolute and package-qualified (`from vidfunnel.probe import probe_file`). Never relative imports, never bare module imports, never `sys.path` edits inside the package. The only thing that sets `PYTHONPATH` is `bin/vidfunnel`.
- No comments inside shell command blocks in any documentation or README (zsh users copy them); explanations go in prose before or after the block.
- Markdown files are plain Markdown with no YAML frontmatter.
- Minimal and auditable over clever: no daemon, no authoritative database (the SQLite index is a rebuildable cache), every decision written to the per-file record with its reason, nothing destructive outside `retire`.
- Report errors directly and specifically. Never claim success without the verification pass. Never fail silently.
- Anything that may take more than one second of wall-clock time prints what it is doing and, when long, live progress (HANDOFF §6.10).

## Order of work (first build done 2026-11-02)

1. Package skeleton, `cli.py`, `--version`.
2. `deps.py` and `check` with per-platform install hints.
3. `probe.py` and `record.py`, tested against the fixture set from `tests/make_fixtures.sh`.
4. `analyze.py` rules (HANDOFF §4) with unit tests on captured probe JSON.
5. `plan.py`, `encode.py`, `verify.py`, then `retire.py` last.
6. `Containerfile` and `bin/vidfunnel-podman`.

Test the metadata-tag round trip (HANDOFF §6.7 and §9) on the first remux that works, not at the end.

## Testing

`python3 -m unittest discover -s tests` must pass before any commit that touches the package. Integration tests that run ffmpeg are skipped automatically when ffmpeg is absent, and say so. That sequential command is the reference and is what CI runs.

`sh tests/run_gate.sh` runs the same modules as separate processes: first the ones that are safe side by side, four at a time (`-j N` or `VIDFUNNEL_GATE_JOBS` changes that), then the timing-sensitive ones (signals, locks, grace periods; the list and reasons are in the script) one at a time, alone. It prints every failing module's output in full, exits non-zero on any failure, and may be used as the gate before a commit. On the 4-core iMac (2026-11-09, 1060 tests, 1 skip) the sequential command took about 11 minutes and the runner about 9 min 20 s; the timing-sensitive modules alone take about 5 min 20 s of that.

# End of File #
