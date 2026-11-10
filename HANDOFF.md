# Handoff — Video-Format-Funnel (package and CLI `vidfunnel`)

Version 20261102. Revision of the 20261101.5 draft after a consistency review (audio copy versus audio filters, level 4.1 versus field-rate HD, SAR and colour tags on remux, `-xerror`, output and record name collisions, which subcommands need ffmpeg, the redaction example, `dvdvideo` as optional); the points that could be tested were tested with ffmpeg 7.1.5 and the results are recorded in §9 and §10. Name decided: package, CLI and record directory are `vidfunnel`. Built on 2026-11-02 (every stage, tests, container files, README); the text below is the specification the code follows, with the deviations noted inline and in §10. Supersedes `handoffs/HANDOFF - Media archive conversion.md` (Oct 2026), which scoped a bash `media-convert` for the `photos` server. This is now a standalone Python project in its own public repo, run from a workstation (Linux or macOS) against any folder, including the `Media` share over the network. The earlier handoff's agreed points (remux first, verify before replace, Jellyfin layout) carry over.

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

`convert --refit` (§6.3) may raise the CRF of a re-encode that came out larger than its source, capped at 23 for sources up to 576 lines and 24 above; the caps are a chosen quality floor (the transparent band stays 18 to 20), past which the loss is judged not worth the space and the existing output is kept instead.

### 2.2 Audio

| Source | Action |
|---|---|
| AAC-LC | copy |
| MP3 | copy (MP3-in-MP4 is in the ISO spec and plays everywhere non-obsolete); `--audio-strict-aac` forces re-encode |
| AC-3, DTS, WMA, PCM, Vorbis, Opus, ADPCM, anything else | encode to AAC-LC, native `aac` encoder, 96 kb/s per channel (192k stereo, 448k 5.1 cap), source channel layout kept, source sample rate kept |

Opus and Vorbis were considered and rejected: Vorbis-in-MP4 is nonstandard, Opus-in-MP4 is not universal on Apple or in Jellyfin direct-play profiles. AAC-LC is the only choice that fits the compatibility goal. All audio streams are kept; language tags carried over.

"Copy" means `-c:a copy` with no audio filter at all; a filter graph forces a decode and re-encode. The sync rules in §4.4 therefore apply only to streams that are being re-encoded anyway. A copy-eligible stream (AAC-LC, MP3) that needs correction (drift between 0.1 % and 2 %, or timestamp gaps found in the probe pass) is re-encoded to AAC-LC at the rates above with the filters applied, flagged `audio-reencoded-for-sync`, so the record says why the stream was not copied. Within tolerance it is copied unchanged.

### 2.3 Subtitles and extra streams

- Text subtitles (SRT, SSA/ASS, MKV `subrip`): converted to `mov_text` inside the MP4 and always also written as a `.srt` sidecar (`NAME.srt`, or `NAME.<n>.srt` with several text streams): an external `.srt` is what Roku Media Player and most TV players read, embedded `mov_text` is not shown everywhere, and ASS styling is lost in `mov_text` anyway. An `.srt` that already exists beside the source and is not the tool's own (not named in the source's earlier records) is left alone and not written, flag `sidecar-exists`; the subtitles are still inside the MP4. A `plan --replan` after a convert recognises the tool's own sidecars and overwrites them.
- Bitmap subtitles (DVD `dvd_subtitle`, PGS): cannot live in MP4 and are never silently dropped. PGS goes to a `.sup` sidecar through ffmpeg's `sup` muxer. ffmpeg has no `.idx/.sub` muxer, so `dvd_subtitle` streams are written to a `NAME.subs.mkv` sidecar (Matroska carries VobSub natively; mpv and Jellyfin read it as an external track). Flag `subs-sidecar` and list the sidecars in the record.
- Chapters: carried over (`-map_chapters 0`).
- Attachments, fonts, cover art: dropped; they stay listed in the record's probe section.

### 2.4 Container

Fragmented MP4 by default:

```
-movflags +frag_keyframe+empty_moov+default_base_moof+use_metadata_tags
```

Each fragment carries its own `moof` index, so truncation or localised damage leaves everything before it playable. This applies to remuxes as well as re-encodes: fragmenting costs nothing at remux time, so the default is the same for both. Fragments begin at keyframes, so a remuxed file inherits its source GOP length as its fragment length. `faststart` is not in the list: with `empty_moov` the `moov` is already at the front, and ffmpeg 7.1 accepts the combination silently, so the flag would only mislead a reader. `use_metadata_tags` is required for the identity tag in §6.7 (without it ffmpeg drops the unknown `vidfunnel` key; tested, §9). Seeking over HTTP (Jellyfin) in a file with no global index means the client scans `moof` boxes; test seek speed on the Roku and in a browser, and if it is poor add `+global_sidx` (one `sidx` index at the front; `plan` records whichever was used). `--plain-mp4` writes a conventional `+faststart+use_metadata_tags` file for any player that turns out not to like fMP4 (one known risk: Roku direct play through Jellyfin has not been tested with fMP4 — test one file before converting a library).

Metadata: `-map_metadata 0`, preserve `creation_time`, set output mtime to the source mtime.

Output name: the source directory and stem with `.mp4`. Three collision cases, all decided at `plan` and recorded:

- The output name already exists and the file is not ours (§6.7, no tag or a tag whose `src` does not match): flag `output-collision`, nothing is written, `convert` refuses.
- The source is itself `.mp4`, so the output name equals the source name (a `.m4v` source does not collide): `convert` writes `<stem>.vidfunnel.mp4` and `retire` renames it to `<stem>.mp4` after the original has been moved away (§6.5); the record carries both names. Until retirement the two files sit side by side and the tag says which is which.
- Two sources in one folder share a stem (`movie.avi` and `movie.mkv`): both get `output-collision` and `hold`, nothing is written. Two versions of the same content need a human to pick one; the tool does not guess with a suffix.

Marked outputs (opt-in, added 2026-11-09): `plan --mark-outputs` records `stages.plan.mark_outputs: true` and gives every file source (a single file or a VOB group) the interim name `<stem>.vidfunnel.mp4`, whether or not the clean name is free, so a converted file is recognisable at a glance while the originals remain. Its subtitle sidecars get interim names as well (`<stem>.vidfunnel.srt`, `.N.srt`, `.sup`, `.subs.mkv`), listed in `output.interim_sidecars` beside the final names in `output.sidecars`, in the same order; `retire` renames them with the output (§6.5). With the flag the final name is checked for a foreign file too (`output-collision`), and a user's own `<stem>.srt` at the final name drops the text sidecar as without the flag, because `retire` would otherwise have to rename over it. DVD titles keep their final names, since `retire` does not retire a DVD folder. `--replan` rewrites the choice either way; without the flag nothing changes.

### 2.5 Integrity sidecars

- Always: SHA-256 of source and output in the per-file record (§6).
- `--par2 N` (default off; suggested 10): `par2create -r N -n 1` next to the output. This is the real recovery mechanism; the GOP and fragment settings only limit how much a damaged file misbehaves on playback.

## 3. Remux versus re-encode

Decided per file from the probe.

Remux (`-c:v copy`) when the video stream is H.264, profile ≤ High, level ≤ 4.1, 8-bit 4:2:0, and not interlaced. Audio handled per §2.2 independently. Container rewrapped to fMP4. Keyframe density cannot be changed in a remux; `--reencode-video` forces an x264 pass anyway (use when resilience outranks avoiding a second generation). For remux candidates only, `plan` measures the source's actual keyframe spacing from the packet list (`ffprobe -show_packets -show_entries packet=pts_time,flags`, keyframes carry `K`; no decoding, so it is a demux pass) and records the maximum gap, so the report shows which remuxed files have long GOPs. Typical source spacing: x264/HandBrake defaults 250 frames (8–10 s) with extra I-frames at scene cuts; DivX/XviD 250–300 frames; web FLV/WMV anywhere from 2 s to 10 s+; DVD MPEG-2 at most 18 frames (0.6 s), so VOB-derived content is already dense and gets re-encoded anyway. FLV, MKV, MOV, MP4 and AVI carrying H.264 all qualify; this will be most MKVs.

Re-encode for everything else: MPEG-4 ASP (DivX/XviD, including packed B-frames), MPEG-1/2, Sorenson H.263, VP6, WMV7/8/9, VC-1, Indeo, Cinepak, MS Video 1, RealVideo, QuickTime Animation/Sorenson 3/Apple Intermediate, DV, FLIC, H.264 outside the profile limits, interlaced H.264.

HandBrake's "Web optimized" checkbox is `-movflags +faststart` (moov atom moved to the front). It has nothing to do with keyframes and is not fragmentation. Files HandBrake produced are ordinary H.264/AAC MP4 and will be remuxed, not re-encoded, unless `--reencode-video` is given.

## 4. Analysis rules

All analysis is sampled, not full-length, and every result is recorded with the sample positions so it can be reproduced. Default sampling: 12 windows of 3 s, evenly spaced, skipping the first and last 3 % of the duration (titles, credits, black). Files shorter than 60 s are analysed in full automatically (the windows would overlap). `--samples N` and `--full-analysis` override. Each window is read by one ffmpeg process whose filter graph splits the decoded video into one branch per filter: `idet`, `cropdetect` when §4.2 needs bar evidence, and `blockdetect` in at most 4 of the windows, spread evenly (of 12: the 1st, 4th, 7th and 10th). The split, not a chain, so that each filter negotiates its own pixel format as it would alone (a chain changed cropdetect and blockdetect results on RGB and palette sources); the three results are parsed from that one run's stderr.

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
| Output video duration differs from the source's by more than 0.1 s plus one output frame interval (stream durations, not container durations) | −20 |
| Frame count outside the expected range (after telecine/field-rate adjustment) | −30 |
| Output of a re-encode larger than source (always kept, flag `output-larger`) | `retire` refuses it unless `--allow-larger` is given, which adds the flag `allowed-larger`; a remux is exempt, since only the container changed and fragment headers can make a small file grow slightly (flag `remux-larger`, informational) |
| Output stream count differs from the expected (a video, audio or text-subtitle stream of the source is missing) | −50 |
| Identity tag missing or naming another record or source | −100 |
| Optional sampled SSIM against source below threshold (`--ssim-check`, default off, threshold 0.97 on luma) | −25 |
| Quarantined for any reason | 0 |
| Size class (`stages.analyze.size_class`: bits per pixel per frame, classes `heavy` < 0.15, `near` to 0.4, `smaller` to 1.0, `much-smaller` above) and blockiness (`stages.analyze.blockiness`: mean blockdetect score over at most 4 of the sample windows, listed under its `samples`, "not measured" without the filter) | 0, informational; read by `convert --refit` |
| Refit replaced the output (flag `refitted`) | 0, informational; the replaced output is verified and scored again |
| Refit kept the existing output (flag `fit-capped`) | 0, informational; the record keeps `output-larger` and `retire` still needs `--allow-larger` |

Bands: ≥ 90 `ok`, 60–89 `review`, < 60 `hold`. The report sorts ascending so the worst conversions are at the top for hand checking. `retire` (§6.5) refuses `hold` and, without `--allow-review`, refuses `review`.

## 6. Pipeline and CLI

Every stage is a separate subcommand, idempotent, and resumable. State lives in per-folder records (§6.7); an optional central index is a cache, never the source of truth.

```
vidfunnel check
vidfunnel plan   [--samples N] [--full-analysis] [--assume-dar R] [--audio-offset S] [--audio-strict-aac] [--reencode-video] [--max-level 4.1|4.2] [--plain-mp4] [--par2 N] [--mark-outputs] [--replan] [--jobs N] [PATH...]
vidfunnel convert [--par2 N] [--jobs N] [--dry-run] [--refit] [PATH...]
vidfunnel verify [PATH...]
vidfunnel report [--format text|md|json|csv] [--min-score N] [--reveal] [--with-logs] [--index] [PATH...]
vidfunnel report --from REPORT [--reveal]
vidfunnel find ID
vidfunnel retire [--allow-review] [--allow-larger] [--quarantine DIR] PATH...
vidfunnel retire --destroy-originals-now [--allow-review] [--allow-larger] PATH...
vidfunnel index [--rebuild] [--db PATH] [PATH...]
vidfunnel stop-all [--now] PATH...
vidfunnel resume PATH...
vidfunnel --install-command [--user|--system]
vidfunnel --uninstall-command
vidfunnel --version
```

Where `PATH...` is bracketed, no PATH means the current directory, announced on the first output line (`vidfunnel plan: no PATH given; using the current directory '...'`). `retire` keeps PATH required, as a deliberate-action check for the one stage that moves originals (decided 2026-11-07).

Ctrl+C stops a run without leaving false records (`vidfunnel.interrupt`). SIGTERM (`kill`, or `podman stop`, where `python3 -m vidfunnel` is the container's PID 1 and would otherwise ignore it until the engine sends SIGKILL) is handled the same way: `vidfunnel.cli.main` installs a handler, on the main thread and only while SIGTERM has its default action, that sets a plain module flag and raises `Terminated`, a KeyboardInterrupt, on the main thread, so the folder locks are released and the part files removed as after Ctrl+C. The handler takes no lock: Python can run it nested in another run of itself or inside main-thread code that holds a lock, and a handler waiting for that lock hangs for good (a threading.Event's lock did). Everything that locks runs in the except paths on the main thread: `run_pool` and `main` call `vidfunnel.interrupt.stop_workers`, which sets the interrupt event and sends SIGTERM to every ffmpeg and par2 child still running (a SIGTERM to the Python process alone does not reach them, unlike Ctrl+C to the terminal's process group; `vidfunnel.ffmpeg` keeps a registry of the running children for this). The interrupt event is itself lock-free (`vidfunnel.interrupt.Flag`: set, clear and is_set are single attribute stores and reads, wait polls), because a KeyboardInterrupt raised inside `threading.Event.set` leaves its lock held. A child the main thread runs itself is killed and reaped on the way out. The run prints `vidfunnel: terminated (SIGTERM)` and exits with status 1. SIGHUP (a closed terminal) and SIGQUIT (Ctrl+Backslash; the kernel drops both for a PID 1 without handlers, so a container run used to ignore them, and on a host both left the lock and part files behind) get the same handler, each only while its action is the default, so a SIGHUP that `nohup` ignores stays ignored, and the last line names the signal. Only the first of the three raises; a later one does nothing and does not cut that cleanup short. After a hangup, or once a `| tee` has died with the run's terminal, writes fail with EIO or EPIPE: `vidfunnel.progress._write` then points that descriptor at `/dev/null` (`os.dup2`) and drops the line, so no file in progress is recorded as failed for a write and the flush at exit cannot fail (measured on a pty whose master was closed during `plan --jobs 2`: with the SIGHUP handler but without this, two records said `failed` with `[Errno 5] Input/output error` and the exit status was 120; with it, exit 1, no record touched, no lock or part file left).

SIGUSR1 drains (its default action would end the process): the handler sets `vidfunnel.interrupt.draining`, another lock-free flag, and writes one line to file descriptor 2 with `os.write`. `run_pool` hands out no further task (each worker puts a final marker on the result queue as it leaves, and results are collected until every worker has left), and the sequential loops of `plan`, `convert` (`--refit` too), `verify` and `retire` ask `drain_before_next` before each next file; the files in progress run to their end, their children are not signalled, the interrupt event stays clear, and their results are recorded as usual, `convert`'s verify included. `main` then prints `vidfunnel: drained: finished N in progress, started no more (M left planned)` (`not started` for `plan`, `--refit`, `verify` and `retire`) and returns the stage's own exit code; the counts are kept on the main thread in `vidfunnel.interrupt.drain_tally`. A second SIGUSR1 does nothing; SIGTERM, SIGHUP, SIGQUIT or Ctrl+C during a drain stop at once as usual. `check`, `report`, `find`, `index`, `stop-all` and `resume` have no items to stop between; they run to their end and say so.

`vidfunnel stop-all [--now] PATH...` (`vidfunnel.stopfile`) writes `PATH/.vidfunnel/stop` atomically, `{"host", "pid", "requested", "mode": "drain" | "now", "user"}`; `vidfunnel resume PATH...` removes it, and nothing else writes or removes it. Before each file `plan`, `convert`, `verify` and `retire` start (where `run_pool` takes a task and where the sequential loops ask `drain_before_next`), the run walks up from that file's folder to the root, one `stat` per level, for a stop file: `drain` sets the same drain flag as SIGUSR1, `now` acts as Ctrl+C (the interrupt event, SIGTERM to the children through `stop_workers`, `KeyboardInterrupt` on the main thread or `Interrupted` on a worker); a `now` file is also looked for before each ffmpeg, ffprobe and par2 start (`raise_if_stopped_now`), so it takes effect at the next file or tool start, not in the middle of a running encode; `retire` looks before each file only, since its one tool, par2, runs after the original has been moved and stopping there would leave the file half retired. The first find is announced once with the file's host, time and mode. A file that exists but cannot be parsed counts as a drain, and so does anything there that is not a regular file (a FIFO, which is never opened, since opening it would wait for a writer). A drain, once begun, holds for the whole run, so a stop file on a subfolder ends a run started above it once the run reaches that subfolder. `plan`, `convert`, `verify` and `retire` refuse to start (exit 1, before any folder lock) under a stop file at or above one of their PATHs, naming the file and `vidfunnel resume '<dir>'`. Through the container wrapper, `vidfunnel stop` signals this host's run (SIGUSR1, or SIGTERM with `--now`); `stop-all` and `resume` reach every host through the share.

### 6.1 `check`

Dependency table for this machine: ffmpeg/ffprobe version, `libx264`, native `aac`, `dvdvideo` demuxer, `libdvdread`, `par2`, filters `idet`/`bwdif`/`fieldmatch`/`cropdetect`, bitstream filter `h264_metadata`; each marked required or optional. Prints the exact install command for the detected platform (`/etc/os-release`: zypper/dnf/apt/pacman; macOS: Homebrew). `plan`, `convert` and `verify` run the same check first and refuse with the same message if something required is missing. `report`, `find`, `index` and `retire` work from records and hashes alone and run without ffmpeg, so a redacted report can be read and resolved on any machine.

### 6.2 `plan`

Probe + analysis (§4), writes the JSON record with the full decision list, the reasons and the sample positions. The record describes the file and how it is to be converted; it does not hold a program to run. `plan` also stores the ffmpeg command as it would be rendered on the planning machine (`stages.plan.argv`, and one per subtitle sidecar), as a preview for `report` and for reading the record by eye. `convert` does not execute or read it (§6.3). Prints a summary table: action (remux / re-encode / quarantine), flags, predicted confidence, source size. `plan` never writes media. Re-running `plan` on a planned file is a no-op unless `--replan`. `--jobs N` plans N files at once, also within one folder: the ffmpeg and ffprobe passes run in parallel, while each file's record step (reading its record and the folder's records that claim the same output name, deciding the collision, saving) runs under a per-folder lock, and files that would share an output name are planned one after another in list order, so the records equal those of a sequential run. The record also notes which ffmpeg made the measurements, under `stages.plan.ffmpeg`: its path, its version string and the SHA-256 of its configuration line. This is information about the analysis; `convert` does not compare it with its own ffmpeg.

### 6.3 `convert`

Renders the ffmpeg command for each planned record and runs it to a temp file in the same directory (`.NAME.mp4.part`), renames into place on success, sets mtime, writes PAR2 if asked, then runs `verify` on the result. `--jobs N` parallelises at the file level; x264 already uses all cores for one file, so the default is 1 and `--jobs` is for remux-heavy sets. `--dry-run` prints the commands as rendered on this machine and writes nothing. A file whose record says `converted` or `verified` and whose output still matches its cheap fingerprint (size, mtime, first and last MiB, §6.7) is skipped; a failed run leaves the record at `failed` with the ffmpeg stderr, and the `.part` file is removed.

**Rendering.** The command is built at convert time from three things: the record (probe, analysis, action, options, output name, record ID, source SHA-256), the folder the record sits in, and this machine's ffmpeg (`--ffmpeg`, `VIDFUNNEL_FFMPEG`, or PATH). Source and output paths are the folder joined with the names in the record, so a library that was moved, or is mounted elsewhere on another machine, converts without a new plan. The ffmpeg path is this machine's, so a plan made natively converts through the container and the other way round, and a rebuilt image with a newer ffmpeg converts the plans made with the older one. The subtitle sidecar commands are rendered the same way, and the sidecars `plan` left out because a file that is not ours held the name (`stages.plan.sidecars_skipped`) stay left out; when the sidecar names rendered now differ from `output.sidecars`, the record is skipped with both lists and "plan again with --replan". A record written by an older vidfunnel, whose `stages.plan.argv` was once the command to run, is rendered like any other; the stored command is left in place and ignored. `plan` renders its preview with the same code (`vidfunnel.plan.render_record`), so the two cannot drift.

**What convert writes down.** The convert stage records the command that ran (`stages.convert.argv`, and `stages.convert.sidecar_argvs`), the ffmpeg that ran it (`stages.convert.ffmpeg`: path, version string, SHA-256 of the configuration line) and the vidfunnel version that rendered it (`stages.convert.version`). The record of a converted file therefore says what was decided, why, and what was actually executed, by which build. A failed run records the same three. The ffmpeg's identity comes from one `-version` call per build per process (`vidfunnel.ffmpeg.current_identity`), so a run over 500 records calls it once.

**What this ffmpeg must have.** Before a record's command runs, `convert` checks that its ffmpeg has what that command names, from the capability listing `check` uses (cached per binary, §6.1): `libx264` and the native `aac` encoder for a re-encode, each filter the record's video and audio chains name, the `h264_metadata` bitstream filter for a remux that sets aspect or colour, and the `dvdvideo` demuxer for a DVD-title record. When something is missing the record is skipped (counted as skipped, not failed, and left untouched) with a reason that names the missing item and the ffmpeg's path. Two machines with different ffmpeg builds may convert on one share; each file's record says which build made it. `verify` needs no such check: it probes the output.

**Values taken from the record.** Records travel on shares, so `convert` treats a record as data that may have been edited. Names (`source.name`, the members of a VOB group, `output.name`, `output.interim_name`, sidecar names) must be plain file names with no directory part; the filter strings, aspect ratios, colour names, level, bitrates and stream indexes that go into the command must have the form `analyze` writes. A record that fails this is skipped and left untouched, with a reason that names the field. The source's cheap fingerprint is compared with the record as before; a changed source is `source-changed`.

**A newer vidfunnel renders by its own rules.** What the analysis decided (crop, cadence filter, sync correction, audio handling, colour) is in the record and does not change. What the renderer itself supplies (the CRF for a frame height, the preset, the default level, the container flags) follows the vidfunnel that runs `convert`. That is why the version and the command are recorded at convert time.

`--refit` (built 2026-11-07) revisits re-encodes flagged `output-larger` (status `converted` or `verified`, no `fit-capped`, no completed refit) instead of planned records. It renders the command again, from the record and this machine's ffmpeg, with the CRF raised by `ceil(ln(ratio) / ln(1.12))`, where ratio is output size over source size (one CRF step of `-preset slower` saved 11 to 12.5 % of size on the material measured for the rule; 1.12 per step is a round figure a little below that), capped at 23 up to 576 lines and 24 above (§2.1). When the analysis put the source in the `heavy` size class and its blockiness is at or above 3.5, `hqdn3d=2:1:2:3` goes into the video filter chain, first or right after the cadence filter. The CRF it raises is the one the existing output was made with: read from `stages.convert.argv`, or from `stages.plan.argv` for a file converted before convert recorded its command (read for that one number, not executed). The capability and value checks above apply to a refit as well. The result goes to the part file; it replaces the output only when it is smaller and passes the identity, stream-count and duration checks of §6.4, after which the record returns to `converted` with its verify stage moved to history, gets the flag `refitted`, loses `output-larger` only when the new output is no larger than the source, has its PAR2 written again when convert wrote one, and is verified straight away. Otherwise the part file is removed, the output is kept, and the record gets the flag `fit-capped`. The attempt (CRF, filters, size, ratio, time, the command, the ffmpeg that ran it and the vidfunnel version) and the outcome are recorded under `stages.refit`; an attempt whose ffmpeg run failed is recorded as `failed` and tried again on the next `--refit` run. `--dry-run` prints the refit commands.

### 6.4 `verify`

Full-length null decode of the output with error counting, duration and frame-count comparison, stream-count comparison, optional SSIM sampling, size ratio. Updates the record and the score. Can be re-run at any time (e.g. after moving files).

### 6.5 `retire`

The only stage that touches originals, and it is separate from `convert` by design. For each source it checks: a record exists, status is `verified`, the source still matches the record's cheap fingerprint (it has not been replaced since it was planned), output exists and its full SHA-256 matches the record, score meets the band rule, output size rule satisfied (a re-encode larger than its source, or flagged `output-larger`, only with `--allow-larger`, which flags the record `allowed-larger`). Only then it moves the original into the quarantine directory (default `.vidfunnel-retired/` under the top-level path given, preserving relative paths), and, for an output written under the interim `<stem>.vidfunnel.mp4` name (§2.4), renames it to `<stem>.mp4` once the original is out of the way, recording the rename; sidecars written under interim names (`output.interim_sidecars`, §2.4) are renamed to their final names after a successful output rename (`renamed_sidecars` in the retire stage), and a final sidecar name held by another file refuses the retire before anything is moved (`interim-rename-blocked`). `--destroy-originals-now` unlinks instead of moving and is the only destructive flag in the tool; it prints what it is about to do and requires typing `destroy` on stdin unless `--yes` is also given. No other subcommand ever removes or overwrites a source.

### 6.6 `report`

Reads the records and prints them sorted: record ID, score, band, status, action, flags, source name, source → output size. Plain text by default, for reading in a terminal: rows grouped by folder with the folder line printed once per group, folders in the order of their worst row and rows inside a folder in report order, each record on two lines (score, band, status, action, flags, ID; then source → output name and sizes), columns aligned over the whole report. A quarantined or failed record gets a third line, `reason: ...`, always (not only with `--with-logs`, whose logs follow it): for a quarantine, `stages.plan.collision_reason` when set (a planned record quarantined later by another source's plan keeps its planning reason in `stages.plan.reason`), else `stages.plan.reason` when the plan's action was `quarantine`, prefixed with `quarantine_reason` when the text does not start with it followed by `:`, else `quarantine_reason` alone; for a failure, the `error` (else `stderr`) of the last stage that stored one, as `<stage>: <first line>` (with the next non-empty line joined to it when the first ends with `:`, as ffmpeg errors do: `'ffmpeg' exited with status 1:` and the cause on the next line) plus ` ...` when there are more lines. `--with-logs` adds a stage's `error` to the logs when it has more than one line and no stored stderr or log of that stage holds the same text (a verify failure stores only `error`). Markdown lists the same reasons in a `## Reasons` section between the table and `## Folders` (which `--from` skips), and JSON and CSV carry them as a `reason` field (in CSV the `reason` column comes before the `logs` column that `--with-logs` adds). Every record collected is passed to the redactor before the filters drop any, since a reason or log may name another record's file; when a PATH is a single file, the other records in its folder's `.vidfunnel/` are learned too (for redaction only, they add no rows). A reason or log names files in single quotes, and a file may be named that no record holds any more (the other source of a collision, removed as the reason asked), so every single-quoted part that looks like a file name (a `/`, a plausible extension, or a DVD title key `<name>_titleNN`) is redacted as a name or path whether or not it was learned; quoted codec, status and tool names are kept. `--format md` gives a Markdown table to paste into Obsidian, without a path column: the `source` cell holds the file name (and the output name only when it is not the source stem plus `.mp4`, such as the interim `<stem>.vidfunnel.mp4` or a DVD title), the sizes have their own column, and a `## Folders` section after the table lists each folder once with the IDs of its rows. Text and Markdown show each record ID as its first 8 characters, more only when two of the records the report read share those (counted before the filters, so a shown prefix stays unique for `--from`), which `find` accepts as a prefix. Rows are grouped by their real folder, so two folders with the same redacted form stay two groups. The output name shown is the name the output has or will have: the interim name until `retire` renames it, and also after a `retire` whose rename failed (`stages.retire.renamed_output` false). In text, and in the Markdown table, Reasons and Folders lines, every line boundary that Python's `str.splitlines()` recognises inside a name or other field (also U+2028, U+2029 and U+0085, which the `--from` parser splits on) is printed as a space. JSON and CSV keep full IDs and are for processing. Names are redacted by default (§6.8); `--reveal` shows them. Reports are generated from records only, so a report is cheap and never triggers probing or encoding.

Every report starts with a header: report ID, generated-at time, tool version, the exact invocation (paths, filters, format), and a hint line reading, in substance, "Names in this report are redacted. To see them, re-run the same command with --reveal, or `vidfunnel report --from <this file> --reveal`. To locate one row: `vidfunnel find <ID>`." Text and CSV reports cannot be read back, so their hint offers `--format md` in place of `--from`. `--from REPORT` reads the invocation out of an existing Markdown or JSON report's header and re-runs it with the same arguments plus whatever is added, so a redacted report that was sent out can be reissued revealed without reconstructing the command. A text report is for reading only; `--from` refuses it with a usage error, also when stderr's progress lines were saved in front of it. Rows are matched by record ID, an ID prefix of a Markdown report resolved against the records found (a prefix matching more than one record is an error naming both); a revealed Markdown report whose invocation has no PATH (made from the index) reads the whole index again and keeps the rows it lists, and reads the folders of its Folders section only when no index can be read (`--no-index` or no database file), skipping with a warning any folder that no longer exists; Markdown reports in the earlier layout (full IDs, a `path` column) are still read; if a record's status has advanced since the original report, the row carries a `changed-since` marker rather than silently showing newer data.

### 6.7 Records, identity and "have I seen this"

Three layers, one source of truth:

1. **The output file identifies itself.** Every file the tool writes (remux or re-encode) carries a freeform metadata tag written with `-movflags +use_metadata_tags`:

   ```
   vidfunnel={"id":"<record id>","tool":"vidfunnel","ver":"20261101","src":"<first 16 hex of source SHA-256>","profile":"h264-high41-fmp4"}
   ```

   `ver` is the vidfunnel version that wrote the file, that is, the version that ran `convert`. `verify` and `plan` identify an output by `tool`, `id` and `src`; `ver` and `profile` are informational.

   The tag travels with the file through copies, renames and moves. An MP4 next to a source with the same base name but without this tag (or with a tag whose `src` does not match the source) is *not ours*: `plan` reports `output-collision` and does nothing; `convert` refuses. There is no `--adopt`; a conversion the tool did not make and cannot verify is not trusted. Round-trip of the tag through ffprobe, and that the standard `title` tag still shows in QuickTime/Jellyfin alongside it, is a test (§9).

2. **Per-folder records are the audit trail and the authority.** Each folder processed gets a `.vidfunnel/` directory holding one JSON record per *source* file (named after the full source file name including its extension, `movie.avi.json`, so `movie.avi` and `movie.mkv` never share a record; quarantined and unsupported sources have records too, not only converted ones), plus `folder.json` (last scan time, tool version, inventory of source names seen). A record holds: record ID (random UUID, assigned once at `plan`), source path relative to the folder, source fingerprint (size, mtime, SHA-256 of the first and last 1 MiB — cheap, computed on every `plan`), full source SHA-256 (computed once, reused), every probe result and decision with its reason and sample positions, the ffmpeg command `plan` previewed and the one `convert` ran (with the ffmpeg build and vidfunnel version that ran it), output name (and the interim name when §2.4 applies), output fingerprint and SHA-256, verify results, score, status (`planned` / `converted` / `verified` / `quarantined` / `failed` / `retired` / `source-changed`; the `ok`/`review`/`hold` bands of §5 are a property of the score, not a status, and the quarantine reason such as `unsupported` or `swf-vector` is a field of its own), and timestamps per stage. Records are append-only per stage: a stage adds its section, never rewrites an earlier one.

   "Have I looked at this?" is answered from here: `plan` walks the tree, and for each source with a record whose cheap fingerprint still matches, skips it (status shown in the summary); if the fingerprint changed, the record is marked `source-changed` and the file is re-planned under a new record ID with the old one kept in `history`. A folder with no `.vidfunnel/` has never been scanned. Records survive the tool being run from different machines over the same share, because they sit with the data, and two machines running at once on the same folder is prevented by a `.vidfunnel/lock` file with host name, pid, start time and whether the holder ran inside a container (`/run/.containerenv` or `/.dockerenv` exists). Every lock is respected for 24 h and taken over with a warning after that; the pid can only shorten the wait. A lock written on this host name by a run on the same side of a container boundary is judged by its pid: when that process no longer exists (`os.kill(pid, 0)` raises `ProcessLookupError`) the lock is taken over at once with a message naming the dead pid and the lock's age; while it exists (or may not be signalled, `PermissionError`) the folder is skipped with a message saying the pid is running here, until the lock is 24 h old, when the pid is taken for a reused one (after a reboot, or a container's PID 1 that is vidfunnel itself). A pid that is this process's own, or that `os.kill` cannot take (not a positive int, or 2**31 and more, `OverflowError`), counts as unknown. Any other lock is judged by age alone, because its pid means nothing here: a container run shows the container ID as its host name and has its own pid namespace; the skip message says when it is held by a container run. The lock file is only created with `O_CREAT | O_EXCL`. A takeover runs under `flock(LOCK_EX)` on the regular file `.vidfunnel/lock.takeover` (else on the `.vidfunnel` directory (released by the kernel if the holder dies), and inside it re-checks that the file is the one it judged (inode, mtime, size, content), unlinks it and creates its own with `O_EXCL`; a racer that loses either step raises LockHeld naming the winner. Where a file system refuses `flock` on both, the takeover runs without it, guarded only by that check and the `O_EXCL` create, and two racers can both get in. A holder's pid is judged only when the lock was written on this machine: the lock records `/etc/machine-id` (else the kernel boot id), since two machines on one share may carry the same host name.

   The holder also keeps the descriptor it created the lock file with open for the whole run and takes `flock(LOCK_EX)` on it before writing the contents, which add `boot` (`/proc/sys/kernel/random/boot_id`, `null` where there is none, as on macOS), `device` (the file's `st_dev` as the holder sees it) and `flock` (`true` once taken; `false` without `fcntl` or on a file system that refuses `flock`, and then the rules above apply unchanged). `vidfunnel.ffmpeg.run` and `encode.write_par2` start every child with `pass_fds` set to the descriptors of all the locks the process holds (`vidfunnel.record.held_lock_fds`, a registry under a lock that is also held while a descriptor is closed, so a child never gets a closed or reused number), and a flock belongs to the open file, not to a process: the lock is free when the last process holding the descriptor exits, so an ffmpeg left running by a SIGKILLed vidfunnel keeps the folder locked until it ends (before this, the pid rule freed the lock at once and the next run restarted the same item into the same part file). On NFS, where Linux emulates `flock` with POSIX `fcntl` locks, which a child does not inherit, the lock is freed when vidfunnel dies even while its ffmpeg runs on, the same outcome as before this rule. The `flock` rule is verified on local file systems and bind mounts, and untested over NFS, SMB and sshfs (on SMB without `nobrl` a `flock` becomes a byte-range lock, which other hosts may meet when they read the lock file; a lock that cannot be read is judged by its age). A judge whose boot id equals the lock's (the same kernel: a container and its host share one) and that sees the lock file on the lock's `device` (the same mounted file system: a bind mount shares it, while a share mounted twice, each mount keeping its locks to itself as sshfs does, shows two devices) opens the file read-only and tries `flock(LOCK_SH | LOCK_NB)`: busy means a holder is alive, and the folder is skipped whatever the lock's age (the one case where liveness lengthens the 24 h wait, because the kernel says so, not a pid); free means every holder has exited, and the lock is taken over at once with a message naming it, which is what a container run killed with SIGKILL needed (its lock said pid 1 on a throwaway host name, so the pid rule could not judge it and the next run waited 24 h). Any other lock (another boot id, another device, `flock` false, or no `fcntl`) is judged by the pid and age rules above, except one written on this machine before its last reboot: when the lock's `machine` is ours and came from `/etc/machine-id` (its `machine_source` is `machine-id`, as opposed to `boot-id` or `hardware`; a lock without the field counts when its `machine` differs from its `boot`, since the boot id fallback makes them equal) and its `boot` is present and differs from ours, no process of that boot still runs, so it is taken over at once with a message saying the holder ran before this machine's last reboot, before the pid rule (a pid reused since the reboot must not count as alive) and the age rule; `vidfunnel-podman` mounts the host's `/etc/machine-id` read-only so that container runs record the host's id rather than the boot id. Release unlinks the file first and closes the descriptor after, so the file never exists unflocked while its run holds it. The holder never opens and closes its own lock file (on NFS, where Linux emulates `flock` with POSIX locks, closing any descriptor of a file can drop them): `release` reads the file through the kept descriptor, and a judge in the holding process that finds the lock file this process holds (same `st_dev` and inode) raises LockHeld without opening it. A `flock` does not cross kernels, so another machine on the share, NFS mounted `nolock`, or a Mac's podman VM next to the Mac still sees only the age rule.

   Why not sidecars next to each file: a flat folder with one `.json` per video doubles the entry count in Dolphin, Finder and Jellyfin, and loose sidecars get separated from their videos. One hidden directory per folder keeps the folder readable and keeps all the state in one place to copy, inspect or delete.

3. **Central index: optional, derived, disposable.** `vidfunnel index PATH...` walks the given trees and loads every `.vidfunnel/*.json` into a SQLite file (`~/.local/state/vidfunnel/index.sqlite` by default; `sqlite3` is in the standard library). `report` and `find` use it when present for cross-folder queries and speed; `--rebuild` drops and recreates it. Nothing is ever written to the index that is not also in a record, and deleting the index loses nothing. It is per machine and per user, which is why it cannot be authoritative for a share used from several machines.

`vidfunnel find ID` resolves a record ID (or any unique prefix) to the real path, status and output, which is how a redacted report (§6.8) is mapped back to files locally.

### 6.8 Redacted reports

Redaction is the default for `report` output; `--reveal` is the opt-in. Records on disk are never redacted. The default produces a report that can be shared for troubleshooting without revealing file names, subject matter or directory structure, while keeping enough shape that the user can match rows to files by eye and resolve any row exactly with `find ID`.

Redaction rules, applied as a pure function of the string (deterministic, no salt, no state):

- Every word in a file name, directory component, or metadata string that starts with a letter keeps that first letter; every further letter becomes `_` and every further digit becomes `#`. A word of four or more characters also keeps its last character when that is a letter (`Wedding` → `W_____g`, `Day` → `D__`, `part2` → `p___#`), so long names of the same length stay distinguishable to someone looking at the real folders. A word that starts with a digit is masked completely (a leading `2` in a year or a `0` in a month carries no shape worth keeping). Word length is preserved. Separators (space, `_`, `-`, `.`, parentheses, brackets) are kept as they are. The file extension is kept. Non-ASCII first characters are kept as one character. Example: `Client Name - Deposition 2019-03-14 part2.avi` → `C____t N__e - D________n ####-##-## p___#.avi`.
- Paths: every directory component is redacted the same way; the number of components and the separator are kept. The top-level path given on the command line is replaced by `<root>`.
- Metadata: `title`, `artist`, `album`, `comment`, `description`, `show`, `episode_id`, chapter titles, `handler_name` and any other free-text tag in the probe output are redacted. Codec names, stream parameters, dimensions, durations, bit rates, frame rates, timestamps, flags, scores, the ffmpeg argv and ffmpeg stderr are kept, because they are what troubleshooting needs. Inside the argv and stderr, every occurrence of the real source path, output path, and any redacted metadata value is replaced by its redacted form before output, so an ffmpeg error line cannot leak a name. The substitution is one regex over every real path, name and value of the report, shaped as a trie (`clip`, `clip 1`, `clip2` and `clock` give `cl(?:ip(?:\ 1|2)?|ock)`, longest match first) so the engine follows one branch per character instead of trying every name at every position, and compiled once per report, after every record has been learned. The trie source is built without recursion, so a metadata value thousands of characters long is no problem; when Python's regex parser cannot compile the nesting (several hundred values that are each a prefix of the next), the flat alternation, longest first, is used instead, slower but matching the same. Above 500 records `report` prints `report: redacting N records` before it starts, since redaction takes about 0.6 s per thousand records.
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

- Before any operation that may take a second or more, print one line saying what is about to happen and to which file (`[plan 12/340] probing C____t N__e.avi`), redacted the same way as reports when the terminal output is being captured with `--log FILE`, revealed on the terminal.
- Every line about one item of `plan`, `convert`, `convert --refit` and `verify` starts with the item's counter (`[convert 3/1054] [re-encode] 01:17:26 / 01:37:08  79.7 %`), the live progress lines and the lines of the verify that `convert` runs included, so the interleaved lines of `--jobs` say which item each belongs to; sequential runs carry the same tag. `vidfunnel.progress.TaggedStream` adds it at every line start (after a newline or a carriage return), so a terminal's in-place line keeps it on each redraw.
- Long operations show live progress. ffmpeg is run with `-nostats -progress pipe:1`, which emits `out_time`, `frame`, `fps` and `speed` every half second; these render as one updating line: `[re-encode] 00:12:34 / 01:32:10  13.6 %  212 fps  8.1x  eta 00:09:48`. Hashing and copying report bytes and MB/s the same way. Sampled analysis shows the window counter (`window pass 7/12`). Operations with no measurable length show an elapsed-time counter, not a spinner.
- On a TTY the line updates in place (`\r`, no ANSI beyond that, `NO_COLOR` respected, plain ASCII). When stdout is not a TTY (log file, CI, `--jobs` with interleaved output) the same information is written as a new line at most every 30 seconds, so logs stay readable and nothing is lost.
- `--quiet` keeps per-file start lines and errors only; `--verbose` adds the ffmpeg argv before each run. There is no mode that is silent while working. The `--log FILE` option described above is not implemented in the first build (open question 8).
- Each stage ends with a one-line summary: counts per outcome, total time, and where the records went.
- Implementation in one module, `progress.py`, used by every other stage; no third-party packages.

## 7. Architecture and repo layout

Python ≥ 3.9, standard library only (subprocess, json, argparse, pathlib, concurrent.futures). No third-party packages, so it runs from a clone with the system Python on Fedora, Tumbleweed, Debian and macOS. ffmpeg/ffprobe are external and discovered on PATH (`--ffmpeg PATH`, defaulting to the `VIDFUNNEL_FFMPEG` environment variable, points at the ffmpeg binary or wrapper; ffprobe is looked up next to it under the same name pattern, so a pair of `distrobox-export` wrappers in a directory of their own works, and the host's ffmpeg is never shadowed).

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
    data/           reserved for the few small real files that cannot be synthesised (a `.fli`); `.gitignore` excepts them; empty so far
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

uCore / immutable hosts without Python (the `photos` server, Ryzen 5600, which is where most converting will happen): run the tool in a container. `check` recognises an image-based host (ostree or bootc: the `/run/ostree-booted` marker, the `rpm-ostree` or `bootc` binary, or an image `VARIANT_ID` in os-release) and promotes the container route in its hints, with `rpm-ostree install` layering as the second option; it never prints `dnf` commands there. The repo ships a `Containerfile` (Alpine + `python3`, `ffmpeg`, `par2cmdline`, and `ffsubsync` installed with pip into a venv inside the image; the build checks `-encoders` for `libx264` and `-demuxers` for `dvdvideo` and fails loudly if either is missing, so a failure means the base image changed) and a `vidfunnel-podman` wrapper that bind-mounts every existing folder argument, the folder of every existing file argument, and the current directory read-write at the same path inside the container, also at the physical (symlink-resolved) path when that differs and along every hop of an argument that is a symlink, never `/`, prepares `--quarantine` and `--db` targets on the host (full names anywhere; after `retire`, `index`, `find` or `report`, the abbreviations argparse accepts, with or without `=`), mounts `report --from=` and `find --root=` values, refuses `--ffmpeg` (the image's ffmpeg is the only one that works inside it; `VIDFUNNEL_FFMPEG` is not passed in), runs as the invoking uid/gid (`--userns=keep-id`) so records and outputs are owned correctly, and passes `nice`/`ionice` through. The wrapper passes `HOME` in and mounts `~/.local/state/vidfunnel` and `~/.cache/vidfunnel` from the host (created first, and also at their physical path when `~/.cache` or `~/.local/state` is a symlink), because the passwd entry that `--userns=keep-id` adds has a home that is not usable (`/`, root-owned and read-only, on a workstation; the working directory on the uCore host) and `~` would otherwise resolve to the wrong place; inside the container `HOME`'s own directory is root-owned and not writable unless `HOME` is under the working directory, which is mounted whole; otherwise only the two mounted folders are writable, and vidfunnel writes nothing else under `HOME` (ffsubsync 0.5.1 runs in that configuration, checked with `--version`). `--install-command` and `--uninstall-command` (or their accepted abbreviations) among the options before the subcommand run on the host without needing an engine and link `vidfunnel` to the wrapper (through the repo's installer with `--launcher` when the host has Python 3.9 or newer, else through a minimal sh fallback that edits no rc file). Debian trixie slim is the fallback base if something ever needs glibc (the `alass` binary, for instance); its ffmpeg 7.1.5 also has `dvdvideo`. Records live beside the data (§6.7), so a tree worked on from the container and from a workstation stays consistent. The image is pinned by digest in the wrapper and rebuilt deliberately, matching the project's container-update conventions. This is a v1 target.

The wrapper runs `plan`, `convert` and `verify` in a detached container (`vidfunnel-STAGE-HASH`, labelled `vidfunnel=1` with stage, cwd, paths and start time) and follows it with `logs -f`, so a dead terminal, wrapper or `| tee` (a terminal SIGQUIT on the server, 2026-11-08) no longer leaves a run nobody can see; it removes the container itself after `wait` gives the exit code, because `-d --rm` let podman remove it before `wait` could read the code (podman 6.1.4, k8s-file driver). Ctrl+C stops the run through `podman stop` (a second one only detaches), a second start on the same paths on the same host is refused, `attach` lists, replays and follows a run (`attach --clean` removes exited ones), and `stop [NAME]` drains this host's run with SIGUSR1 (`--now`: SIGTERM) and refuses a path, while vidfunnel's own `stop-all PATH` and `resume PATH` (the share-wide stop) pass through attached. `VIDFUNNEL_ATTACHED=1` keeps the old attached run.

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
7. Level 4.2 for field-rate deinterlaced HD (§4.3) is a judgement call made in this revision: it keeps the motion the field-rate rule exists for, at the cost of the single-level promise. `--max-level 4.1` is the escape hatch. Revisit if a target device turns out to reject 4.2.
8. `--log FILE` (a redacted copy of the terminal output) is specified in §6.10 but not built; the CLI does not offer the option until it works. `--subsync` and `--embed-subs` (§4.4a) are likewise not built.

## 11. Conventions (the author's)

- Versions are date-based: `VERSION = "20261101"`, same-day patch `20261101.1`. `--version` prints them.
- Every script and source file ends with a blank line and `# End of File #` (language-appropriate comment syntax).
- No comments inside shell command blocks in documentation; explanations in prose.
- README and docs are plain Markdown, no YAML frontmatter.
- Minimal and auditable over clever: no daemon, no authoritative database (the SQLite index is a rebuildable cache), no third-party Python packages, every decision written down with its reason, nothing destructive outside `retire`.
- Report errors directly and specifically; never claim success without the verification pass.

# End of File #
