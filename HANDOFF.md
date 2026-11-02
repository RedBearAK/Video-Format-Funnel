# Handoff — Video-Format-Funnel (package and CLI `vidfunnel`)

Version 20261102. Revision of the 20261101.5 draft after a consistency review (audio copy versus audio filters, level 4.1 versus field-rate HD, SAR and colour tags on remux, `-xerror`, output and record name collisions, which subcommands need ffmpeg, the redaction example, `dvdvideo` as optional); the points that could be tested were tested with ffmpeg 7.1.5 and the results are recorded in §9 and §10. Name decided: package, CLI and record directory are `vidfunnel`. Nothing built yet. Supersedes `handoffs/HANDOFF - Media archive conversion.md` (Oct 2026), which scoped a bash `media-convert` for the `photos` server. This is now a standalone Python project in its own public repo, run from a workstation (Linux or macOS) against any folder, including the `Media` share over the network. The earlier handoff's agreed points (remux first, verify before replace, Jellyfin layout) carry over.

## 1. Goal

Funnel every legacy video format that ffmpeg can decode (AVI with any codec, FLV, SWF-with-video, FLIC, MPEG-1/2 PS and VOB, WMV/ASF, old QuickTime MOV, RealMedia, MKV, and anything else ffprobe can read) into one output profile that:

- plays without transcoding on every non-obsolete device, OS, browser and media server (Apple devices and browsers included; Roku via Jellyfin direct play);
- is usually smaller than the source with no visible loss (second-generation encode at a transparent quality target);
- degrades gracefully under bit rot (closed GOP every 2 s, fragmented MP4, optional PAR2);
- is produced by a deterministic, auditable, resumable pipeline that records every decision and never deletes anything without an explicit, separate step.

Disk space is secondary to compatibility. AV1 and HEVC are not goals; H.264 is the output codec. Obsolete hardware (pre-2016 smart TVs, old Android `MediaPlayer`, QuickTime 7 era) is out of scope.

## 2. Output profile

### 2.1 Video

| Setting | Value | Reason |
|---|---|---|
| Encoder | `libx264` | Most decoded bitstream in existence; patents lapsing 2027–2030 |
| Profile / level | High, 4.1; 4.2 only for field-rate deinterlaced HD (§4.3) | 4.1 is the ceiling of every non-obsolete hardware decoder at 1080p30; 1080p50/60 needs 4.2 |
| Pixel format | `yuv420p`, 8-bit | No High10, no 4:2:2/4:4:4 — hardware decoders lack them |
| Quality | by source height: CRF 18 at ≤ 576p, 19 at 720p, 20 at 1080p; `-preset slower` | Transparent band for re-encoding lossy SD sources |
| GOP | closed, max 2 s: `-g 2*fps -keyint_min fps -x264-params open-gop=0` plus `-force_key_frames expr:gte(t,n_forced*2)`, where `fps` is the output frame rate (doubled after field-rate deinterlacing) | Bounds decode damage to ≤ 2 s; ~5–15 % size cost |
| Resolution | source | Never scale, except the pixel-art rule in §4.7 |
| Frame rate | source; VFR sources stay VFR | No padding to CFR |
| Aspect | signalled as SAR in both the H.264 VUI and the container, never resampled. Re-encode: `setsar` filter (x264 writes it into the VUI) plus `-aspect` for the container. Remux: `-bsf:v h264_metadata=sample_aspect_ratio=W/H` rewrites the VUI losslessly, plus `-aspect` for the container. `-aspect` alone only changes the container and is not enough on a remux | Reversible, zero cost |
| Colour tags | `-color_range tv` plus `smpte170m` (NTSC SD), `bt470bg` (PAL SD), `bt709` (HD) for colorspace/primaries; the trc is `smpte170m`, `bt709` and `bt709` respectively (the PAL transfer curve has no `bt470bg` name in ffmpeg, only `gamma28`, and players treat it as bt709 anyway); on a re-encode the tags go through `setparams` in the filter chain, because ffmpeg 7.1 drops the `-color_primaries`/`-color_trc` output options (tested); on a remux the same values go through `h264_metadata` (`colour_primaries`, `transfer_characteristics`, `matrix_coefficients`, `video_full_range_flag`), which only sets the fields it is given | Old sources are unflagged; players guess from frame size |
| Tune | none; `-tune animation` for cel/pixel content only | |

Deinterlacing and telecine handling: §4.3.

### 2.2 Audio

| Source | Action |
|---|---|
| AAC-LC | copy |
| MP3 | copy (MP3-in-MP4 is in the ISO spec and plays everywhere non-obsolete); `--audio-strict-aac` forces re-encode |
| AC-3, DTS, WMA, PCM, Vorbis, Opus, ADPCM, anything else | encode to AAC-LC, native `aac` encoder, 96 kb/s per channel (192k stereo, 448k 5.1 cap), source channel layout kept, source sample rate kept |

Opus and Vorbis were considered and rejected: Vorbis-in-MP4 is nonstandard, Opus-in-MP4 is not universal on Apple or in Jellyfin direct-play profiles. AAC-LC is the only choice that fits the compatibility goal. All audio streams are kept; language tags carried over.

"Copy" means `-c:a copy` with no audio filter at all; a filter graph forces a decode and re-encode. The sync rules in §4.4 therefore apply only to streams that are being re-encoded anyway. A copy-eligible stream (AAC-LC, MP3) that needs correction (drift between 0.1 % and 2 %, or timestamp gaps found in the probe pass) is re-encoded to AAC-LC at the rates above with the filters applied, flagged `audio-reencoded-for-sync`, so the record says why the stream was not copied. Within tolerance it is copied unchanged.

### 2.3 Subtitles and extra streams

- Text subtitles (SRT, SSA/ASS, MKV `subrip`): converted to `mov_text` inside the MP4 and also written as a `.srt` sidecar when styling would be lost (ASS).
- Bitmap subtitles (DVD `dvd_subtitle`, PGS): cannot live in MP4 and are never silently dropped. PGS goes to a `.sup` sidecar through ffmpeg's `sup` muxer. ffmpeg has no `.idx/.sub` muxer, so `dvd_subtitle` streams are written to a `NAME.subs.mkv` sidecar (Matroska carries VobSub natively; mpv and Jellyfin read it as an external track). Flag `subs-sidecar` and list the sidecars in the record.
- Chapters: carried over (`-map_chapters 0`).
- Attachments, fonts, cover art: dropped, listed in the report.

### 2.4 Container

Fragmented MP4 by default:

```
-movflags +frag_keyframe+empty_moov+default_base_moof+use_metadata_tags
```

Each fragment carries its own `moof` index, so truncation or localised damage leaves everything before it playable. This applies to remuxes as well as re-encodes: fragmenting costs nothing at remux time, so the default is the same for both. Fragments begin at keyframes, so a remuxed file inherits its source GOP length as its fragment length. `faststart` is not in the list: with `empty_moov` the `moov` is already at the front, and ffmpeg 7.1 accepts the combination silently, so the flag would only mislead a reader. `use_metadata_tags` is required for the identity tag in §6.7 (without it ffmpeg drops the unknown `vidfunnel` key; tested, §9). Seeking over HTTP (Jellyfin) in a file with no global index means the client scans `moof` boxes; test seek speed on the Roku and in a browser, and if it is poor add `+global_sidx` (one `sidx` index at the front; `plan` records whichever was used). `--plain-mp4` writes a conventional `+faststart+use_metadata_tags` file for any player that turns out not to like fMP4 (one known risk: Roku direct play through Jellyfin has not been tested with fMP4 — test one file before converting a library).

Metadata: `-map_metadata 0`, preserve `creation_time`, set output mtime to the source mtime.

Output name: the source directory and stem with `.mp4`. Three collision cases, all decided at `plan` and recorded:

- The output name already exists and the file is not ours (§6.7, no tag or a tag whose `src` does not match): flag `output-collision`, nothing is written, `convert` refuses.
- The source is itself `.mp4` (or `.m4v`), so the output name equals the source name: `convert` writes `<stem>.vidfunnel.mp4` and `retire` renames it to `<stem>.mp4` after the original has been moved away (§6.5); the record carries both names. Until retirement the two files sit side by side and the tag says which is which.
- Two sources in one folder share a stem (`movie.avi` and `movie.mkv`): both get `output-collision` and `hold`, nothing is written. Two versions of the same content need a human to pick one; the tool does not guess with a suffix.

### 2.5 Integrity sidecars

- Always: SHA-256 of source and output in the per-file record (§6).
- `--par2 N` (default off; suggested 10): `par2create -r N -n 1` next to the output. This is the real recovery mechanism; the GOP and fragment settings only limit how much a damaged file misbehaves on playback.

## 3. Remux versus re-encode

Decided per file from the probe.

Remux (`-c:v copy`) when the video stream is H.264, profile ≤ High, level ≤ 4.1, 8-bit 4:2:0, and not interlaced. Audio handled per §2.2 independently. Container rewrapped to fMP4. Keyframe density cannot be changed in a remux; `--reencode-video` forces an x264 pass anyway (use when resilience outranks avoiding a second generation). For remux candidates only, `plan` measures the source's actual keyframe spacing from the packet list (`ffprobe -show_packets -show_entries packet=pts_time,flags`, keyframes carry `K`; no decoding, so it is a demux pass) and records the maximum gap, so the report shows which remuxed files have long GOPs. Typical source spacing: x264/HandBrake defaults 250 frames (8–10 s) with extra I-frames at scene cuts; DivX/XviD 250–300 frames; web FLV/WMV anywhere from 2 s to 10 s+; DVD MPEG-2 at most 18 frames (0.6 s), so VOB-derived content is already dense and gets re-encoded anyway. FLV, MKV, MOV, MP4 and AVI carrying H.264 all qualify; this will be most MKVs.

Re-encode for everything else: MPEG-4 ASP (DivX/XviD, including packed B-frames), MPEG-1/2, Sorenson H.263, VP6, WMV7/8/9, VC-1, Indeo, Cinepak, MS Video 1, RealVideo, QuickTime Animation/Sorenson 3/Apple Intermediate, DV, FLIC, H.264 outside the profile limits, interlaced H.264.

HandBrake's "Web optimized" checkbox is `-movflags +faststart` (moov atom moved to the front). It has nothing to do with keyframes and is not fragmentation. Files HandBrake produced are ordinary H.264/AAC MP4 and will be remuxed, not re-encoded, unless `--reencode-video` is given.

## 4. Analysis rules

All analysis is sampled, not full-length, and every result is recorded with the sample positions so it can be reproduced. Default sampling: 12 windows of 3 s, evenly spaced, skipping the first and last 3 % of the duration (titles, credits, black). Files shorter than 60 s are analysed in full automatically (the windows would overlap). `--samples N` and `--full-analysis` override.

### 4.1 Decode check

`ffmpeg -v warning -i SRC -map 0:v? -map 0:a? -f null -` on the full file (this one is not sampled), stderr captured. The explicit maps decode every video and audio stream; without them ffmpeg decodes only one of each and damage in a second audio track goes unseen. Every distinct message class (the message with stream/frame numbers and timestamps normalised away) is recorded with its count. No `-xerror`: it aborts on the first hard error, which would hide every later class, and it does not fire at all on the concealed macroblock errors that make up most decode damage (tested: identical output with and without it on a file with three damaged regions). Streams ffmpeg cannot decode put the file in quarantine with reason `unsupported`, never in the conversion queue.

### 4.2 Aspect ratio

1. Read what the container and codec signal: MPEG-2 sequence header DAR, DV DAR, MOV `pasp`, MKV DisplayWidth/Height, AVI `vprp`. If codec and container disagree, the codec wins and the disagreement is logged.
2. If SAR is 1:1 and the frame size is a known non-square format — 720×480, 704×480, 352×480, 352×240, 480×480, 720×576, 704×576, 352×576, 352×288, 480×576 — the signalled aspect is almost certainly wrong. Candidates: 4:3 (SAR 8:9 NTSC, 16:15 PAL) and 16:9 (SAR 32:27 NTSC, 64:45 PAL), the MPEG-2 conventions that yield exact ratios.
3. Run `cropdetect=limit=24:round=2:reset=0` on every sample window separately. A bar result counts only if ≥ 75 % of windows agree within 2 px; otherwise "no bars". Never use the first-window result alone.
4. With bars: compute the active-area DAR under each candidate SAR; choose the candidate whose active area lands on a standard ratio (1.33, 1.78, 1.85, 2.35, 2.39) within 2 %. Apply as SAR metadata in the VUI and the container (§2.1: `setsar` on re-encode, `h264_metadata` on remux). Confidence penalty −15.
5. Without bars: 4:3 and 16:9 cannot be distinguished from pixels alone. Leave the aspect unchanged, flag `aspect-ambiguous`, confidence penalty −30. `--assume-dar 4:3|16:9` resolves it for a run or a folder.
6. Never crop. Bars are evidence, not a thing to remove (`--crop-bars` could be a later opt-in).

### 4.3 Interlace and cadence

`idet` over the sample windows, aggregated:

| Result | Action | Penalty |
|---|---|---|
| Progressive | none | 0 |
| Telecined (NTSC 3:2, hard or soft) | `fieldmatch,decimate` → 23.976p | −10 |
| Interlaced (TFF/BFF) | `bwdif=mode=1` at field rate (59.94/50 fps) | −5 |
| Mixed or inconsistent across windows | treat as interlaced, flag `cadence-mixed` | −40 |

Field-rate deinterlacing keeps all motion; frame-rate deinterlacing would discard half the temporal information, which is visible on camcorder and sports footage. SD at field rate (480p59.94, 576p50) is far inside level 4.1. HD at field rate (1080p50/59.94) is not: 4.1 tops out at 1080p30, so interlaced HD sources (1080i broadcast captures, HDV, AVCHD) are encoded at level 4.2, which every 1080p60-capable hardware decoder of the last decade accepts, flagged `level-4.2` so the report shows them. `--max-level 4.1` forces frame-rate deinterlacing (`bwdif=mode=0`) for those files instead, for a library that must stay at 4.1. DVD "judder" from 3:2 pulldown is the telecined case and is handled; judder from PAL speed-up or from a bad transfer is not detectable or fixable and is out of scope. Mixed-cadence DVDs (film body with 29.97i menus, credits or inserts) are flagged for a human; `decimate` on those would drop real frames.

Interlaced output is never written (bad on Apple and in browsers).

### 4.4 Audio/video sync

- `-fflags +genpts` always; `-avoid_negative_ts make_zero`, which shifts all streams by the same amount so the earliest starts at zero and the relative offsets between streams are kept.
- Relative stream start-time offsets from ffprobe are preserved, not zeroed. An offset delta > 100 ms between video and first audio is recorded and flagged `sync-offset`; `--audio-offset SECONDS` overrides.
- Drift is modelled as a ratio: audio duration (samples ÷ rate) over video duration (frames ÷ rate, or last PTS for VFR). Ratio within 0.1 %: nothing. Between 0.1 % and 2 %: the audio is stretched to the video with `atempo=RATIO` (inaudible at these magnitudes; pitch change is below 0.3 semitones), flag `sync-drift` with the ratio recorded, penalty −20. Beyond 2 %: not corrected, flag `sync-drift-large`, `hold`. `aresample=async=1` is additionally applied to every re-encoded audio stream (harmless when timestamps are sane) so that timestamp gaps in FLV/WMV/MPEG-PS are closed. Both filters imply a re-encode; what that means for copy-eligible AAC and MP3 is in §2.2.
- Why audio is the one stretched: the file alone cannot say whether the audio clock or the video clock lied; stretching audio by < 2 % is inaudible, while re-timing video frames turns CFR into VFR and is visible on motion.
- Demuxer warnings (`non-monotonic DTS`, `timestamp discontinuity`, `pts < dts`, AVI `dwInitialFrames`, VBR-MP3-in-AVI) are captured from the probe pass and listed in the record; each class is −10.
- AVI zero-length (dropped) frames and FLV/WMV VFR are handled by keeping VFR output.
- Not detectable from the file: clustered frame drops with a CFR container, and a constant offset that is simply wrong. These surface only as low confidence; the content-aware check (lip/voice correlation, e.g. SyncNet-class models) is out of scope.

### 4.4a Subtitle sidecars

Embedded subtitles are assumed in sync with their own video. External `.srt`/`.ass` sidecars often come from another cut or frame rate (23.976 vs 25 = 4.1 % drift, or a different intro). Optional `--subsync`: if `ffsubsync` or `alass` is on PATH, align the sidecar against the output's audio (voice-activity correlation; both tools solve offset and frame-rate ratio, `alass` also handles splits), write the aligned copy as `NAME.synced.srt`, keep the original, record the tool's score, flag `subsync-applied`. Nothing is overwritten and the sidecar is never embedded into the MP4 unless `--embed-subs` is also given.

### 4.5 Colour

Tag, never convert. SD (height ≤ 576): the PAL family (576 or 288 lines, or a 25/50 fps frame rate) → `bt470bg`; everything else (480/486, 240, and odd web sizes) → `smpte170m`. HD → `bt709`. Sources that already carry explicit tags keep them. Written by the encoder flags on re-encode and by `h264_metadata` on remux (§2.1).

### 4.6 VOB and DVD structures

- A `VIDEO_TS` directory or a `VTS_xx_0.IFO` is read with ffmpeg's `dvdvideo` demuxer (ffmpeg ≥ 7, needs libdvdread) per title, which handles cell boundaries and PTS wrap. Output one file per title; menus skipped.
- Loose `VTS_xx_N.VOB` fragments with no IFO are concatenated per title set and read as MPEG-PS with `+genpts`. Flag `vob-no-ifo`, penalty −10.
- `dvdvideo` is optional (§8); distro builds often lack it (the Tumbleweed Packman ffmpeg 7.1.5 used for this revision does). When it is missing, a `VIDEO_TS` tree takes the concatenation path above with flag `dvd-no-demuxer`, penalty −10, and `check` says how to get the demuxer.
- Multiple audio tracks and subtitle streams kept per §2.2/§2.3.

### 4.7 Synthetic and palette content

FLIC, QuickTime Animation, 8-bit palette AVI, screen recordings with flat colour: 4:2:0 subsampling smears coloured edges at native size. If the source height is ≤ 300 px and the codec is palette/RLE class, integer-upscale with `scale=iw*2:ih*2:flags=neighbor` (3× below 160 px) before encoding, and use `-tune animation`. Recorded as `pixelart-upscale`. Files stay tiny.

### 4.8 SWF

ffprobe the file. If it reports a video stream (`flv1`, `vp6f`, `h264`) it is a video container and goes through normally. If it reports none, it is a vector/ActionScript animation; there is no decode, only rendering, and the only maintained renderer (Ruffle) has no reliable headless export with audio. Quarantine as `swf-vector`, original kept, listed in the report. Revisit if Ruffle's exporter matures.

## 5. Confidence score

Each converted file gets a score from 100 minus the penalties above, plus post-conversion checks:

| Check | Penalty |
|---|---|
| Any decoder warning during the probe pass | −10 per class |
| Output duration differs from source by > 0.1 s | −20 |
| Frame count outside the expected range (after telecine/field-rate adjustment) | −30 |
| Output of a re-encode larger than source (kept only with `--keep-larger`) | not eligible for retirement; a remux is exempt, since only the container changed and fragment headers can make a small file grow slightly (flag `remux-larger`, informational) |
| Output stream count differs from the expected (a video, audio or text-subtitle stream of the source is missing) | −50 |
| Identity tag missing or naming another record or source | −100 |
| Optional sampled SSIM against source below threshold (`--ssim-check`, default off, threshold 0.97 on luma) | −25 |
| Quarantined for any reason | 0 |

Bands: ≥ 90 `ok`, 60–89 `review`, < 60 `hold`. The report sorts ascending so the worst conversions are at the top for hand checking. `retire` (§6.5) refuses `hold` and, without `--allow-review`, refuses `review`.

## 6. Pipeline and CLI

Every stage is a separate subcommand, idempotent, and resumable. State lives in per-folder records (§6.7); an optional central index is a cache, never the source of truth.

```
vidfunnel check
vidfunnel plan   [--samples N] [--assume-dar R] [--reencode-video] PATH...
vidfunnel convert [--par2 N] [--plain-mp4] [--jobs N] [--dry-run] PATH...
vidfunnel verify PATH...
vidfunnel report [--format md|json|csv] [--min-score N] [--reveal] [--with-logs] PATH...
vidfunnel report --from REPORT [--reveal]
vidfunnel find ID
vidfunnel retire [--allow-review] [--quarantine DIR] PATH...
vidfunnel retire --destroy-originals-now [--allow-review] PATH...
vidfunnel index [--rebuild] [--db PATH] PATH...
vidfunnel --install-command [--user|--system]
vidfunnel --uninstall-command
vidfunnel --version
```

### 6.1 `check`

Dependency table for this machine: ffmpeg/ffprobe version, `libx264`, native `aac`, `dvdvideo` demuxer, `libdvdread`, `par2`, filters `idet`/`bwdif`/`fieldmatch`/`cropdetect`, bitstream filter `h264_metadata`; each marked required or optional. Prints the exact install command for the detected platform (`/etc/os-release`: zypper/dnf/apt/pacman; macOS: Homebrew). `plan`, `convert` and `verify` run the same check first and refuse with the same message if something required is missing. `report`, `find`, `index` and `retire` work from records and hashes alone and run without ffmpeg, so a redacted report can be read and resolved on any machine.

### 6.2 `plan`

Probe + analysis (§4), writes the JSON record with the full decision list, the reasons, the sample positions, and the exact ffmpeg command that `convert` will run. Prints a summary table: action (remux / re-encode / quarantine), flags, predicted confidence, source size. `plan` never writes media. Re-running `plan` on a planned file is a no-op unless `--replan`.

### 6.3 `convert`

Runs the planned command to a temp file in the same directory (`.NAME.mp4.part`), renames into place on success, sets mtime, writes PAR2 if asked, then runs `verify` on the result. `--jobs N` parallelises at the file level; x264 already uses all cores for one file, so the default is 1 and `--jobs` is for remux-heavy sets. `--dry-run` prints the commands only. A file whose record says `converted` or `verified` and whose output still matches its cheap fingerprint (size, mtime, first and last MiB, §6.7) is skipped; a failed run leaves the record at `failed` with the ffmpeg stderr, and the `.part` file is removed.

### 6.4 `verify`

Full-length null decode of the output with error counting, duration and frame-count comparison, stream-count comparison, optional SSIM sampling, size ratio. Updates the record and the score. Can be re-run at any time (e.g. after moving files).

### 6.5 `retire`

The only stage that touches originals, and it is separate from `convert` by design. For each source it checks: a record exists, status is `verified`, the source still matches the record's cheap fingerprint (it has not been replaced since it was planned), output exists and its full SHA-256 matches the record, score meets the band rule, output size rule satisfied. Only then it moves the original into the quarantine directory (default `.vidfunnel-retired/` under the top-level path given, preserving relative paths), and, for an output written under the interim `<stem>.vidfunnel.mp4` name (§2.4), renames it to `<stem>.mp4` once the original is out of the way, recording the rename. `--destroy-originals-now` unlinks instead of moving and is the only destructive flag in the tool; it prints what it is about to do and requires typing `destroy` on stdin unless `--yes` is also given. No other subcommand ever removes or overwrites a source.

### 6.6 `report`

Reads the records and prints a sorted table: record ID, score, status, action, flags, source → output size, path. Markdown by default (paste into Obsidian), JSON and CSV for processing. Names are redacted by default (§6.8); `--reveal` shows them. Reports are generated from records only, so a report is cheap and never triggers probing or encoding.

Every report starts with a header: report ID, generated-at time, tool version, the exact invocation (paths, filters, format), and a hint line reading, in substance, "Names in this report are redacted. To see them, re-run the same command with --reveal, or `vidfunnel report --from <this file> --reveal`. To locate one row: `vidfunnel find <ID>`." `--from REPORT` reads the invocation out of an existing report's header and re-runs it with the same arguments plus whatever is added, so a redacted report that was sent out can be reissued revealed without reconstructing the command. Rows are matched by record ID; if a record's status has advanced since the original report, the row carries a `changed-since` marker rather than silently showing newer data.

### 6.7 Records, identity and "have I seen this"

Three layers, one source of truth:

1. **The output file identifies itself.** Every file the tool writes (remux or re-encode) carries a freeform metadata tag written with `-movflags +use_metadata_tags`:

   ```
   vidfunnel={"id":"<record id>","tool":"vidfunnel","ver":"20261101","src":"<first 16 hex of source SHA-256>","profile":"h264-high41-fmp4"}
   ```

   The tag travels with the file through copies, renames and moves. An MP4 next to a source with the same base name but without this tag (or with a tag whose `src` does not match the source) is *not ours*: `plan` reports `output-collision` and does nothing; `convert` refuses. There is no `--adopt`; a conversion the tool did not make and cannot verify is not trusted. Round-trip of the tag through ffprobe, and that the standard `title` tag still shows in QuickTime/Jellyfin alongside it, is a test (§9).

2. **Per-folder records are the audit trail and the authority.** Each folder processed gets a `.vidfunnel/` directory holding one JSON record per *source* file (named after the full source file name including its extension, `movie.avi.json`, so `movie.avi` and `movie.mkv` never share a record; quarantined and unsupported sources have records too, not only converted ones), plus `folder.json` (last scan time, tool version, inventory of source names seen). A record holds: record ID (random UUID, assigned once at `plan`), source path relative to the folder, source fingerprint (size, mtime, SHA-256 of the first and last 1 MiB — cheap, computed on every `plan`), full source SHA-256 (computed once, reused), every probe result and decision with its reason and sample positions, the exact ffmpeg argv, output name (and the interim name when §2.4 applies), output fingerprint and SHA-256, verify results, score, status (`planned` / `converted` / `verified` / `quarantined` / `failed` / `retired` / `source-changed`; the `ok`/`review`/`hold` bands of §5 are a property of the score, not a status, and the quarantine reason such as `unsupported` or `swf-vector` is a field of its own), and timestamps per stage. Records are append-only per stage: a stage adds its section, never rewrites an earlier one.

   "Have I looked at this?" is answered from here: `plan` walks the tree, and for each source with a record whose cheap fingerprint still matches, skips it (status shown in the summary); if the fingerprint changed, the record is marked `source-changed` and the file is re-planned under a new record ID with the old one kept in `history`. A folder with no `.vidfunnel/` has never been scanned. Records survive the tool being run from different machines over the same share, because they sit with the data, and two machines running at once on the same folder is prevented by a `.vidfunnel/lock` file with host, pid and start time (stale after 24 h with a warning).

   Why not sidecars next to each file: a flat folder with one `.json` per video doubles the entry count in Dolphin, Finder and Jellyfin, and loose sidecars get separated from their videos. One hidden directory per folder keeps the folder readable and keeps all the state in one place to copy, inspect or delete.

3. **Central index: optional, derived, disposable.** `vidfunnel index PATH...` walks the given trees and loads every `.vidfunnel/*.json` into a SQLite file (`~/.local/state/vidfunnel/index.sqlite` by default; `sqlite3` is in the standard library). `report` and `find` use it when present for cross-folder queries and speed; `--rebuild` drops and recreates it. Nothing is ever written to the index that is not also in a record, and deleting the index loses nothing. It is per machine and per user, which is why it cannot be authoritative for a share used from several machines.

`vidfunnel find ID` resolves a record ID (or any unique prefix) to the real path, status and output, which is how a redacted report (§6.8) is mapped back to files locally.

### 6.8 Redacted reports

Redaction is the default for `report` output; `--reveal` is the opt-in. Records on disk are never redacted. The default produces a report that can be shared for troubleshooting without revealing file names, subject matter or directory structure, while keeping enough shape that the user can match rows to files by eye and resolve any row exactly with `find ID`.

Redaction rules, applied as a pure function of the string (deterministic, no salt, no state):

- Every word in a file name, directory component, or metadata string that starts with a letter keeps that first letter; every further letter becomes `_` and every further digit becomes `#`. A word that starts with a digit is masked completely (a leading `2` in a year or a `0` in a month carries no shape worth keeping). Word length is preserved. Separators (space, `_`, `-`, `.`, parentheses, brackets) are kept as they are. The file extension is kept. Non-ASCII first characters are kept as one character. Example: `Client Name - Deposition 2019-03-14 part2.avi` → `C_____ N___ - D_________ ####-##-## p___#.avi`.
- Paths: every directory component is redacted the same way; the number of components and the separator are kept. The top-level path given on the command line is replaced by `<root>`.
- Metadata: `title`, `artist`, `album`, `comment`, `description`, `show`, `episode_id`, chapter titles, `handler_name` and any other free-text tag in the probe output are redacted. Codec names, stream parameters, dimensions, durations, bit rates, frame rates, timestamps, flags, scores, the ffmpeg argv and ffmpeg stderr are kept, because they are what troubleshooting needs. Inside the argv and stderr, every occurrence of the real source path, output path, and any redacted metadata value is replaced by its redacted form before output, so an ffmpeg error line cannot leak a name.
- Dates: `creation_time` and file mtimes are kept by default; `--redact-dates` truncates them to the year.
- The record ID is printed on every row unredacted; it is random and reveals nothing.

`--with-logs` adds the run log and the ffmpeg stderr for each file to the report (redacted like everything else unless `--reveal` is given), which is the bundle to post when asking why a file misbehaved. The redaction code lives in one module (`redact.py`) with a table-driven test of inputs and expected outputs, so a reviewer can confirm what it does without reading the rest of the tool. Redaction never touches the files or the records; it is applied to report output only, and `--reveal` on the same invocation gives the identical report with names.

### 6.9 `--install-command`

The repo's `bin/vidfunnel` is the one launcher: a POSIX `sh` script that resolves its own real location (a readlink loop, since `readlink -f` is not on older macOS), checks for a `python3` ≥ 3.9 on PATH (printing the distro or Homebrew install command if not), and runs `exec env PYTHONPATH="<repo root>" python3 -m vidfunnel "$@"`. The package itself never edits `sys.path`; the launcher sets the environment, which is the only place that belongs.

`vidfunnel --install-command` symlinks that launcher into a bin directory:

- `--user` (default): `~/.local/bin/vidfunnel`. If `~/.local/bin` is not on PATH in the user's login shell, the tool offers to append one guarded line to the right rc file — `~/.bashrc` for bash, `~/.zprofile` for zsh (macOS default), `fish_add_path` in `~/.config/fish/config.fish` — bracketed by a `# vidfunnel` marker so it is idempotent and `--uninstall-command` can remove exactly that line. Nothing is edited without printing the file and the line first and getting a yes; `--yes` skips the prompt for scripted installs. It then says which new shell is needed to see the change.
- `--system`: `/usr/local/bin/vidfunnel`, with `sudo` on Linux; on macOS the admin user usually owns `/usr/local/bin` and no `sudo` is needed. Apple Silicon Homebrew lives in `/opt/homebrew/bin`, which is never used.
- Both: refuse to overwrite a file that is not a symlink to this repo's launcher; report what is there instead.
- A dangling symlink after the repo is moved makes the command fail with a shell error, which the README documents; re-running `--install-command` from the new location fixes it.

`--uninstall-command` removes the symlink and the marked PATH line, nothing else. `install.sh` in the repo is a thin wrapper that calls the launcher's `--install-command` so a fresh clone works the same way.

### 6.10 Progress and feedback

Standing rule: nothing the tool does that may take more than one second of wall-clock time runs silently. A user should never have to wonder whether it is working.

- Before any operation that may take a second or more, print one line saying what is about to happen and to which file (`[plan 12/340] probing C_____ N___.avi`), redacted the same way as reports when the terminal output is being captured with `--log FILE`, revealed on the terminal.
- Long operations show live progress. ffmpeg is run with `-nostats -progress pipe:1`, which emits `out_time`, `frame`, `fps` and `speed` every half second; these render as one updating line: `[re-encode] 00:12:34 / 01:32:10  13.6 %  212 fps  8.1x  eta 00:09:48`. Hashing and copying report bytes and MB/s the same way. Sampled analysis shows the window counter (`idet window 7/12`). Operations with no measurable length show an elapsed-time counter, not a spinner.
- On a TTY the line updates in place (`\r`, no ANSI beyond that, `NO_COLOR` respected, plain ASCII). When stdout is not a TTY (log file, CI, `--jobs` with interleaved output) the same information is written as a new line at most every 30 seconds, so logs stay readable and nothing is lost.
- `--quiet` keeps per-file start lines and errors only; `--verbose` adds the ffmpeg argv before each run. There is no mode that is silent while working. The `--log FILE` option described above is not implemented in the first build (open question 8).
- Each stage ends with a one-line summary: counts per outcome, total time, and where the records went.
- Implementation in one module, `progress.py`, used by every other stage; no third-party packages.

## 7. Architecture and repo layout

Python ≥ 3.9, standard library only (subprocess, json, argparse, pathlib, concurrent.futures). No third-party packages, so it runs from a clone with the system Python on Fedora, Tumbleweed, Debian and macOS. ffmpeg/ffprobe are external and discovered on PATH (`--ffmpeg PATH` override points at the ffmpeg binary or wrapper; ffprobe is looked up next to it under the same name pattern, so a pair of `distrobox-export` wrappers in a directory of their own works, and the host's ffmpeg is never shadowed).

Each module is named for the single stage it implements and declares `VERSION` at the top; the CLI's `--version` lists the package version and every module's version.

Imports are absolute and package-qualified everywhere (`from vidfunnel.probe import probe_file`, never `from .probe import …` or bare `import probe`). The package is runnable as `python3 -m vidfunnel` and from the `bin/` launcher, and `tests/` imports it the same way; nothing may depend on the current directory or on `sys.path` edits.

```
vidfunnel/
  vidfunnel/
    __init__.py
    __main__.py
    cli.py          argparse, subcommand dispatch, exit codes
    deps.py         `check`: tool discovery, platform detection, install hints
    probe.py        ffprobe wrapper → normalised stream model
    analyze.py      sampling, idet, cropdetect, aspect, sync, colour rules (§4)
    plan.py         decisions → ffmpeg argv, record creation
    encode.py       `convert`: temp file, rename, mtime, par2, job pool
    verify.py       post-conversion checks, scoring (§5)
    retire.py       quarantine / destroy, with all guards (§6.5)
    report.py       table rendering: md/json/csv
    record.py       JSON record schema, load/save, fingerprints, hashing, folder lock
    index.py        optional SQLite index built from records (§6.7)
    redact.py       report redaction (§6.8)
    ffmpeg.py       argv builder, subprocess runner, `-progress` parsing, version parsing
    progress.py     terminal/non-TTY progress rendering (§6.10)
    installer.py    `--install-command` / `--uninstall-command` (§6.9)
  bin/vidfunnel     POSIX sh launcher (§6.9); the only thing that sets PYTHONPATH
  bin/vidfunnel-setup-distrobox  creates/removes the Alpine distrobox and its tool wrappers (§8)
  tests/
    fixtures/       generated by tests/make_fixtures.sh, not committed
    data/           the few small real files that cannot be synthesised (a `.fli`), committed; `.gitignore` excepts them
    test_*.py
  install.sh        wrapper around bin/vidfunnel --install-command
  Containerfile     Alpine + python3 + ffmpeg + par2 + ffsubsync, for hosts without Python (§8)
  bin/vidfunnel-podman  wrapper that runs the tool in that container against a bind-mounted folder
  README.md
  LICENSE
```

The name is decided: package, CLI, launcher and record directory are all `vidfunnel` (repo `Video-Format-Funnel`). The per-module header convention follows §11.

## 8. Dependencies and install hints

Required: ffmpeg ≥ 6 built with libx264 and the native `aac` encoder, with the `h264_metadata` bitstream filter and the `idet`/`bwdif`/`fieldmatch`/`cropdetect` filters (all present in any normal build). Optional: ffmpeg ≥ 7 with libdvdread for the `dvdvideo` demuxer (IFO-driven DVD input, §4.6; without it DVD trees take the VOB concatenation path), `par2`, `mediainfo` (richer probe for odd AVI/MOV), `ffsubsync` or `alass` (subtitle sidecar alignment), `ruffle` (future SWF rendering).

What `check` prints per platform:

- Fedora: base `ffmpeg-free` has no x264. Enable RPM Fusion free and `dnf swap ffmpeg-free ffmpeg --allowerasing`. RPM Fusion's ffmpeg 7.1.5 is built without libdvdread (tested 2026-11-02, Fedora 43), so `dvdvideo` is unavailable; for IFO-driven DVD input use the container or an Alpine distrobox (below).
- openSUSE: add Packman and `zypper dup --from packman --allow-vendor-change`, then `zypper install ffmpeg par2cmdline`. Packman's ffmpeg 7.1.5 is also built without libdvdread (same note as Fedora).
- Debian/Ubuntu: `apt install ffmpeg par2`. Debian trixie's ffmpeg 7.1.5 is built with libdvdread and libdvdnav and has `dvdvideo` (tested 2026-11-02).
- Arch: `pacman -S ffmpeg par2cmdline`. Arch's ffmpeg 9.0.2 has `dvdvideo` (tested 2026-11-02).
- Alpine: `apk add ffmpeg par2cmdline python3`. Alpine's ffmpeg 8.1.3 has `dvdvideo`, libx264, `aac`, `h264_metadata` and all the filters (tested 2026-11-02); the base image is 9 MB. This is the container base and the recommended distrobox for workstations whose distro ffmpeg lacks `dvdvideo`.
- macOS: `brew install ffmpeg par2`. Homebrew's build flags not yet checked for libdvdread.

Which builds have `dvdvideo` is a matter of distro build flags, not ffmpeg version, which is why `check` tests for the demuxer itself rather than inferring it from the version string.

Nobody packages `ffsubsync` or `alass` (Debian trixie, Arch official, Alpine and Fedora 43 searched 2026-11-02). `ffsubsync` installs with pip into a venv (0.5.1 works on Debian trixie and Alpine, tested); `alass` ships glibc binaries on GitHub, which run on Debian and Arch but not on Alpine without `cargo install alass-cli` or gcompat (not tested).

Workstation distrobox (the way the openSUSE iMac gets `dvdvideo`): `bin/vidfunnel-setup-distrobox` creates an Alpine distrobox (`vidfunnel-alp`, image `alpine:3.24`) with `ffmpeg`, `par2cmdline`, `python3`, `mediainfo` and `py3-pip`, installs `ffsubsync` into a venv in the shared home (`~/.cache/ffsubsync-venv`), exports `ffmpeg`, `ffprobe` and `ffsubsync` through `distrobox-export --bin` into a directory of their own (`~/.local/share/vidfunnel-tools/bin`), never into a PATH directory, verifies the result from the host (libx264, `aac`, `dvdvideo`, `h264_metadata`, par2, mediainfo, ffsubsync) and prints the `--ffmpeg` line to use. The host's ffmpeg is never shadowed. The script is idempotent, refuses to overwrite a wrapper that is not its own, and `--remove` takes away exactly what it created (box, wrappers, venv), leaving the pulled image in the engine's cache. Tested 2026-11-02 on the iMac: create, re-run, remove.

uCore / immutable hosts without Python (the `photos` server, Ryzen 5600, which is where most converting will happen): run the tool in a container. The repo ships a `Containerfile` (Alpine + `python3`, `ffmpeg`, `par2cmdline`, and `ffsubsync` installed with pip into a venv inside the image; the build checks `-encoders` for `libx264` and `-demuxers` for `dvdvideo` and fails loudly if either is missing, so a failure means the base image changed) and a `vidfunnel-podman` wrapper that bind-mounts the folder given on the command line read-write at the same path inside the container, runs as the invoking uid/gid (`--userns=keep-id`) so records and outputs are owned correctly, and passes `nice`/`ionice` through. Debian trixie slim is the fallback base if something ever needs glibc (the `alass` binary, for instance); its ffmpeg 7.1.5 also has `dvdvideo`. Records live beside the data (§6.7), so a tree worked on from the container and from a workstation stays consistent. The image is pinned by digest in the wrapper and rebuilt deliberately, matching the project's container-update conventions. This is a v1 target.

## 9. Testing

- Fixtures are synthesised with ffmpeg itself, which can encode most of the legacy formats: MPEG-4 ASP in AVI (`-c:v mpeg4 -vtag xvid`), FLV1 in FLV and SWF, WMV2 in ASF, MPEG-2 in MPEG-PS with 4:3/16:9 DAR and with interlaced flags, DV, 8-bit palette video, H.264 in MKV/FLV/AVI, MP3/AC-3/PCM audio variants, a file with a deliberate 300 ms audio offset, a VFR file, a letterboxed 720×480 file with SAR 1:1. Telecine fixture via `telecine` filter. FLIC has no ffmpeg encoder; keep one real small `.fli` under `tests/data/` if licence allows, else skip. Also: an H.264 source that is already `.mp4` (interim-name path, §2.4), a `movie.avi`/`movie.mkv` pair (collision hold), an AAC source with 1 % drift (`audio-reencoded-for-sync`) and one within tolerance (must be copied: output audio packets byte-identical to the source), a 1080i fixture (level 4.2 and the `--max-level 4.1` path).
- Unit tests for every rule in §4 and §5 against probe JSON captured from the fixtures (no ffmpeg needed for those).
- Integration tests run `plan → convert → verify → retire --quarantine` on the fixture set and assert the records and the quarantine moves. `retire --destroy-originals-now` is tested only against copies in a temp dir.
- Metadata tag round trip: the `vidfunnel` tag survives remux and re-encode, reads back through ffprobe, and the standard `title` still displays in QuickTime Player, mpv and Jellyfin. Checked with ffmpeg 7.1.5 on 2026-11-02: with `+use_metadata_tags` both `vidfunnel` and `title` read back through `ffprobe -show_format` on an `empty_moov` fMP4; without the flag the `vidfunnel` key is dropped silently and `title` survives. Player display remains to be checked.
- fMP4 probing: `ffprobe` reports a correct `format.duration` and stream duration on an `empty_moov` file (tested), but `nb_frames` is `N/A`; `verify` counts frames with `-count_packets`/`-count_frames`, never from `nb_frames`.
- Remux metadata: `h264_metadata` with SAR and colour fields plus `-aspect` on a `-c:v copy` remux reads back as the intended SAR, DAR and colour tags (tested).
- Redaction: table-driven tests for `redact.py` (names, paths, metadata, argv and stderr substitution, non-ASCII, `--redact-dates`).
- Corruption test: flip bytes in the middle of an fMP4 output and confirm it still decodes before and after the damage; do the same to a `--plain-mp4` output for the comparison the README will cite.
- GitHub Actions: tests on ubuntu-latest with distro ffmpeg; macOS job with Homebrew ffmpeg; a job that builds the `Containerfile` and runs the integration tests inside it.

## 10. Open questions

1. Roku direct play of fragmented MP4 through Jellyfin: untested. If it fails, default stays fMP4 and the Jellyfin library gets `--plain-mp4`, or Jellyfin is allowed to remux.
2. Whether to offer MKV output as an alternative container for collections that never touch Apple devices or browsers (better in-container resilience, Opus audio possible). Not in v1.
3. Aspect without bars (§4.2.5): a per-folder hint file (`.vidfunnel-hints`) may be more practical than a CLI flag for mixed folders.
4. Denoise opt-in (`hqdn3d`) for VHS captures whose re-encode comes out larger than the source. Not in v1; the size guard covers the safety side.
5. Resolved 2026-11-02: project name is repo `Video-Format-Funnel`, package, CLI and record directory `vidfunnel`.
6. Partly resolved 2026-11-02: with `use_metadata_tags` the standard `title` still reads back through ffprobe next to the `vidfunnel` key (§9). Whether QuickTime Player and Jellyfin display `title` from an `mdta`-keyed file is still to be checked on a real player; if not, fall back to a `comment` tag with the same JSON payload.
8. `--log FILE` (a redacted copy of the terminal output) is specified in §6.10 but not built; the CLI does not offer the option until it works. `--subsync` and `--embed-subs` (§4.4a) are likewise not built.
7. Level 4.2 for field-rate deinterlaced HD (§4.3) is a judgement call made in this revision: it keeps the motion the field-rate rule exists for, at the cost of the single-level promise. `--max-level 4.1` is the escape hatch. Revisit if a target device turns out to reject 4.2.

## 11. Conventions (the author's)

- Versions are date-based: `VERSION = "20261101"`, same-day patch `20261101.1`. `--version` prints them.
- Every script and source file ends with a blank line and `# End of File #` (language-appropriate comment syntax).
- No comments inside shell command blocks in documentation; explanations in prose.
- README and docs are plain Markdown, no YAML frontmatter.
- Minimal and auditable over clever: no daemon, no authoritative database (the SQLite index is a rebuildable cache), no third-party Python packages, every decision written down with its reason, nothing destructive outside `retire`.
- Report errors directly and specifically; never claim success without the verification pass.

# End of File #
