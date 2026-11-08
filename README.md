# Video-Format-Funnel

A command-line tool (`vidfunnel`) that funnels legacy video files — AVI with any codec, FLV, SWF with embedded video, FLIC, MPEG-1/2 and DVD VOB, WMV/ASF, old QuickTime MOV, RealMedia, MKV, and anything else ffmpeg can decode — into one archival output profile: H.264 High 4.1 and AAC in fragmented MP4, with closed keyframe groups every two seconds and optional PAR2 recovery data. Files that are already H.264 are remuxed, not re-encoded. Originals are never deleted by the conversion itself; a separate `retire` step moves them away only after verification, and only when asked.

The design document is `HANDOFF.md`; it is the full specification and the place to look when this README is too brief. `CLAUDE.md` holds the working conventions for the repo.

## What it does and does not do

- **Remuxes** a file whose video is already H.264 within the output limits (profile up to High, level up to 4.1, 8-bit 4:2:0, progressive). Only the container changes; the video bitstream is copied.
- **Re-encodes** everything else with libx264 at a transparent quality target (CRF 18 to 20 by height, `-preset slower`), deinterlacing or removing telecine where needed. Audio that is already AAC-LC or MP3 is copied; anything else becomes AAC-LC.
- **Writes fragmented MP4** with closed keyframe groups of at most two seconds on re-encodes, so damage to a file stays local and everything before it keeps playing. `--plain-mp4` writes a conventional `+faststart` MP4 instead.
- **Never touches originals** outside `retire`. `plan` writes no media at all, `convert` writes new files next to the sources, and `retire` refuses anything that has not been verified.
- **Records every decision with its reason**: one JSON record per source, holding the probe, the analysis, the sample positions, the exact ffmpeg command, the hashes, the verify results and the score.
- **Redacts reports by default**, so a report can be posted when asking for help without giving away file names or folder structure.

It does not scale video (except an integer upscale of tiny palette animations), does not crop letterbox bars, does not write interlaced output, and does not produce HEVC, AV1 or MKV.

## Requirements

- Python 3.9 or newer, standard library only. No packages to install, no virtualenv.
- ffmpeg and ffprobe 6 or newer, built with `libx264`, the native `aac` encoder, the `h264_metadata` bitstream filter and the `idet`, `bwdif`, `fieldmatch`, `cropdetect` and `setparams` filters. Any normal build with libx264 has all of these.
- Optional: ffmpeg 7 or newer built with libdvdread, for the `dvdvideo` demuxer (IFO-driven DVD input). Without it a `VIDEO_TS` tree is read by concatenating its VOB files, with a small confidence penalty.
- Optional: `par2` (par2cmdline) for `--par2`, `mediainfo`, and `ffsubsync` or `alass` for subtitle alignment.

Whether an ffmpeg has `dvdvideo` depends on the distribution's build flags, not on the version. As tested on 2026-11-02: Debian trixie (7.1.5), Arch (9.0.2) and Alpine (8.1.3) have it; Fedora's RPM Fusion ffmpeg 7.1.5 and openSUSE's Packman ffmpeg 7.1.5 do not. Homebrew's build has not been checked. Fedora's base `ffmpeg-free` has no libx264 at all, so Fedora needs RPM Fusion either way. Nobody packages `ffsubsync` or `alass`; `check` prints how to install them.

## Install

Clone the repo and run the launcher from the clone. It checks for a suitable `python3` and runs the package from the repo, so nothing needs to be installed to try it:

```
git clone https://github.com/RedBearAK/Video-Format-Funnel.git
cd Video-Format-Funnel
bin/vidfunnel --version
```

To have a `vidfunnel` command on PATH, either of these symlinks `bin/vidfunnel` into `~/.local/bin`:

```
./install.sh
bin/vidfunnel --install-command
```

If `~/.local/bin` is not on PATH in your login shell, it prints the rc file and the one line it would add (`~/.bashrc` for bash, `~/.zprofile` for zsh, `fish_add_path` for fish), asks before appending it, and marks the line with `# vidfunnel`. `--system` installs into `/usr/local/bin` instead (with `sudo` on Linux when that is not writable), and `--yes` skips the prompt. It never overwrites a file that is not a symlink to this clone's launcher. If you move the clone, the old symlink dangles; run `--install-command` again from the new location. To remove the symlink and the marked PATH line, and nothing else:

```
vidfunnel --uninstall-command
```

Then check what this machine has:

```
vidfunnel check
```

On my openSUSE Tumbleweed machine with Packman's ffmpeg it prints:

```
querying the capabilities of '/usr/bin/ffmpeg' (7 calls)
platform: openSUSE Tumbleweed ('opensuse-tumbleweed')
name           need      status   detail
-------------  --------  -------  ------
ffmpeg         required  present  7.1.5 '/usr/bin/ffmpeg'
ffprobe        required  present  7.1.5 '/usr/bin/ffprobe'
libx264        required  present  encoder
aac            required  present  encoder
idet           required  present  filter
bwdif          required  present  filter
fieldmatch     required  present  filter
cropdetect     required  present  filter
setparams      required  present  filter
h264_metadata  required  present  bitstream filter
dvdvideo       optional  MISSING  demuxer missing: DVD trees take the VOB concatenation path
libdvdread     optional  MISSING  ffmpeg not configured with --enable-libdvdread
par2           optional  present  '/usr/bin/par2'
mediainfo      optional  present  '/usr/bin/mediainfo'
ffsubsync      optional  MISSING  not found
alass          optional  MISSING  not found
all required items present
```

followed by install hints for the missing optional items. When something required is missing, `check` prints the install command for the detected platform (zypper, dnf, apt, pacman, apk or Homebrew), and `plan`, `convert` and `verify` refuse to start with the same message. `report`, `find`, `index` and `retire` work from the records and hashes alone and run without ffmpeg.

## A distrobox for the dvdvideo demuxer

On a workstation whose distro ffmpeg lacks `dvdvideo` (Fedora, openSUSE), an Alpine distrobox provides one without replacing the host's ffmpeg:

```
bin/vidfunnel-setup-distrobox
```

It creates a distrobox named `vidfunnel-alp` from `alpine:3.24` with ffmpeg, par2cmdline, python3 and mediainfo, installs `ffsubsync` into a venv in your home (`~/.cache/ffsubsync-venv`), and exports `ffmpeg`, `ffprobe` and `ffsubsync` as wrappers into `~/.local/share/vidfunnel-tools/bin`, a directory that is deliberately not on PATH. It ends by printing the two ways to point `vidfunnel` at the wrapper (ffprobe and ffsubsync are looked up next to it). Per call:

```
vidfunnel --ffmpeg ~/.local/share/vidfunnel-tools/bin/ffmpeg check
```

Or once per shell, so the installed command is used exactly as it would be with the host ffmpeg. Add the line to `~/.bashrc` or `~/.zshrc` to keep it:

```
export VIDFUNNEL_FFMPEG=~/.local/share/vidfunnel-tools/bin/ffmpeg
vidfunnel check
```

`--ffmpeg` on the command line wins over the variable.

`--name`, `--image`, `--tools-dir` and `--venv-dir` change the defaults, and `--no-subsync` skips ffsubsync. The script can be run again safely and refuses to overwrite a wrapper that is not its own. To take away exactly what it created (the box, the wrappers and the venv; the pulled image stays in the engine's cache):

```
bin/vidfunnel-setup-distrobox --remove
```

## A container for hosts without Python

For immutable hosts and servers without Python, the repo has a `Containerfile` (Alpine with python3, ffmpeg, par2cmdline, mediainfo and ffsubsync in a venv). The build checks that ffmpeg has `libx264`, `aac`, `dvdvideo` and `h264_metadata` and that par2 and ffsubsync are present, and fails loudly if any of them is missing. `bin/vidfunnel-podman` runs the tool in that image with podman, or docker when podman is not installed. Build the image once, then use the wrapper wherever you would use `vidfunnel`:

```
bin/vidfunnel-podman --build
bin/vidfunnel-podman plan /srv/media/Home\ Videos
```

Every argument that is an existing file or folder on the host is bind-mounted read-write at the same path inside the container, and the current directory is mounted too and used as the working directory. The container runs with `--userns=keep-id` under podman (`--user uid:gid` under docker) so that records and outputs are owned by you, and with `--security-opt label=disable` for SELinux hosts. `VIDFUNNEL_NICE` runs the command under `nice -n VALUE` and `VIDFUNNEL_IONICE_CLASS` under `ionice -c VALUE`, which is worth setting on a server that does other work. `--image` or `VIDFUNNEL_IMAGE` choose another image; setting `VIDFUNNEL_IMAGE_DIGEST` near the top of the script pins the image by digest so it changes only when that line is edited.

## Usage

The pipeline is five separate subcommands, run in order. Each one is idempotent and resumable: run it again on the same folder and it picks up where it left off. Folders are walked recursively. `plan`, `convert`, `verify` and `index` work on the current directory when no PATH is given and print the resolved folder first; `retire`, the only stage that moves originals, always needs an explicit PATH. Global options (`--ffmpeg`, `--quiet`, `--verbose`, `--yes`) go before the subcommand name; `--ffmpeg` defaults to the `VIDFUNNEL_FFMPEG` environment variable when set.

The examples below are real output from a scratch folder holding copies of six test fixtures under made-up names: two H.264 MKVs, an H.264 MP4, an XviD AVI, and a `movie.avi`/`movie.mkv` pair that share a name.

### plan

```
vidfunnel plan "Home Videos"
```

`plan` probes and analyses each source and writes its record into the folder's `.vidfunnel/` directory: the probe, the decisions with their reasons and sample positions, the predicted score, and the exact ffmpeg command `convert` will run. It never writes media. A source whose record still matches it is skipped on the next run; `--replan` plans it again. The tail of the output, with the summary table:

```
[plan 6/6] probing 'movie.mkv'
decode check of 'movie.mkv'
[decode] 00:00:05 / 00:00:05  100.0 %  0 fps  136.0x  eta 00:00:00
idet over 1 window(s) of 'movie.mkv'
[idet window] 1/1  100.0 %
reading packet timing of 2 stream(s) of 'movie.mkv'
measuring keyframe spacing of 'movie.mkv'
[keyframes] elapsed 00:00:00  1 keyframes in 125 packets
  action                             score       size  source  flags
  remux                                100    100 KiB  Beach day.mkv  -
  reencode                             100    217 KiB  Birthday party 2004-06-12.avi  -
  remux                                100    102 KiB  Phone clip 3.mp4  -
  remux                                 85    110 KiB  Wedding DVD rip.mkv  long-gop
  quarantine (output-collision)          -    217 KiB  movie.avi  output-collision
  quarantine (output-collision)          -    100 KiB  movie.mkv  output-collision
plan: 3 remux, 1 re-encode, 2 quarantine, 0 skipped, 0 failed in 00:00:08; records in '.vidfunnel/' under each folder
```

`Wedding DVD rip.mkv` is a 720×480 file flagged as square pixels with letterbox bars; `plan` inferred 4:3 from the bars (15 points) and noted that its keyframes are more than 5 s apart, which a remux cannot change. The two `movie.*` files would both become `movie.mp4`, so neither is converted.

The main options:

- `--samples N` sets the number of sample windows for `idet` and `cropdetect` (default 12 windows of 3 s, skipping the first and last 3 %); `--full-analysis` analyses the whole file. Files shorter than 60 s are always analysed in full.
- `--assume-dar 4:3|16:9` resolves an SD frame whose aspect cannot be told from the pixels.
- `--audio-offset S` replaces the measured audio start offset.
- `--audio-strict-aac` re-encodes MP3 to AAC instead of copying it.
- `--reencode-video` re-encodes H.264 that could be remuxed, for when two-second keyframe groups matter more than avoiding a second generation.
- `--max-level 4.1` deinterlaces HD at frame rate to stay at level 4.1 (the default allows 4.2 for field-rate HD).
- `--plain-mp4` plans a conventional `+faststart` MP4 instead of a fragmented one.
- `--par2 N` records that `convert` should write PAR2 with N % redundancy.

### convert

```
vidfunnel convert "Home Videos"
```

`convert` runs the planned command for every record with status `planned`. ffmpeg writes to a hidden `.NAME.mp4.part` file in the same folder, which is renamed into place on success; the output gets the source's modification time, subtitle sidecars are written the same way, PAR2 is written when asked, and `verify` runs on the result straight away. A failed run leaves the record at `failed` with ffmpeg's error output, and removes the part file. A record that is already converted and whose output still matches is skipped.

```
[convert 2/4] re-encode 'Birthday party 2004-06-12.avi' -> 'Birthday party 2004-06-12.mp4'
[re-encode] 00:00:04 / 00:00:05  98.4 %  0 fps  10.2x  eta 00:00:00
  converted 'Birthday party 2004-06-12.mp4' (149212 bytes, 67 % of the source) in 0.7 s
verifying 'Birthday party 2004-06-12.mp4' (146 KiB)
[verify decode] 00:00:05 / 00:00:05  99.2 %  0 fps  147.0x  eta 00:00:00
  verified 'Birthday party 2004-06-12.mp4': score 100 (ok); frames 125 of 125 expected, duration delta 0.04 s
[convert 3/4] remux 'Phone clip 3.mp4' -> 'Phone clip 3.vidfunnel.mp4'
[remux] 00:00:04 / 00:00:05  98.4 %  0 fps  573.0x  eta 00:00:00
  converted 'Phone clip 3.vidfunnel.mp4' (103872 bytes, 100 % of the source) in 0.2 s
verifying 'Phone clip 3.vidfunnel.mp4' (101 KiB)
[verify decode] 00:00:05 / 00:00:05  100.0 %  0 fps  156.0x  eta 00:00:00
  verified 'Phone clip 3.vidfunnel.mp4': score 100 (ok); frames 125 of 125 expected, duration delta 0.0 s
[convert 4/4] remux 'Wedding DVD rip.mkv' -> 'Wedding DVD rip.mp4'
[remux] 00:00:04 / 00:00:05  98.8 %  0 fps  1300.0x  eta 00:00:00
  remuxed output is larger than the source (114714 > 112684 bytes); container overhead, kept
  converted 'Wedding DVD rip.mp4' (114714 bytes, 102 % of the source) in 0.2 s
verifying 'Wedding DVD rip.mp4' (112 KiB)
[verify decode] 00:00:05 / 00:00:05  99.8 %  0 fps  60.0x  eta 00:00:00
  verified 'Wedding DVD rip.mp4': score 85 (review); frames 150 of 150 expected, duration delta 0.0 s
convert: 0 converted, 4 verified, 0 skipped, 0 failed in 00:00:04; records in '.vidfunnel/' under each folder
```

The re-encode came out at two thirds of the source's size. The MP4 source was written under the interim name because the output name is its own, and the letterboxed file keeps its 15-point aspect penalty from `plan`, which puts it in the `review` band.

Options:

- `--dry-run` prints the commands and writes nothing.
- `--jobs N` converts N files at once. The default is 1, because x264 already uses every core for one file; `--jobs` is for remux-heavy sets.
- `--par2 N` writes PAR2 with N % redundancy, overriding the planned value (10 is a reasonable choice).
- `--refit` revisits re-encodes that came out larger than their sources, described below.
A re-encode that comes out larger than its source is kept like any other output and flagged `output-larger`, with both sizes in the record; convert prints `output is larger than the source (N > M bytes); kept and flagged 'output-larger'`. `retire` refuses such a record unless `--allow-larger` is given, so you can compare the two files first. Remuxes are exempt from this size rule: only the container changed, and fragment headers can make a small file grow slightly. A larger remux is kept and flagged `remux-larger` for information.

`convert --refit` gives those larger re-encodes one more try. It selects the records under the given paths that are `converted` or `verified`, re-encoded, flagged `output-larger` and not yet flagged `fit-capped`; the rest are skipped and counted by reason (`not converted`, `not a re-encode`, `not larger`, `already capped`, `already refitted`). For each selected record it raises the CRF of the planned command by `ceil(ln(ratio) / ln(1.12))` steps, where `ratio` is the output size over the source size: one CRF step of `-preset slower` saved 11 to 12.5 % of size on the material this rule was measured on, and 1.12 per step is a round figure a little below that, so an output 1.42 times its source goes from CRF 18 to 22, and an output 1.08 times its source goes one step. The CRF is capped at 23 for sources up to 576 lines and at 24 above. The caps are a chosen quality floor, not a measured limit: past them the loss is judged not worth the space, so a refit that would need more is capped and the existing output is kept. When `plan` measured the source as heavily compressed (fewer than 0.15 bits per pixel per frame) and blocky (blockdetect's score at or above 3.5), a light `hqdn3d=2:1:2:3` denoise goes into the filter chain as well (first, or right after the deinterlacing or inverse-telecine filter), so the encoder spends fewer bits on the blocks. The rest of the command stays as planned, and the result is written to the hidden part file. If it is smaller than the existing output and passes the identity, stream and duration checks, it replaces the output, the record gets the flag `refitted` and goes back to `converted`, and `verify` runs on it as after a conversion; `output-larger` is removed only when the new output is no larger than the source. Otherwise the part file is deleted, the existing output is kept, and the record gets the flag `fit-capped` with the attempt and the reason. The record holds the CRF, the filters, the size and the ratio of the attempt. A record gets one refit; only an attempt whose ffmpeg run failed is tried again on a later `--refit` run. `--dry-run` with `--refit` prints the refit commands and writes nothing.

```
vidfunnel convert --refit "Home Videos"
```

```
[refit 1/1] 'Web clip 2006.avi': CRF 18 -> 19, hqdn3d, ratio 1.08
  size class 'heavy' and blockiness 173.8731 at or above 3.5: hqdn3d=2:1:2:3
[refit] 00:00:05 / 00:00:05  97.7 %  0 fps  13.8x  eta 00:00:00
  refit 'Web clip 2006.avi': CRF 18 -> 19, hqdn3d, ratio 1.08 -> 1.05; replaced 'Web clip 2006.mp4' (123535 bytes, was 126343); still larger than the source, 'output-larger' kept
verifying 'Web clip 2006.mp4' (121 KiB)
[verify decode] 00:00:05 / 00:00:05  97.8 %  0 fps  162.0x  eta 00:00:00
  verified 'Web clip 2006.mp4': score 100 (ok); frames 124 of 125 expected, duration delta 0.12 s
skipped: 1 not larger
convert --refit: 1 refitted, 0 kept, 1 skipped, 0 failed in 00:00:01; records in '.vidfunnel/' under each folder
```

That run is the H.264 AVI test clip re-encoded with `--reencode-video`: one CRF step and the denoise took it from 108 % to 105 % of the source, so it was replaced but keeps `output-larger`, and `retire` still asks for `--allow-larger`. The synthetic test pattern compresses unlike real footage, which is also why its blockiness score is so high.

### verify

```
vidfunnel verify "Home Videos"
```

`convert` already verifies each output; run `verify` again after moving files, or to add the SSIM comparison. It decodes every video and audio stream of the output in full, compares the video stream's duration with the source's (more than 0.1 s plus one frame apart costs 20 points), exact frame count (outside 2 % plus 2 frames costs 30) and stream counts with the source (a video, audio or text-subtitle stream missing from the output costs 50), reads back the identity tag (missing or naming another record or source costs 100), and records the output's SHA-256, size and size ratio. `--ssim-check` also compares sampled windows with the source by SSIM on luma (below `--ssim-threshold`, default 0.97, costs 25; `--samples N` sets the windows). It is slow and off by default.

The score is 100 minus the penalties from the analysis and from verify. Bands: 90 and above is `ok`, 60 to 89 is `review`, below 60 is `hold`. Verifying again replaces the earlier verify penalties rather than adding to them, and the previous result is kept in the record's history.

### report

```
vidfunnel report "Home Videos"
```

`report` reads the records and prints a table, worst scores at the top and unscored rows (planned, failed, quarantined) below them. It never probes or encodes. Names and paths are redacted by default; the top-level path becomes `<root>`. The report from the example folder, apart from one machine-readable header comment that `--from` uses:

```
# vidfunnel report

> Names in this report are redacted. To see them, re-run the same command with --reveal, or vidfunnel report --from <this file> --reveal. To locate one row: vidfunnel find <ID>.

- report ID: 943a787d-82ee-4bd9-b384-02efffd35cc5
- generated at: 2026-11-02T11:59:15Z
- tool version: 20261102
- invocation: vidfunnel report --format md --no-index -- '<root>'
- redacted: yes
- records: 6

| ID | score | band | status | action | flags | source -> output | path |
|---|---|---|---|---|---|---|---|
| 38bcd7ff-e8b3-4a6e-a4e8-23c88a520e53 | 85 | review | verified | remux | long-gop, remux-larger | 110 KiB -> 112 KiB (W______ D__ r__.mp4) | <root>/C____/W______ D__ r__.mkv |
| 2602a78c-1ae5-4c10-baa0-ca5ef5fcb56c | 100 | ok | verified | remux | remux-larger | 100 KiB -> 101 KiB (B____ d__.mp4) | <root>/####/B____ d__.mkv |
| 064739ae-255f-4fb6-b307-f0d9291fdf0f | 100 | ok | verified | reencode | -- | 217 KiB -> 146 KiB (B_______ p____ ####-##-##.mp4) | <root>/####/B_______ p____ ####-##-##.avi |
| 64a7dea8-9b5b-4def-bb87-1eedd10d9f6f | 100 | ok | verified | remux | -- | 102 KiB -> 101 KiB (P____ c___ #.mp4) | <root>/C____/P____ c___ #.mp4 |
| 62a38f85-287e-4520-9675-e4d64e89ca2a | -- | -- | quarantined | quarantine | output-collision | 217 KiB -> -- B (m____.mp4) | <root>/C____/m____.avi |
| 66df35d9-befc-4d16-b8d8-5bd2b66e4795 | -- | -- | quarantined | quarantine | output-collision | 100 KiB -> -- B (m____.mp4) | <root>/C____/m____.mkv |
```

Options:

- `--reveal` shows the real names and paths. `--redact-dates` truncates creation times and mtimes to the year.
- `--format md|json|csv`: Markdown (the default) pastes into a note or an issue; JSON and CSV are for processing. CSV writes its header to stderr and cannot be used with `--from`.
- `--min-score N`, `--max-score N` and `--status S` (repeatable) filter the rows.
- `--with-logs` adds the stored ffmpeg error output and run log of each row, redacted like the rest. This is the bundle to post when asking why a file misbehaved.
- `--from REPORT` re-runs the invocation recorded in an earlier Markdown or JSON report, with whatever options you add, so a redacted report that was sent out can be reissued with `--reveal`. A row whose status has moved on since that report is marked as changed. Because a redacted report records its paths as `<root>`, give the PATH arguments again, or have the central index available.
- `--no-index` always reads the records; `--db PATH` names the index to use.

To map a row of a redacted report back to a real file on your own machine:

```
vidfunnel find 064739ae
```

`find` takes a record ID or any unique prefix and prints the ID, status, score, action, and the real source, output and record paths. It uses the index when there is one and otherwise searches the records under `--root` (default: the current directory).

### retire

```
vidfunnel retire "Home Videos"
```

`retire` is the only subcommand that touches an original. For each source it checks, in order, and stops at the first failure without moving anything:

1. a usable record exists and its status is `verified`;
2. the source still matches the record's fingerprint, so it has not been replaced since it was planned;
3. the output exists;
4. the score is not in the `hold` band, and not in the `review` band unless `--allow-review` is given;
5. a re-encode's output is not larger than the source and the record is not flagged `output-larger`, unless `--allow-larger` is given (the record is then flagged `allowed-larger`, so the report shows the choice);
6. the output's full SHA-256 matches the record (the one expensive check, so it runs last);
7. an output under the interim `<stem>.vidfunnel.mp4` name can be renamed to its final name;
8. the quarantine destination does not exist yet.

Only then is the original moved into the quarantine directory, by default `.vidfunnel-retired/` under each PATH given, keeping its relative path; `--quarantine DIR` chooses another. An interim-named output is then renamed to its final name. The record stays as the audit trail with status `retired`. Outputs are never removed. In the example folder, retire moved the three originals that scored 100 into `.vidfunnel-retired/` (renaming `Phone clip 3.vidfunnel.mp4` to `Phone clip 3.mp4` on the way), skipped the two quarantined sources, and refused the letterboxed one:

```
[retire 4/4] 'Clips/Wedding DVD rip.mkv' -> quarantine
[retire 4/4] 'Clips/Wedding DVD rip.mkv' refused (score-review): score 85 is in the review band (60-89); pass --allow-review after checking it
```

```
retire: 3 retired, 2 skipped, 1 refused score-review in 00:00:00; records in .vidfunnel/ under each folder (kept for retired sources)
```

`--destroy-originals-now` unlinks the originals instead of moving them, and is the only destructive option in the tool. It prints what it is about to do and asks you to type the word `destroy`; the global `--yes` (given before `retire`) skips that for scripted use. Check the quarantine directory a while before deciding you need this.

### index (optional)

```
vidfunnel index "Home Videos"
```

`index` loads every record under the given trees into a SQLite file (`~/.local/state/vidfunnel/index.sqlite` by default, `--db PATH` for another), which `report` and `find` use for queries across folders. It holds nothing that is not also in a record: delete it at any time, or `--rebuild` it. It is per machine and per user, which is why it is never the authority for a share used from several machines.

## How decisions are made

Every file starts at 100 points; the analysis and verify subtract penalties, and the score's band decides what `retire` will accept. The analysis is sampled (12 windows of 3 s by default) except for the decode check, which reads the whole file. The rules that cost points or change the encode:

| Finding | What happens | Penalty |
|---|---|---|
| Aspect inferred from letterbox bars (SD frame signalled as square pixels) | SAR set in the H.264 VUI and the container; never cropped | 15 |
| Aspect ambiguous (no bars, 4:3 or 16:9 undecidable) | left as signalled, flag `aspect-ambiguous`; `--assume-dar` resolves it | 30 |
| Telecined (NTSC 3:2 pulldown) | `fieldmatch,decimate` to 23.976p | 10 |
| Interlaced | `bwdif` at field rate (59.94 or 50 fps) | 5 |
| Mixed or inconsistent cadence | treated as interlaced, flag `cadence-mixed` | 40 |
| Interlaced HD | encoded at level 4.2, flag `level-4.2`; `--max-level 4.1` deinterlaces at frame rate instead | as interlaced |
| Audio starts more than 0.1 s from video | offset kept, flag `sync-offset`; `--audio-offset` overrides | 0 |
| Audio drift 0.1 % to 2 % | audio stretched with `atempo`, flag `sync-drift`; AAC or MP3 is then re-encoded, flag `audio-reencoded-for-sync` | 20 |
| Audio drift beyond 2 % | not corrected, flag `sync-drift-large` | 100 |
| Decoder warnings | recorded per message class with counts; known harmless classes are not counted | 10 per class |
| Loose VOB fragments without an IFO, or a DVD tree without the `dvdvideo` demuxer | fragments concatenated, flag `vob-no-ifo` or `dvd-no-demuxer` | 10 |
| Palette or RLE animation at most 300 px high | integer nearest-neighbour upscale (2×, 3× below 160 px), `-tune animation`, flag `pixelart-upscale` | 0 |
| H.264 keyframes more than 5 s apart (remux) | kept as they are, flag `long-gop` | 0 |
| After conversion: a video, audio or text-subtitle stream of the source is missing from the output | flag `stream-count-mismatch` | 50 |
| After conversion: the `vidfunnel` identity tag is missing or names another record or source | flag `identity-mismatch` | 100 |

Sources that cannot be converted are quarantined in place with a reason and a record: `unsupported` (ffmpeg cannot decode a stream), `decode-failed`, `no-video`, `swf-vector` (an SWF with no video stream), and `output-collision` (see below).

The output profile in short: libx264 High profile, level 4.1 (4.2 only for field-rate HD), `yuv420p`, CRF 18 up to 576 lines, 19 at 720p, 20 at 1080p, `-preset slower`, closed GOP of at most 2 s, source resolution and frame rate (VFR stays VFR), aspect as SAR, colour tags set from the frame size and rate when the source has none. Audio: AAC-LC and MP3 copied unchanged, everything else AAC-LC at 96 kb/s per channel with the channel layout and sample rate kept. Text subtitles become `mov_text` and an `.srt` sidecar is always written as well, unless an `.srt` of that name already exists beside the source and is not the tool's own, which is left alone with the flag `sidecar-exists` (an external `.srt` is what Roku Media Player and most TV players read; embedded `mov_text` is not shown everywhere), bitmap subtitles go to sidecars (`.sup` for PGS, `NAME.subs.mkv` for DVD subtitles), chapters are kept. Container: `-movflags +frag_keyframe+empty_moov+default_base_moof+use_metadata_tags`. The full specification, with the reasons for each choice, is in `HANDOFF.md` sections 2 to 5.

### What CRF means

An encoder has to decide, in every frame, how much detail to throw away. Telling it a bitrate ("this many bits per second, whatever happens") is like packing a suitcase of fixed size for every trip: a quiet scene of someone talking gets far more room than it needs, and a busy scene with confetti and camera motion is squeezed until it breaks into blocks. CRF, the Constant Rate Factor, is the other way of asking: "keep every frame looking about this good, and use however many bits that takes". Quiet scenes become tiny, busy scenes get what they need, and the quality stays even from start to finish. The file size is whatever comes out.

The number is a quality dial that runs backwards: lower is better. In x264, 0 is lossless and enormous, 51 is the worst it can do, and each step of one changes the file size by roughly 12 to 15 %. The useful range for real video is about 17 to 28. 18 is transparent for most people on most material and is what this tool uses for standard definition, where every flaw is magnified on a large screen; 19 and 20 give the same visual result at 720p and 1080p, where fine detail hides small errors; 23 is x264's own default, fine for things watched once; 28 and above is visibly soft.

CRF judges quality against the frame it is given, not against the original film. Handed a blocky DivX rip, it faithfully preserves every block and ring as if they were detail worth keeping, which is why such a re-encode can come out larger than its source (see the table below). Turning the dial to 22 or 23 on that kind of source discards some of the junk along with a little real detail, and a light denoise before encoding removes the junk first so the dial can stay lower.

### What to expect in output sizes

The profile aims at transparency and playability, not at shrinking, so the result depends almost entirely on the source. Expectations by source class, from how x264 behaves at these settings on typical material; the honest numbers for a library come from converting one representative folder and reading the sizes in its records:

| Source | Path | Expected output size |
|---|---|---|
| H.264 in MKV, AVI, FLV, MOV, MP4 | remux | about the same; AVI shrinks a little (index gone), very small files grow a little (fragment headers, flag `remux-larger`) |
| H.264 in MPEG-TS (broadcast captures) | remux | 5 to 10 % smaller |
| DVD MPEG-2 at 4 to 8 Mb/s, progressive or telecined | re-encode | 40 to 60 % smaller; a 4.5 GB disc image lands around 1.5 to 2.5 GB |
| MPEG-2, true interlaced, field-rate deinterlaced | re-encode at double frame rate | 25 to 45 % smaller; the doubled rate eats part of the saving |
| DV AVI (25 Mb/s video, PCM audio) | re-encode | 85 to 92 % smaller; 13 GB per hour becomes 1 to 2 GB |
| Uncompressed, RLE, QuickTime Animation, palette AVI | re-encode | 90 % or more smaller |
| DivX/Xvid AVI, WMV, FLV1, Sorenson at the usual 0.7 to 1.5 Mb/s | re-encode | often larger, 1.2 to 2.5 times: CRF 18 preserves the blocking and ringing of the source as if it were detail |
| Low-bitrate web H.264 that must be re-encoded (Baseline, wrong level, damaged) | re-encode | similar to somewhat larger |
| 1080i broadcast H.264 or MPEG-2 at 15 to 18 Mb/s | re-encode | 50 to 70 % smaller |

Audio moves the same way: AAC and MP3 are byte-identical copies; AC-3 stereo becomes AAC 192k, a modest saving; PCM at 1.5 Mb/s becomes 192k, a large one on DV and camera clips; AC-3 5.1 at 448k becomes AAC at the 448k cap, about the same. The closed 2 s GOP costs roughly 5 to 15 % against x264's default GOP; it is the price of bounding decode damage to two seconds.

The DivX and WMV row is the reason the size rule exists (see `convert`): on a library of 2000s rips a good share of re-encodes will come out larger than their sources. That is the correct result for the stated goal, since a lower quality setting would visibly degrade already poor material, but it means the tool does not shrink that class by default. `convert --refit` (see `convert`) re-encodes such outputs once more at a CRF chosen from how much larger they came out, up to 23 for SD and 24 for HD, and keeps the result only when it is smaller. `--par2 N` adds roughly N % of the output size plus a small index file; the `.subs.mkv` sidecar for DVD bitmap subtitles is a few megabytes at most.

## Records and identity

Each folder that `plan` processes gets a hidden `.vidfunnel/` directory with one JSON record per source, named after the full source file name with its extension (`movie.avi.json`, `movie.mkv.json`), so two sources with the same stem never share a record. Quarantined sources get records too. Each record holds a random record ID, the source fingerprint (size, mtime, SHA-256 of the first and last MiB) and full SHA-256, every probe result and decision with its reason, the ffmpeg command, the output name, hashes, verify results, score and status, with a timestamped section per stage. Every write is atomic. `folder.json` holds the last scan time, the tool version and the inventory of source names seen. While a run is active, `.vidfunnel/lock` records the host, pid and start time, so two machines working on the same share cannot run in one folder at once; a lock older than 24 hours is taken over with a warning. If a source changes after it was planned, its record is marked `source-changed` and the file is planned again under a new record ID, with the old one kept in its history.

Keeping the records in one hidden directory per folder, rather than a sidecar per video, keeps folders readable in file managers and media servers, and the state stays with the data when the tool is run from different machines over the same share.

Every MP4 the tool writes carries a `vidfunnel` metadata tag naming its record ID, the tool version, the first 16 hex digits of the source's SHA-256 and the output profile. The tag travels with the file through copies and renames, and `verify` checks it. An MP4 that already sits at the output name without this tag, or with a tag for another source, is not ours: `plan` quarantines the source as `output-collision` and writes nothing. The same happens when two sources in one folder share a stem (`movie.avi` and `movie.mkv`), since picking one of two versions of the same content is a human's job. When the source is itself an MP4, the output is written as `<stem>.vidfunnel.mp4` next to it, and `retire` renames it to `<stem>.mp4` once the original has been moved away.

## Redaction

In a redacted report, every word that starts with a letter keeps that letter, every further letter becomes `_` and every further digit becomes `#`; a word that starts with a digit is masked completely, and separators and the file extension are kept. So `Client Name - Deposition 2019-03-14 part2.avi` becomes `C_____ N___ - D_________ ####-##-## p___#.avi`.

The point is that a report can be shared for troubleshooting without revealing names, subjects or folder structure, while the shape of each name is still enough to match rows to files by eye, and `vidfunnel find ID` resolves any row exactly. The rule is a pure function of the string, with no salt or state. Codec names, stream parameters, durations, flags, scores and ffmpeg output are kept, because they are what troubleshooting needs; real paths and metadata values inside the ffmpeg command and error text are replaced by their redacted forms. Records on disk are never redacted; redaction applies to report output only.

## Testing

The test fixtures are synthesised with ffmpeg itself and are not committed:

```
tests/make_fixtures.sh
python3 -m unittest discover -s tests
```

`make_fixtures.sh` writes 39 short clips to `tests/fixtures/` (about 29 MB): XviD and H.264 AVI, FLV and SWF, WMV, MPEG-2 program streams with 4:3, 16:9, interlaced, telecined and PAL variants, DV, palette and RLE animation, QuickTime Animation, letterboxed 720×480 with square and 8:9 pixels, an audio offset, drift within and beyond tolerance, VFR, several audio codecs, subtitles and chapters, a damaged FLV, a 1080i transport stream, an MP4 source, a `movie.avi`/`movie.mkv` collision pair and loose VOB fragments without an IFO. `--force` remakes existing ones. It prints a SKIP line for the four cases ffmpeg cannot produce: a vector SWF, a FLIC file and a `VIDEO_TS` tree with an IFO.

The rule tests run against probe and analysis JSON captured from the fixtures and committed under `tests/probe_json/` and `tests/analysis_json/`, so they need no ffmpeg. The integration tests that run ffmpeg or par2 skip themselves when the tool is not on PATH and say so.

## Known limits

- Roku direct play of fragmented MP4 through Jellyfin has not been tested. Convert one file and try it before converting a library; `--plain-mp4` is the fallback.
- IFO-driven DVD input through the `dvdvideo` demuxer is only partly exercised: IFO files cannot be synthesised, so the tests cover the argument building and the loose-VOB path, not a real disc. My openSUSE machine's ffmpeg lacks `dvdvideo`, so that path has not been run end to end here.
- SWF files with no video stream (vector or ActionScript animations) are quarantined as `swf-vector` and kept; there is no headless renderer to convert them with.
- There is no FLIC fixture, since ffmpeg cannot write FLIC.
- The redacted log file option described in `HANDOFF.md` section 6.10 (`--log FILE`) is not built yet.
- Whether QuickTime Player and Jellyfin display the standard `title` tag next to the `vidfunnel` tag has been checked only through ffprobe, not on a real player.

## Sponsor / Donate

A tool like this takes a lot of time to get right: every legacy format has its own quirks, every ffmpeg major changes something, and nothing may ever touch an original by accident. If vidfunnel saved you time or a library, and you would like me to keep maintaining and improving it, you can throw some money at me on one of these platforms:

<a href="https://www.buymeacoffee.com/RedBearAK"><img src="https://img.buymeacoffee.com/button-api/?text=Buy me a coffee&emoji=&slug=RedBearAK&button_colour=865e3c&font_colour=ffffff&font_family=Bree&outline_colour=ffffff&coffee_colour=FFDD00" /></a>

https://buymeacoffee.com/RedBearAK

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/G2G34VVZW)

https://ko-fi.com/RedBearAK

## Licence

GPL-3.0-or-later.

# End of File #
