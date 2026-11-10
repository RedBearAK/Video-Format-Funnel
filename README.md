# Video-Format-Funnel

A command-line tool (`vidfunnel`) that funnels legacy video files — AVI with any codec, FLV, SWF with embedded video, FLIC, MPEG-1/2 and DVD VOB, WMV/ASF, old QuickTime MOV, RealMedia, MKV, and anything else ffmpeg can decode — into one archival output profile: H.264 High 4.1 and AAC in fragmented MP4, with closed keyframe groups every two seconds and optional PAR2 recovery data. Files that are already H.264 are remuxed, not re-encoded. Originals are never deleted by the conversion itself; a separate `retire` step moves them away only after verification, and only when asked.

The design document is `HANDOFF.md`; it is the full specification and the place to look when this README is too brief. `CLAUDE.md` holds the working conventions for the repo.

## What it does and does not do

- **Remuxes** a file whose video is already H.264 within the output limits (profile up to High, level up to 4.1, 8-bit 4:2:0, progressive). Only the container changes; the video bitstream is copied.
- **Re-encodes** everything else with libx264 at a transparent quality target (CRF 18 to 20 by height, `-preset slower`), deinterlacing or removing telecine where needed. Audio that is already AAC-LC or MP3 is copied; anything else becomes AAC-LC.
- **Writes fragmented MP4** with closed keyframe groups of at most two seconds on re-encodes, so damage to a file stays local and everything before it keeps playing. `--plain-mp4` writes a conventional `+faststart` MP4 instead.
- **Never touches originals** outside `retire`. `plan` writes no media at all, `convert` writes new files next to the sources, and `retire` refuses anything that has not been verified.
- **Records every decision with its reason**: one JSON record per source, holding the probe, the analysis, the sample positions, the ffmpeg command convert ran, the hashes, the verify results and the score.
- **Redacts reports by default**, so a report can be posted when asking for help without giving away file names or folder structure.

It does not scale video (except an integer upscale of tiny palette animations), does not crop letterbox bars, does not write interlaced output, and does not produce HEVC, AV1 or MKV.

## Requirements

- Python 3.9 or newer, standard library only. No packages to install, no virtualenv.
- ffmpeg and ffprobe 6 or newer, built with `libx264`, the native `aac` encoder, the `h264_metadata` bitstream filter and the `idet`, `bwdif`, `fieldmatch`, `cropdetect` and `setparams` filters. Any normal build with libx264 has all of these.
- Optional: ffmpeg 7 or newer built with libdvdread, for the `dvdvideo` demuxer (IFO-driven DVD input). Without it a `VIDEO_TS` tree is read by concatenating its VOB files, with a small confidence penalty.
- Optional: `par2` (par2cmdline) for `--par2`, `mediainfo`, and `ffsubsync` or `alass` for subtitle alignment.

Whether an ffmpeg has `dvdvideo` depends on the distribution's build flags, not on the version. As tested on 2026-11-02: Debian trixie (7.1.5), Arch (9.0.2) and Alpine (8.1.3) have it; Fedora's RPM Fusion ffmpeg 7.1.5 and openSUSE's Packman ffmpeg 7.1.5 do not. Homebrew's build has not been checked. Fedora's base `ffmpeg-free` has no libx264 at all, so Fedora needs RPM Fusion either way. Nobody packages `ffsubsync` or `alass`; `check` prints how to install them.

On an image-based host (Fedora Silverblue, Kinoite or CoreOS, the uBlue images such as uCore, Bazzite, Bluefin and Aurora, RHEL image mode) nothing is installed with `dnf`; packages are layered into the image with `rpm-ostree` and need a reboot. `check` detects such a host from the ostree boot marker, the `rpm-ostree` or `bootc` binary or the os-release variant, says so on its platform line, and puts the container route first in its hints, since it needs nothing on the host beyond podman or docker. The layering commands follow as the second option.

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

The capability listings those calls produce are cached under `~/.cache/vidfunnel/capabilities/` (or `$XDG_CACHE_HOME/vidfunnel/capabilities/`), keyed by the ffmpeg binary's path, size, modification time and a hash of its first and last MiB, so every other stage starts without the seven calls; a replaced or updated ffmpeg is queried again, and `check` always queries again and refreshes the cache. Through a distrobox wrapper each call costs more than a second, which is where the cache matters most.

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

Every argument that is an existing folder on the host is bind-mounted read-write at the same path inside the container, and the current directory is mounted too and used as the working directory. A file argument mounts its folder, because the records, the output and the sidecars are written beside the source. Each folder is also mounted at its physical path (symlinks resolved) when that differs, and for an argument that is a symlink, every link on the way and the folder it ends in are mounted too, so a folder argument that is a symlink to another disk, or a symlink inside the current directory, still points somewhere inside the container. The wrapper never mounts `/` and stops instead. `--quarantine DIR` and `--db PATH` (also written `--quarantine=DIR` and `--db=PATH`) are created on the host first (the folder holding `PATH`, for `--db`) and mounted, and `report --from=REPORT` and `find --root=PATH` are mounted like separate arguments. After the subcommand that has the option, the abbreviations argparse accepts are handled like the full name: `--q` and longer for `--quarantine` after `retire`, `--d` for `--db` after `index`, `find` and `report`, `--fr` for `--from` after `report`, and `--r` for `--root` after `find`. `report --from REPORT` re-runs the invocation stored in the report, but only the folder holding the report file is mounted, not the paths the report names, so give those paths again as arguments (as for a redacted report). `--ffmpeg` is refused before the container starts, because the image's own ffmpeg is the only one that works inside it; `VIDFUNNEL_FFMPEG` from the host is not passed in either. The container runs with `--userns=keep-id` under podman (`--user uid:gid` under docker) so that records and outputs are owned by you, and with `--security-opt label=disable` for SELinux hosts. Your supplementary groups are kept inside the container where the engine allows it, so a share writable only through a group you belong to (a NAS mount owned `root:media` with mode 775) stays writable: podman on the crun runtime gets `--group-add keep-groups`, docker gets `--group-add` for each of your group ids. podman on runc cannot keep them (runc ignores `keep-groups`, and numeric ids map to subordinate gids under `keep-id`); on runc the wrapper prints a warning naming each mounted folder that you can write only through one of those groups (owned by someone else, writable by its group but not by everyone, its group one of your supplementary groups and not your primary group), and stays quiet when no mounted folder depends on one, as on a CIFS mount that presents every file as your own. The container gets a terminal only when both stdin and stdout are terminals; piped (`| less`, `| tee`, `> file`) it gets no terminal and no stdin, so the pipe's reader owns the terminal and nothing is left in raw mode afterwards (vidfunnel's one prompt, retire's `--destroy-originals-now`, refuses without a terminal unless `--yes`). `VIDFUNNEL_NICE` runs the command under `nice -n VALUE` and `VIDFUNNEL_IONICE_CLASS` under `ionice -c VALUE`, which is worth setting on a server that does other work. `--image` or `VIDFUNNEL_IMAGE` choose another image; setting `VIDFUNNEL_IMAGE_DIGEST` near the top of the script pins the image by digest so it changes only when that line is edited.

The container gets your `HOME` (`-e HOME=$HOME`): with `--userns=keep-id` podman adds a passwd entry for your uid, but its home is not usable (`/` on one host, root-owned and read-only; the working directory on another), so without this every `~` would resolve to the wrong place. The wrapper creates `~/.local/state/vidfunnel` (where the index database lives by default) and `~/.cache/vidfunnel` (the ffmpeg capability cache) on the host and mounts both at the same path (and at their physical path when `~/.cache` or `~/.local/state` is a symlink), so they persist from one run to the next and are owned by you. Inside the container `HOME`'s own directory is root-owned and not writable; only those two folders are, and vidfunnel writes nothing else under `HOME`. If `HOME` is unset or empty, the wrapper stops instead of guessing.

To run the container as plain `vidfunnel`, put the wrapper on PATH:

```
bin/vidfunnel-podman --install-command
```

This runs on the host, not in the container, and needs neither podman nor docker. The wrapper recognises `--install-command` and `--uninstall-command` (or an abbreviation argparse accepts, such as `--install`) among the options before the subcommand, skipping the value of `--ffmpeg` or `--launcher`; `bin/vidfunnel-podman check --install-command` goes to the container. With `--build` the image is built first, which does need an engine. It makes `~/.local/bin/vidfunnel` (or `/usr/local/bin/vidfunnel` with `--system`) a symlink to the wrapper; `--uninstall-command` removes it. When the host has Python 3.9 or newer, the repo's own installer does this, as with `bin/vidfunnel --install-command`, including the offer to add `~/.local/bin` to PATH. Without Python the wrapper does it itself: it replaces only a symlink to the wrapper or to `bin/vidfunnel`, refuses anything else at that path, and prints the PATH line for your shell's startup file instead of editing it; for `--system` it needs `/usr/local/bin` to be writable and otherwise prints the `sudo ln -s` command to run.

Long runs are detached. `plan`, `convert` and `verify` (`convert --refit` too) run in a detached container named `vidfunnel-STAGE-HASH`, where the hash comes from the working directory and the first path argument, labelled with the stage, the folder, the paths and the start time. The wrapper follows the container's output and, once it exits, removes it and exits with its exit code. If the terminal, the wrapper or a `| tee` after it dies (a SIGQUIT from the terminal once took down all three on the server, and the run went on headless with nobody watching), the run goes on and can be followed again. Ctrl+C still stops it: the wrapper sends SIGTERM through `podman stop`, vidfunnel ends cleanly, and the wrapper shows the rest of its output; a second Ctrl+C only stops following. A closed terminal leaves the run alone. A detached run has no terminal, so progress arrives as whole lines. While a run on the same paths is going on this host, a second start is refused with that run's name; containers on other hosts are not visible to podman here, and between hosts the folder locks described under "Several machines on one share" keep runs apart. `VIDFUNNEL_ATTACHED=1` runs these stages attached, as the other subcommands are. With the wrapper installed as `vidfunnel`:

```
vidfunnel attach
vidfunnel attach vidfunnel-convert-3c7019ba93c9
vidfunnel attach --clean
```

`vidfunnel attach` lists this host's vidfunnel containers, or follows the one started in the current folder when there is just one. `attach NAME` (or the start of a name, when only one container's name starts with it) follows that container, replaying its whole log first, so `vidfunnel attach NAME | tee -a convert.log` fills in what a dead `| tee convert.log` missed, and exits with the run's exit code once it ends. `attach --clean` removes the containers that exited while nobody followed them, printing each one's exit code first; a new run of the same name does that for its predecessor, with its last 20 lines.

```
vidfunnel stop
vidfunnel stop --now
vidfunnel stop-all /srv/media
vidfunnel resume /srv/media
```

There are three stop commands. `vidfunnel stop` (or `stop NAME`) is for this host's run only: it asks the run to drain, so the items in progress finish, no new ones start and vidfunnel exits normally, which lets the server be rebooted without losing the encodes in progress; `--now` is the immediate stop (SIGTERM). Both follow the run until it ends. `stop` refuses a path and points to `stop-all`. `vidfunnel stop-all PATH` stops every `plan`, `convert`, `verify` and `retire` run under that path on every host, through the share, and `vidfunnel resume PATH` lifts that stop (deleting `PATH/.vidfunnel/stop` does the same); these two are vidfunnel's own commands and run in the container like `check`.

## Usage

The pipeline is five separate subcommands, run in order. Each one is idempotent and resumable: run it again on the same folder and it picks up where it left off. Folders are walked recursively. `plan`, `convert`, `verify`, `report` and `index` work on the current directory when no PATH is given and print the resolved folder first (`report --index` reads the central index instead); `retire`, the only stage that moves originals, always needs an explicit PATH. Global options (`--ffmpeg`, `--quiet`, `--verbose`, `--yes`) go before the subcommand name; `--ffmpeg` defaults to the `VIDFUNNEL_FFMPEG` environment variable when set.

The examples below are real output from a scratch folder holding copies of six test fixtures under made-up names: two H.264 MKVs, an H.264 MP4, an XviD AVI, and a `movie.avi`/`movie.mkv` pair that share a name.

### plan

```
vidfunnel plan "Home Videos"
```

`plan` probes and analyses each source and writes its record into the folder's `.vidfunnel/` directory: the probe, the decisions with their reasons and sample positions, the predicted score, and a preview of the ffmpeg command as this machine would run it (`convert` builds its own command when it runs, see below). It never writes media. The record also notes the ffmpeg the plan was made with (its path, version and a hash of its build configuration), as information about the analysis. A source whose record still matches it is skipped on the next run; `--replan` plans it again. The tail of the output, with the summary table:

```
[plan 6/6] probing 'movie.mkv'
[plan 6/6] decode check of 'movie.mkv'
[plan 6/6] [decode] 00:00:05 / 00:00:05  100.0 %  0 fps  136.0x  eta 00:00:00
[plan 6/6] idet, blockdetect over 1 window(s) of 'movie.mkv'
[plan 6/6] [window pass] 1/1  100.0 %
[plan 6/6] reading packet timing of 2 stream(s) of 'movie.mkv'
[plan 6/6] measuring keyframe spacing of 'movie.mkv'
[plan 6/6] [keyframes] elapsed 00:00:00  1 keyframes in 125 packets
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

- `--samples N` sets the number of sample windows for `idet` and `cropdetect` (default 12 windows of 3 s, skipping the first and last 3 %); `--full-analysis` analyses the whole file. Files shorter than 60 s are always analysed in full. Each window is decoded once, by one ffmpeg running `idet`, `cropdetect` (when the aspect rule needs bar evidence) and `blockdetect` together; `blockdetect` runs in at most 4 of the windows, spread evenly.
- `--jobs N` plans N files at once (default 1), also within one folder. On a four-core machine, four copies of a 3-minute MPEG-2 clip planned in 21 s with `--jobs 4` and in 39 s with `--jobs 1`. The records are the same as with `--jobs 1`; files that would get the same output name are planned one after another. A good value is one per physical core (6 on a six-core machine): each worker runs one ffmpeg at a time and the legacy decoders use about one core, while SMT siblings add little. Plan also reads every file in full twice (the error-scan decode and the packet probe), so on a library reached over a network mount the read path, not the CPU, is often the limit; a load average well below the core count with the disk or network busy means fewer jobs would be no slower.
- `--assume-dar 4:3|16:9` resolves an SD frame whose aspect cannot be told from the pixels.
- `--audio-offset S` replaces the measured audio start offset.
- `--audio-strict-aac` re-encodes MP3 to AAC instead of copying it.
- `--reencode-video` re-encodes H.264 that could be remuxed, for when two-second keyframe groups matter more than avoiding a second generation.
- `--max-level 4.1` deinterlaces HD at frame rate to stay at level 4.1 (the default allows 4.2 for field-rate HD).
- `--plain-mp4` plans a conventional `+faststart` MP4 instead of a fragmented one.
- `--par2 N` records that `convert` should write PAR2 with N % redundancy.
- `--mark-outputs` names every output `<stem>.vidfunnel.mp4` and its subtitle sidecars `<stem>.vidfunnel.srt` (`.sup`, `.subs.mkv`) until `retire` has moved the original away and renamed them to `<stem>.mp4` and `<stem>.srt`. While the originals are still there, a converted file is then recognisable at a glance in a file manager or in Jellyfin; after `retire` the names are clean. Without it, only an MP4 source, whose output would otherwise take its own name, goes through the interim name. The choice is stored in the record, so `convert`, `verify`, `report` and `convert --refit` find the output under whichever name it has; `--replan` with or without the flag changes it for files not yet converted. A file already at the final name that is not ours stops the plan with `output-collision`, as without the flag, since `retire` would have to rename over it. DVD titles keep their final names, since `retire` does not retire a DVD folder.

### convert

```
vidfunnel convert "Home Videos"
```

`convert` builds and runs the ffmpeg command for every record with status `planned`, under the output name the plan chose (the interim `<stem>.vidfunnel.mp4` for an MP4 source and for every source planned with `--mark-outputs`). ffmpeg writes to a hidden `.NAME.mp4.part` file in the same folder, which is renamed into place on success; the output gets the source's modification time, subtitle sidecars are written the same way, PAR2 is written when asked, and `verify` runs on the result straight away. A failed run leaves the record at `failed` with ffmpeg's error output, and removes the part file. A record that is already converted and whose output still matches is skipped.

The plan says what to do with a file; `convert` works out the command on the machine it runs on, with that machine's ffmpeg and the folder as it finds it. A library that was moved or is mounted somewhere else, a switch between the native install and the container, or a rebuilt image with a newer ffmpeg needs no new plan. Before each file, `convert` checks that its ffmpeg has what that file needs (`libx264`, the filters the plan chose, `dvdvideo` for a DVD title) and skips the record with the reason when it does not. The record of a converted file holds the command that ran, the ffmpeg version and build that ran it, and the vidfunnel version. `--refit` and `--dry-run` work the same way; `--dry-run` shows the commands this machine would run.

Ctrl+C stops a run without leaving false records: the files in progress keep the state they had, their part files are removed and the folder locks released, and the next run picks them up. `kill`, or `podman stop` for a run in the container, has the same effect (SIGTERM is passed on to the running ffmpeg and par2 of a `--jobs` run, which are waited for; a run without `--jobs` kills and reaps its own child on the way out); the run then prints `vidfunnel: terminated (SIGTERM)`. A closed terminal (SIGHUP) and Ctrl+Backslash (SIGQUIT) stop a run the same way and are named in that line; a SIGHUP that `nohup` has set to be ignored stays ignored. Once the terminal or the reader of a pipe (a `| tee` killed by the same keypress) is gone, the run's remaining output is discarded rather than failing the files in progress. Only the first of these signals counts: a later one does nothing, so it cannot cut that cleanup short.

**Stopping runs.** Ctrl+C, SIGTERM, SIGHUP and SIGQUIT stop now, as above. SIGUSR1 drains `plan`, `convert`, `verify` and `retire` instead: the files in progress run to their end and are recorded as usual (`convert` verifies them as always), no further file is started, and the run exits with its normal exit code after printing `vidfunnel: drained: finished N in progress, started no more (M left planned)` (`not started` for `plan`, `convert --refit`, `verify` and `retire`). The other subcommands (`check`, `report`, `find`, `index`) have no files to stop between; they run to their end and say so. That is the way to let a server reboot without losing the encodes in progress; the files left over stay `planned` for the next run. On a host run it is `kill -USR1 <pid>`; a second SIGUSR1 does nothing, while Ctrl+C or SIGTERM during a drain still stop at once. Through the container wrapper, `vidfunnel stop` drains this host's run (`stop --now` stops it). `vidfunnel stop-all PATH` asks every `plan`, `convert`, `verify` and `retire` run working at or below PATH, on every host that sees the share, to drain: it writes `PATH/.vidfunnel/stop`, which each run looks for, walking up from a file's folder, before it starts that file (and, for `--now`, before each ffmpeg or par2 it starts, so a stop-all `--now` takes effect at the next file or tool start, not in the middle of an encode; a file whose encode has finished still gets its subtitle sidecars and PAR2, and its record is saved, so `convert` never throws a finished encode away (in a `--jobs` run too, when another worker finds the file), and the stop takes effect at its verify or the next file; `retire` stops before its next file only, so no file is left half retired). A run that finds the file says so, with the host and time of the request; the drain then holds for that whole run, so a stop file on one subfolder also ends a run started on the folder above it, once that run reaches a file in the subfolder. While the file exists, `plan`, `convert`, `verify` and `retire` refuse to start under PATH and name the file. `vidfunnel resume PATH` removes it (deleting the file does the same); a run already draining keeps draining.

```
[convert 2/4] re-encode 'Birthday party 2004-06-12.avi' -> 'Birthday party 2004-06-12.mp4'
[convert 2/4] [re-encode] 00:00:04 / 00:00:05  98.4 %  0 fps  10.2x  eta 00:00:00
[convert 2/4]   converted 'Birthday party 2004-06-12.mp4' (149212 bytes, 67 % of the source) in 0.7 s
[convert 2/4] verifying 'Birthday party 2004-06-12.mp4' (146 KiB)
[convert 2/4] [verify decode] 00:00:05 / 00:00:05  99.2 %  0 fps  147.0x  eta 00:00:00
[convert 2/4]   verified 'Birthday party 2004-06-12.mp4': score 100 (ok); frames 125 of 125 expected, duration delta 0.04 s
[convert 3/4] remux 'Phone clip 3.mp4' -> 'Phone clip 3.vidfunnel.mp4'
[convert 3/4] [remux] 00:00:04 / 00:00:05  98.4 %  0 fps  573.0x  eta 00:00:00
[convert 3/4]   converted 'Phone clip 3.vidfunnel.mp4' (103872 bytes, 100 % of the source) in 0.2 s
[convert 3/4] verifying 'Phone clip 3.vidfunnel.mp4' (101 KiB)
[convert 3/4] [verify decode] 00:00:05 / 00:00:05  100.0 %  0 fps  156.0x  eta 00:00:00
[convert 3/4]   verified 'Phone clip 3.vidfunnel.mp4': score 100 (ok); frames 125 of 125 expected, duration delta 0.0 s
[convert 4/4] remux 'Wedding DVD rip.mkv' -> 'Wedding DVD rip.mp4'
[convert 4/4] [remux] 00:00:04 / 00:00:05  98.8 %  0 fps  1300.0x  eta 00:00:00
[convert 4/4]   remuxed output is larger than the source (114714 > 112684 bytes); container overhead, kept
[convert 4/4]   converted 'Wedding DVD rip.mp4' (114714 bytes, 102 % of the source) in 0.2 s
[convert 4/4] verifying 'Wedding DVD rip.mp4' (112 KiB)
[convert 4/4] [verify decode] 00:00:05 / 00:00:05  99.8 %  0 fps  60.0x  eta 00:00:00
[convert 4/4]   verified 'Wedding DVD rip.mp4': score 85 (review); frames 150 of 150 expected, duration delta 0.0 s
convert: 0 converted, 4 verified, 0 skipped, 0 failed in 00:00:04; records in '.vidfunnel/' under each folder
```

Every line about one file starts with that file's counter, `[convert 3/4]`, the live progress lines too, so the lines of `--jobs 2`, where two files are converted at once and their lines alternate, still say which file each belongs to. On a terminal a run without `--jobs` updates its progress line in place; with `--jobs` above 1 the progress lines are written as new lines every 30 seconds instead, since two lines cannot both be updated in place. `plan` (`[plan 6/6]`), `convert --refit` (`[refit 1/1]`) and `verify` (`[verify 2/5]`) tag their lines the same way.

The re-encode came out at two thirds of the source's size. The MP4 source was written under the interim name because the output name is its own, and the letterboxed file keeps its 15-point aspect penalty from `plan`, which puts it in the `review` band.

Options:

- `--dry-run` prints the commands and writes nothing.
- `--jobs N` converts N files at once. The default is 1, because x264 already uses every core for one file; `--jobs` is for remux-heavy sets. On standard-definition sources x264 cannot use many threads (its frame threading scales with picture height and flattens out around six to eight threads at 480 lines), so on a six-core machine two encodes side by side fill it where one leaves it partly idle; three is the most worth trying, and HD sources want 1. Each job also multiplies memory and read traffic.
- `--par2 N` writes PAR2 with N % redundancy, overriding the planned value (10 is a reasonable choice).
- `--refit` revisits re-encodes that came out larger than their sources, described below.
A re-encode that comes out larger than its source is kept like any other output and flagged `output-larger`, with both sizes in the record; convert prints `output is larger than the source (N > M bytes); kept and flagged 'output-larger'`. `retire` refuses such a record unless `--allow-larger` is given, so you can compare the two files first. Remuxes are exempt from this size rule: only the container changed, and fragment headers can make a small file grow slightly. A larger remux is kept and flagged `remux-larger` for information.

`convert --refit` gives those larger re-encodes one more try. It selects the records under the given paths that are `converted` or `verified`, re-encoded, flagged `output-larger` and not yet flagged `fit-capped`; the rest are skipped and counted by reason (`not converted`, `not a re-encode`, `not larger`, `already capped`, `already refitted`). For each selected record it raises the CRF the existing output was made with by `ceil(ln(ratio) / ln(1.12))` steps, where `ratio` is the output size over the source size: one CRF step of `-preset slower` saved 11 to 12.5 % of size on the material this rule was measured on, and 1.12 per step is a round figure a little below that, so an output 1.42 times its source goes from CRF 18 to 22, and an output 1.08 times its source goes one step. The CRF is capped at 23 for sources up to 576 lines and at 24 above. The caps are a chosen quality floor, not a measured limit: past them the loss is judged not worth the space, so a refit that would need more is capped and the existing output is kept. When `plan` measured the source as heavily compressed (fewer than 0.15 bits per pixel per frame) and blocky (blockdetect's score at or above 3.5), a light `hqdn3d=2:1:2:3` denoise goes into the filter chain as well (first, or right after the deinterlacing or inverse-telecine filter), so the encoder spends fewer bits on the blocks. The rest of the command is built as `convert` builds it, and the result is written to the hidden part file. If it is smaller than the existing output and passes the identity, stream and duration checks, it replaces the output, the record gets the flag `refitted` and goes back to `converted`, and `verify` runs on it as after a conversion; `output-larger` is removed only when the new output is no larger than the source. Otherwise the part file is deleted, the existing output is kept, and the record gets the flag `fit-capped` with the attempt and the reason. The record holds the CRF, the filters, the size and the ratio of the attempt. A record gets one refit; only an attempt whose ffmpeg run failed is tried again on a later `--refit` run. `--dry-run` with `--refit` prints the refit commands and writes nothing.

```
vidfunnel convert --refit "Home Videos"
```

```
[refit 1/1] 'Web clip 2006.avi': CRF 18 -> 19, hqdn3d, ratio 1.08
[refit 1/1]   size class 'heavy' and blockiness 173.8731 at or above 3.5: hqdn3d=2:1:2:3
[refit 1/1] [refit] 00:00:05 / 00:00:05  97.7 %  0 fps  13.8x  eta 00:00:00
[refit 1/1]   refit 'Web clip 2006.avi': CRF 18 -> 19, hqdn3d, ratio 1.08 -> 1.05; replaced 'Web clip 2006.mp4' (123535 bytes, was 126343); still larger than the source, 'output-larger' kept
[refit 1/1] verifying 'Web clip 2006.mp4' (121 KiB)
[refit 1/1] [verify decode] 00:00:05 / 00:00:05  97.8 %  0 fps  162.0x  eta 00:00:00
[refit 1/1]   verified 'Web clip 2006.mp4': score 100 (ok); frames 124 of 125 expected, duration delta 0.12 s
skipped: 1 not larger
convert --refit: 1 refitted, 0 kept, 1 skipped, 0 failed in 00:00:01; records in '.vidfunnel/' under each folder
```

That run is the H.264 AVI test clip re-encoded with `--reencode-video`: one CRF step and the denoise took it from 108 % to 105 % of the source, so it was replaced but keeps `output-larger`, and `retire` still asks for `--allow-larger`. The synthetic test pattern compresses unlike real footage, which is also why its blockiness score is so high.

### verify

```
vidfunnel verify "Home Videos"
```

`convert` already verifies each output; run `verify` again after moving files, or to add the SSIM comparison. It decodes every video and audio stream of the output in full, compares the video stream's duration with the source's (more than 0.1 s plus one frame apart costs 20 points), exact frame count (outside 2 % plus 2 frames costs 30) and stream counts with the source (a video, audio or text-subtitle stream missing from the output costs 50), reads back the identity tag (missing or naming another record or source costs 100), and records the output's SHA-256, size and size ratio. `--ssim-check` also compares sampled windows with the source by SSIM on luma (below `--ssim-threshold`, default 0.97, costs 25; `--samples N` sets the windows). It is slow and off by default. Like `convert`, it skips a record whose source, output or sidecar names are not plain file names in its folder, and names the field; with `--ssim-check` it also skips a re-encode whose filter values are not of the form `convert` accepts.

The score is 100 minus the penalties from the analysis and from verify. Bands: 90 and above is `ok`, 60 to 89 is `review`, below 60 is `hold`. Verifying again replaces the earlier verify penalties rather than adding to them, and the previous result is kept in the record's history.

### report

```
vidfunnel report "Home Videos"
```

`report` reads the records and prints them, worst scores at the top and unscored rows (planned, failed, quarantined) below them. It never probes or encodes. Names and paths are redacted by default; the top-level path becomes `<root>`. Each record ID is shown as its first 8 characters, which `vidfunnel find` takes as a prefix (more characters only when two of the records the report read, including rows a filter left out, share those 8).

The default format, `text`, is for reading in a terminal. Rows are grouped by folder, and the folder line is printed once per group; the folders come in the order of their worst row, and the rows inside a folder keep the report's order, so the first row printed is still the worst one. Groups follow the real folders, so two folders whose redacted names look alike (`Christmas 1998` and `Christmas 2001` are both `C_______s ####`) stay two groups under the same heading. Each record takes two lines: score, band, status, action, flags and ID, then the source name, the output name and the sizes. The output name is the name the output has or will have: the interim `<stem>.vidfunnel.mp4` until `retire` renames it, and still after a `retire` whose rename failed. A quarantined or failed record gets a third line, `reason:`, with why: the plan's reason for a quarantine (for example `decode-failed: ...` or `output-collision: ...`, or the quarantine code alone when the record stores no more), and for a failure the stage and the first line of its stored error (with the next line joined to it when the first ends with a colon, as in `'ffmpeg' exited with status 1:`, whose cause follows), followed by ` ...` when the error has more lines. `--with-logs` prints the whole error when it has more than one line, unless the stored stderr holds the same text, which it prints anyway. The reason is shown with or without `--with-logs`, redacted like the rest; a file name or path in single quotes in it is redacted even when no record the report read holds that name, such as the other source of an output collision that has since been removed, and a report of one file also learns the names of the other records in its folder. A line break inside a name (including the Unicode line and paragraph separators) is printed as a space, so each row keeps its lines.

The examples below come from a library of six fixture files from `tests/make_fixtures.sh` copied under realistic names: `h264_avi_mp3.avi` as `Family Tapes/Wedding Day reception 2004-06-12 part 2.avi`, `h264_long_gop_mkv_aac.mkv` as `Family Tapes/Birthday party at Grandmother's.mkv`, `mpeg2_ps_ntsc_dar_4x3_ac3.mpg` as `Christmas 1998/Christmas Night - December 1998 pt1.mpg`, `h264_mp4_aac.mp4` as `Christmas 1998/Presents opening.mp4`, and the two files of `collision_pair/` as `Christmas 1998/Sledding.avi` and `Sledding.mkv`. The library was planned, and `Family Tapes` converted, before `vidfunnel report --no-index "Home Videos"`:

```
vidfunnel report

Names in this report are redacted. To see them, re-run the same command with --reveal; for a report that --from can read back, use --format md. To locate one row: vidfunnel find <ID>.

report ID: 4fbd7b47-1e53-428f-934c-56975917925d
generated at: 2026-11-09T02:33:52Z
tool version: 20261108
invocation: vidfunnel report --format text --no-index -- '<root>'
redacted: yes
records: 6

<root>/F____y T___s/
   90  ok  verified     remux       decode-warnings         50c73ca6
       W_____g D__ r_______n ####-##-## p__t #.avi  ->  W_____g D__ r_______n ####-##-## p__t #.mp4   (115 KiB -> 101 KiB)
  100  ok  verified     remux       long-gop, remux-larger  b789e3c4
       B______y p___y a_ G_________r's.mkv  ->  B______y p___y a_ G_________r's.mp4   (238 KiB -> 241 KiB)

<root>/C_______s ####/
   --  --  planned      reencode    --                      b205a2d9
       C_______s N___t - D______r #### p_#.mpg  ->  C_______s N___t - D______r #### p_#.mp4   (662 KiB -> --)
   --  --  planned      remux       --                      a668761e
       P______s o_____g.mp4  ->  P______s o_____g.v_______l.mp4   (102 KiB -> --)
   --  --  quarantined  quarantine  output-collision        7fead68d
       S______g.avi  ->  S______g.mp4   (217 KiB -> --)
       reason: output-collision: another source in this folder would also be converted to 'S______g.mp4'; a person picks one
   --  --  quarantined  quarantine  output-collision        fa1bc930
       S______g.mkv  ->  S______g.mp4   (100 KiB -> --)
       reason: output-collision: another source in this folder would also be converted to 'S______g.mp4'; a person picks one
```

With `--format md` the same records make a Markdown table to paste into a note or an issue (shown here without the one machine-readable header comment that `--from` uses). The `source` column shows the output name only when it is not the source's stem plus `.mp4`, as for an MP4 source converted under its interim name; the reasons of quarantined and failed rows follow the table in a Reasons section, and the folders are listed once after that, with the IDs of their rows:

```
# vidfunnel report

> Names in this report are redacted. To see them, re-run the same command with --reveal, or vidfunnel report --from <this file> --reveal. To locate one row: vidfunnel find <ID>.

- report ID: 7427b2b0-bfae-4068-bf9b-d3da23df4563
- generated at: 2026-11-09T02:33:52Z
- tool version: 20261108
- invocation: vidfunnel report --format md --no-index -- '<root>'
- redacted: yes
- records: 6

| ID | score | band | status | action | flags | source | size |
|---|---|---|---|---|---|---|---|
| 50c73ca6 | 90 | ok | verified | remux | decode-warnings | W_____g D__ r_______n ####-##-## p__t #.avi | 115 KiB -> 101 KiB |
| b789e3c4 | 100 | ok | verified | remux | long-gop, remux-larger | B______y p___y a_ G_________r's.mkv | 238 KiB -> 241 KiB |
| b205a2d9 | -- | -- | planned | reencode | -- | C_______s N___t - D______r #### p_#.mpg | 662 KiB -> -- |
| a668761e | -- | -- | planned | remux | -- | P______s o_____g.mp4 -> P______s o_____g.v_______l.mp4 | 102 KiB -> -- |
| 7fead68d | -- | -- | quarantined | quarantine | output-collision | S______g.avi | 217 KiB -> -- |
| fa1bc930 | -- | -- | quarantined | quarantine | output-collision | S______g.mkv | 100 KiB -> -- |

## Reasons

- 7fead68d: output-collision: another source in this folder would also be converted to 'S______g.mp4'; a person picks one
- fa1bc930: output-collision: another source in this folder would also be converted to 'S______g.mp4'; a person picks one

## Folders

- <root>/F____y T___s/: 50c73ca6, b789e3c4
- <root>/C_______s ####/: b205a2d9, a668761e, 7fead68d, fa1bc930
```

Options:

- `--reveal` shows the real names and paths. `--redact-dates` truncates creation times and mtimes to the year.
- `--format text|md|json|csv`: text (the default) is for reading; Markdown pastes into a note or an issue; JSON and CSV are for processing and keep the full record IDs; they carry the reason line as a `reason` field (null in JSON and empty in CSV for rows without one); in CSV the `reason` column comes before the `logs` column that `--with-logs` adds. CSV writes its header to stderr. Only Markdown and JSON reports can be read back with `--from`, so the hint of a redacted text or CSV report points to `--format md` instead of `--from`.
- `--min-score N`, `--max-score N` and `--status S` (repeatable) filter the rows.
- `--with-logs` adds the stored ffmpeg error output and run log of each row, and a stage's error text when it has more than one line, redacted like the rest. This is the bundle to post when asking why a file misbehaved.
- `--from REPORT` re-runs the invocation recorded in an earlier Markdown or JSON report, with whatever options you add, so a redacted report that was sent out can be reissued with `--reveal`. A row whose status has moved on since that report is marked as changed. The ID prefixes of a Markdown report are matched against the records found now; a prefix that matches more than one record is an error that names both. Because a redacted report records its paths as `<root>`, give the PATH arguments again, or have the central index available. A text report cannot be read back, and `--from` says so, also when the file was saved with the progress lines from stderr in front of it. A revealed Markdown report whose invocation names no PATH (one made from the index) reads the whole index again and keeps the rows that report lists, wherever their folders are now; only when no index can be read (`--no-index`, or no database file) does it read the folders listed in its Folders section, skipping with a warning any folder that no longer exists. Markdown reports written before version 20261108, with full IDs and a `path` column, are still read.
- With no PATH, `report` reads the current directory like the other stages; `--index` reads the central index instead (every tree `index` has loaded), `--db PATH` names another index file, and `--no-index` always reads the records.

To map a row of a redacted report back to a real file on your own machine:

```
vidfunnel find 50c73ca6
```

`find` takes a record ID or any unique prefix and prints the ID, status, score, action, and the real source, output and record paths. It uses the index when there is one and otherwise searches the records under `--root` (default: the current directory).

### retire

```
vidfunnel retire "Home Videos"
```

`retire` is the only subcommand that touches an original. For each source it checks, in order, and stops at the first failure without moving anything:

1. a usable record exists and its status is `verified`;
2. the source still matches the record's fingerprint (its size and its first and last MiB), so it has not been replaced since it was planned; when its modification time is more than 2 seconds from the one recorded at plan, retire first reads the whole source once and compares its SHA-256 with the record's, and refuses if it differs;
3. the output exists;
4. the score is not in the `hold` band, and not in the `review` band unless `--allow-review` is given;
5. a re-encode's output is not larger than the source and the record is not flagged `output-larger`, unless `--allow-larger` is given (the record is then flagged `allowed-larger`, so the report shows the choice);
6. the output's full SHA-256 matches the record (the one expensive check, so it runs last);
7. an output under the interim `<stem>.vidfunnel.mp4` name, and its interim-named subtitle sidecars (`--mark-outputs`), can be renamed to their final names;
8. the quarantine destination does not exist yet.

Only then is the original moved into the quarantine directory, by default `.vidfunnel-retired/` under each PATH given, keeping its relative path; `--quarantine DIR` chooses another. An interim-named output is then renamed to its final name, and its interim-named sidecars with it. The record stays as the audit trail with status `retired`. Outputs are never removed. In the example folder, retire moved the three originals that scored 100 into `.vidfunnel-retired/` (renaming `Phone clip 3.vidfunnel.mp4` to `Phone clip 3.mp4` on the way), skipped the two quarantined sources, and refused the letterboxed one:

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

### Why nothing here uses a GPU

Nothing in the pipeline uses a GPU, by ffmpeg's defaults and by choice. The output is made by libx264, which runs on the CPU. ffmpeg's hardware H.264 encoders (NVENC, Quick Sync, VAAPI, VideoToolbox) are many times faster, but they are built for real-time work: at the same file size their picture quality is clearly below x264 at `-preset slower`, they have no equivalent of x264's slow rate-distortion search, and their rate control has no true constant-quality mode comparable to CRF. For an archive whose point is a transparent re-encode of lossy sources that trade is the wrong one, so the tool does not use them even when they are present; ffmpeg never switches to them on its own.

Decoding is the same story. ffmpeg uses a hardware decoder only when asked with `-hwaccel`, and many of the formats this tool exists for have none: WMV1 and WMV2, FLV1, Sorenson, Cinepak, MS Video 1, RLE, DV. MPEG-2, H.264, MPEG-4 part 2 (Xvid, DivX) and WMV3 do on some hardware, but the analysis filters (idet, cropdetect, blockdetect, bwdif) run on the CPU, so every decoded frame would be copied back from the GPU, which on standard-definition material is likely to cost about what it saves; this has not been measured here.

The time goes into x264 during `convert`, which scales across CPU cores, and into single-threaded legacy decoders during `plan`, which `--jobs` spreads across files. A machine with a strong GPU and a weak CPU gains nothing here; one with more cores gains in both stages.

## Several machines on one share

The records and the folder locks live in the `.vidfunnel/` directory beside the videos, so more than one machine can run `plan`, `convert` or `verify` on the same share at the same time. The unit of work between machines is the folder: a machine takes a folder's lock before it touches the folder's records and skips a folder another run holds, so two hosts never work in one folder at once, while `--jobs` still parallelises the files inside a folder on each host. A library with one folder per title divides naturally; one flat folder with a thousand files is one host's at a time. A lock is created exclusively and is taken over at once when its holder has exited on the same machine, including a run in a container on that machine that was killed outright (its holder keeps a `flock` on the lock file, which the kernel frees when the holder and its ffmpeg children are gone; on NFS when the holder alone is gone, see below). A reboot of the machine that wrote a lock frees it at once for runs on that machine, on the host or in the container; other machines still wait 24 hours. This relies on each machine having its own `/etc/machine-id`; two machines cloned from one image without regenerating it (`systemd-machine-id-setup`) would be taken for one machine and could take over each other's live locks. A lock from another machine is taken over after 24 hours, and says so when it is skipped.

The machines do not need the same ffmpeg build. Each one renders the command for a record with its own ffmpeg, checks first that this ffmpeg can do what the record asks, and writes the build it used into the record. A host's distro ffmpeg, Homebrew's on a Mac and the Alpine image can work on one share; a machine whose ffmpeg lacks something a record needs (no `libx264`, no `dvdvideo` for a DVD title) skips that record and says why. If you want one encoder for the whole library, use the container image on every machine. On macOS the container runs inside podman's Linux VM, so the share has to be mounted inside that VM as well as on the Mac, and the encodes run through virtualisation.

The share's semantics are the ordinary ones: records are written through a temporary file and an atomic rename, locks through an exclusive create, and both behave on SMB and NFS as they do locally. The lock takeover also takes a `flock` on `.vidfunnel/lock.takeover`; a network filesystem that refuses it leaves the takeover guarded by the identity re-check and the exclusive create alone, which admits a race only when two hosts take over the same dead lock in the same instant. A `flock` does not reach across machines, so the lock's own `flock` only tells runs on the same machine whether its holder is alive (the boot id in the lock says which machine wrote it); between machines the 24-hour rule applies. Clock skew between hosts affects only the 24-hour rule. Mounts may report a file's modification time at different resolutions (SMB in 100 ns steps, some NFS servers in whole seconds, FAT and exFAT in 2 s steps), so a file is judged unchanged by its size and the SHA-256 of its first and last MiB, not by its time; a stage that goes ahead with a differing time notes both values in its section of the record, and a source whose content differs is skipped with its record left as it is, to be planned again with `plan --replan`. Each machine wants its own clone and its own `~/.cache`; only the videos and their `.vidfunnel/` directories are shared.

## Records and identity

Each folder that `plan` processes gets a hidden `.vidfunnel/` directory with one JSON record per source, named after the full source file name with its extension (`movie.avi.json`, `movie.mkv.json`), so two sources with the same stem never share a record. Quarantined sources get records too. Each record holds a random record ID, the source fingerprint (size, mtime, SHA-256 of the first and last MiB) and full SHA-256, every probe result and decision with its reason, the ffmpeg command, the output name, hashes, verify results, score and status, with a timestamped section per stage. Every write is atomic. `folder.json` holds the last scan time, the tool version and the inventory of source names seen. While a run is active, `.vidfunnel/lock` records the host name, pid, start time, whether the run is inside a container, the machine id and where it came from (`machine_source`), the kernel boot id and the file's device number, so two machines working on the same share cannot run in one folder at once. The run also holds a `flock` on the lock file from start to end (`"flock": true` in the file), and the ffmpeg and par2 it starts inherit it, so the kernel frees that `flock` only when the run and every child it started have exited, however they ended. A run on the same machine (the same boot id, inside a container or not) that sees the lock on the same device tests that `flock`: while something holds it the folder is skipped, whatever the lock's age, and the message says so; once nothing holds it the lock is taken over at once, with a message naming the holder. An ffmpeg left running by a killed vidfunnel therefore keeps its folder locked until it ends. On NFS, where Linux emulates `flock` with `fcntl` locks that a child does not inherit, the lock is freed when vidfunnel dies even while its ffmpeg runs on. This `flock` rule is verified on local file systems and bind mounts, and untested over NFS, SMB and sshfs; on SMB without the `nobrl` mount option a `flock` becomes a byte-range lock that other hosts may meet when they read the lock file, and a lock that cannot be read is judged by its age. A lock from another machine, or on a file system that refuses `flock` (`"flock": false`), is judged by the rules that follow, except one written on this machine before its last reboot: its machine id (from `/etc/machine-id`) is ours and its boot id is not, so no process of it still runs, and it is taken over at once with a message saying so, whatever its pid (a pid may have been reused since). A machine id that is itself the boot id (`machine_source` `boot-id`, where `/etc/machine-id` is missing) says nothing across a reboot and leaves the lock to the rules that follow; the container wrapper mounts the host's `/etc/machine-id` read-only so that container runs record it. Every such lock is respected until it is 24 hours old and then taken over with a warning; the pid can only shorten that wait. A lock left by a run on this machine (on the same side of the container boundary) whose pid no longer exists (it crashed or was killed) is taken over at once, with a message naming the pid and the lock's age. While its pid is still running the lock is respected, and the message says so, but after 24 hours the pid is taken for a reused one. A lock from another machine, or from a container run (whose host name is the container ID and whose pids are its own), a pid that is this run's own, or one that cannot be checked, is judged by age alone, with a message saying when it is held by a container run. The lock file is only ever created when none exists, and takeovers are serialised by a `flock` on the regular file `.vidfunnel/lock.takeover` (or on the directory), so of several runs that start together on one folder, one gets the lock and the others report it as held; on a file system that refuses `flock` on both, only the re-check of the lock's identity and the exclusive create guard the takeover, and two racers can both get in. A dead holder's pid is judged only when the lock was written on this machine (`/etc/machine-id`, else the kernel boot id, is recorded in the lock), since two machines on one share may carry the same host name. If a source changes after it was planned, its record is marked `source-changed` and the file is planned again under a new record ID, with the old one kept in its history.

Keeping the records in one hidden directory per folder, rather than a sidecar per video, keeps folders readable in file managers and media servers, and the state stays with the data when the tool is run from different machines over the same share.

Every MP4 the tool writes carries a `vidfunnel` metadata tag naming its record ID, the tool version, the first 16 hex digits of the source's SHA-256 and the output profile. The tag travels with the file through copies and renames, and `verify` checks it. An MP4 that already sits at the output name without this tag, or with a tag for another source, is not ours: `plan` quarantines the source as `output-collision` and writes nothing. The same happens when two sources in one folder share a stem (`movie.avi` and `movie.mkv`), since picking one of two versions of the same content is a human's job. When the source is itself an MP4, the output is written as `<stem>.vidfunnel.mp4` next to it, and `retire` renames it to `<stem>.mp4` once the original has been moved away; `plan --mark-outputs` does the same for every source, sidecars included. The record holds both names.

## Redaction

In a redacted report, every word that starts with a letter keeps that letter, every further letter becomes `_` and every further digit becomes `#`. A word of four or more characters also keeps its last character when that is a letter, so that two long names of the same length can still be told apart; a word of three characters or fewer keeps only its first. A word that starts with a digit is masked completely, and separators and the file extension are kept. So `Client Name - Deposition 2019-03-14 part2.avi` becomes `C____t N__e - D________n ####-##-## p___#.avi`, and `Wedding Day reception 2004-06-12 part 2.avi` becomes `W_____g D__ r_______n ####-##-## p__t #.avi`.

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
