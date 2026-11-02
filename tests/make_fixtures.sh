#!/bin/sh
#
# make_fixtures.sh - synthesise the vidfunnel test fixtures with ffmpeg itself
#
# VERSION = "20261102"
#
# Usage:
#   tests/make_fixtures.sh [--force] [OUTPUT_DIR]
#
#   OUTPUT_DIR  where to write the fixtures (default: tests/fixtures under the repo root,
#               found from this script's own location; that folder is git-ignored)
#   --force     re-make fixtures that already exist (default: skip them)
#   FFMPEG      environment variable naming the ffmpeg binary (default: 'ffmpeg' on PATH)
#
# Every clip is synthesised from lavfi sources (testsrc for video, sine for audio), so nothing is
# downloaded. Video uses testsrc, not testsrc2: ffmpeg's idet filter classifies plain progressive
# testsrc2 as mostly interlaced (TFF 83, BFF 47, progressive 20 of 150 frames at 720x480), which
# would make every progressive fixture look like mixed cadence; testsrc reads as progressive or
# undetermined, and as TFF only after tinterlace or telecine.
#
# Clips are 5 seconds long except where noted. One line is printed per fixture, and a count at
# the end. A fixture that the local ffmpeg cannot produce prints a SKIP line with the reason (the
# last line of ffmpeg's error output) and the run continues; the exit status is 0 when nothing
# failed, 1 when at least one fixture failed, 3 when ffmpeg is not found. With ffmpeg 7.1.5 the
# full run makes 39 fixtures in about 25 s, about 29 MB in all (the DV clip is 15 MB).
#
# AAC audio in Matroska starts at -0.021 s (the encoder's 1024-sample priming at 48 kHz, marked
# for skipping by the container); durations below are of the audible samples. The values after
# "verified" were read back with ffprobe, idet, cropdetect or packet lists on 2026-11-02.
#
# Fixtures (HANDOFF section 9; the section that each one exercises is in brackets):
#
# MPEG-4 ASP, re-encode path [3]
#   xvid_avi_mp3.avi                 MPEG-4 ASP (-vtag XVID) in AVI, MP3, 320x240 25 fps, no B-frames
#   xvid_avi_bframes_mp3.avi         same with 2 B-frames (unpacked; see "not producible" below)
#
# H.264, remux path [3, 2.4]
#   h264_high_mkv_aac.mkv            H.264 High in MKV, AAC stereo: the plain remux candidate
#   h264_flv_aac.flv                 H.264 in FLV, AAC
#   h264_avi_mp3.avi                 H.264 in AVI, MP3
#   h264_mp4_aac.mp4                 H.264 already in .mp4: the interim-name path (2.4)
#   h264_long_gop_mkv_aac.mkv        GOP 250 at 29.97 fps, scene cuts off, 12 s long so that a
#                                    second keyframe exists; verified keyframes at 0 and 8.342 s
#   h264_gop_2s_mkv_aac.mkv          GOP 50 at 25 fps, 6 s; verified keyframes at 0, 2, 4 s
#
# Flash and Windows Media [3, 4.8]
#   flv1_flv_mp3.flv                 Sorenson Spark (flv1) in FLV, MP3 44.1 kHz
#   flv1_swf_mp3.swf                 flv1 in SWF, MP3 44.1 kHz: an SWF with a video stream (4.8)
#   flv1_flv_mp3_damaged.flv         copy of flv1_flv_mp3.flv with 64 bytes of 0xFF written at one
#                                    third, one half and two thirds of the file (4.1 decode check);
#                                    probes like the good file, decode reports corrupt frames
#   wmv2_asf_wmav2.wmv               WMV2 in ASF, WMAv2
#
# MPEG-2 in MPEG-PS (DVD muxer, AC-3 audio) [4.2, 4.3, 4.5, 4.6]
#   mpeg2_ps_ntsc_dar_4x3_ac3.mpg    720x480 29.97 fps; verified SAR 8:9, DAR 4:3, progressive
#   mpeg2_ps_ntsc_dar_16x9_ac3.mpg   720x480 29.97 fps; verified SAR 32:27, DAR 16:9
#   mpeg2_ps_pal_576_25fps_ac3.mpg   720x576 25 fps; verified SAR 16:15, DAR 4:3
#   mpeg2_ps_interlaced_tff_ac3.mpg  720x480, real fields from tinterlace (59.94p woven to
#                                    29.97i), interlaced DCT and ME; verified field_order tt,
#                                    idet TFF on 150 of 150 frames
#   mpeg2_ps_telecined_ac3.mpg       23.976p woven to 29.97 by the telecine filter (3:2 pulldown,
#                                    hard telecine); verified field_order tt, idet repeated
#                                    fields top 28, bottom 28 of 150
#   vob_fragments_no_ifo/            VTS_01_1.VOB and VTS_01_2.VOB: one 6 s DVD-muxed clip split
#                                    at a 2048-byte sector boundary, as loose VOBs are, no IFO (4.6);
#                                    the second part alone starts mid-GOP, the two concatenated
#                                    decode with no warnings
#
# Interlaced HD [4.3, level 4.2 path]
#   h264_1080i_tff_ts_ac3.ts         1920x1080 H.264 interlaced (PAFF/MBAFF from x264), top field
#                                    first, 29.97 frames/s from 59.94p via tinterlace, AC-3, 4 s;
#                                    verified High level 4.0, field_order tt, idet TFF 120 of 120
#
# DV [3, 4.2]
#   dv_avi_ntsc_pcm.avi              DV (dvvideo) 720x480 yuv411p 29.97 fps in AVI, PCM s16le, 4 s
#                                    (DV is a fixed 25 Mb/s, so this is the largest fixture)
#
# Palette and RLE content [4.7]
#   palette_msrle_avi_320x240.avi    Microsoft RLE, pal8, 320x240: 8-bit palette AVI
#   palette_msrle_avi_160x120.avi    Microsoft RLE, pal8, 160x120: the pixel-art upscale rule;
#                                    verified pix_fmt pal8
#   qtrle_mov_rgb24_320x240.mov      QuickTime Animation (qtrle) rgb24 in MOV
#   msvideo1_avi_rgb555_320x240.avi  Microsoft Video 1 (rgb555) in AVI
#
# Audio variants, H.264 video in MKV [2.2]
#   h264_mkv_pcm_s16le.mkv           PCM s16le stereo
#   h264_mkv_ac3_stereo.mkv          AC-3 stereo
#   h264_mkv_ac3_5point1.mkv         AC-3 5.1 (448 kb/s)
#   h264_mkv_vorbis.mkv              Vorbis stereo
#   h264_mkv_opus.mkv                Opus stereo
#
# Sync [4.4, 2.2]
#   h264_mkv_aac_offset_300ms.mkv    audio input shifted with -itsoffset 0.3 (audio starts 0.3 s
#                                    after the video); verified audio start_time 0.279, which is
#                                    0.300 after the 0.021 s priming
#   h264_mkv_aac_drift_long.mkv      video 5.12 s (128 frames at 25 fps), AAC audio about 1 %
#                                    longer: sync-drift, re-encode for sync; verified audio ends at
#                                    5.184 s, ratio 1.0125
#   h264_mkv_aac_drift_within.mkv    video and audio both 5.12 s (a whole number of 1024-sample
#                                    AAC frames at 48 kHz and of video frames): must be copied;
#                                    verified audio ends at 5.120 s, ratio 1.000
#   h264_mkv_vfr_aac.mkv             variable frame rate: 2.5 s at 15 fps then 2.5 s at 30 fps,
#                                    joined with the concat demuxer; verified 38 intervals of
#                                    0.067 s and 74 of 0.033 s
#
# Aspect [4.2]
#   letterbox_720x480_sar_1x1.mkv    720x360 picture padded to 720x480 with black bars, SAR 1:1;
#                                    verified DAR 3:2, cropdetect 720:360:0:60 on 143 of 144
#   letterbox_720x480_sar_8x9.mkv    the same picture with SAR 8:9; verified DAR 4:3
#
# Subtitles and chapters [2.3]
#   h264_mkv_srt_subtitles.mkv       two-cue SubRip track
#   h264_mkv_ass_subtitles.mkv       the same two cues as an ASS track
#   h264_mkv_chapters.mkv            two chapters ("Opening" 0-2.5 s, "Ending" 2.5-5 s)
#
# Collision [2.4]
#   collision_pair/movie.avi         MPEG-4 ASP and MP3, and
#   collision_pair/movie.mkv         H.264 and AAC: two sources sharing a stem (collision hold)
#
# Not producible with ffmpeg (printed as SKIP lines on every run):
#   - MPEG-4 ASP with packed B-frames: no ffmpeg encoder writes the packed (DivX 5) layout; ffmpeg
#     only has the mpeg4_unpack_bframes filter going the other way. xvid_avi_bframes_mp3.avi
#     carries ordinary B-frames instead.
#   - SWF with no video stream (vector/ActionScript animation, 4.8 swf-vector): the swf muxer
#     only wraps flv1/mjpeg video and MP3 audio, it cannot write a vector movie.
#   - FLIC (.fli/.flc): ffmpeg has no FLIC encoder; a real file goes under tests/data/.
#   - A VIDEO_TS tree with VTS_01_0.IFO: IFO files cannot be synthesised; the loose VOB
#     fragments above cover the no-IFO path.

set -eu

VERSION="20261102"

FFMPEG="${FFMPEG:-ffmpeg}"

script_dir=$(cd "$(dirname "$0")" && pwd)
repo_root=$(dirname "$script_dir")
out_dir="$repo_root/tests/fixtures"
force=0

usage() {
    printf 'Usage: %s [--force] [OUTPUT_DIR]\n' "$0"
    printf 'Synthesises the vidfunnel test fixtures with ffmpeg (FFMPEG=%s).\n' "$FFMPEG"
}

out_given=0
for arg in "$@"; do
    case "$arg" in
        --force) force=1 ;;
        -h|--help) usage; exit 0 ;;
        --version) printf 'make_fixtures.sh %s\n' "$VERSION"; exit 0 ;;
        -*) printf "Unknown option: '%s'\n" "$arg" >&2; usage >&2; exit 2 ;;
        *)
            if [ "$out_given" -eq 1 ]; then
                printf "Only one output directory may be given (second one: '%s')\n" "$arg" >&2
                exit 2
            fi
            out_dir="$arg"
            out_given=1
            ;;
    esac
done

if ! command -v "$FFMPEG" >/dev/null 2>&1; then
    printf "ffmpeg not found: '%s' (set FFMPEG or put ffmpeg on PATH)\n" "$FFMPEG" >&2
    exit 3
fi

mkdir -p "$out_dir"
out_dir=$(cd "$out_dir" && pwd)
work_root="$out_dir/.make_fixtures_work.$$"
rm -rf "$work_root"
mkdir -p "$work_root"
trap 'rm -rf "$work_root"' EXIT
trap 'exit 130' INT TERM

count_made=0
count_existing=0
count_failed=0
count_unproducible=0
start_seconds=$(date +%s)

printf "Writing fixtures to '%s' with '%s'\n" "$out_dir" "$(command -v "$FFMPEG")"

# Common pieces. Every ffmpeg call is quiet except for errors, which go to the per-fixture log.
ff() {
    "$FFMPEG" -hide_banner -nostdin -loglevel error -y "$@"
}

video_source() {
    # size rate duration
    printf 'testsrc=size=%s:rate=%s:duration=%s' "$1" "$2" "$3"
}

audio_source() {
    # sample_rate duration
    printf 'sine=frequency=440:beep_factor=4:sample_rate=%s:duration=%s' "$1" "$2"
}

x264_small="-c:v libx264 -preset veryfast -crf 26 -profile:v high -pix_fmt yuv420p"

# make_fixture NAME FUNCTION
# Runs FUNCTION STAGED WORKDIR, where STAGED is the path the function must create (a file, or a
# directory for multi-file fixtures). The result is moved into place only when the function
# succeeds, so an interrupted or failed run never leaves a half-written fixture behind.
make_fixture() {
    fixture_name="$1"
    fixture_function="$2"
    target="$out_dir/$fixture_name"
    if [ -e "$target" ] && [ "$force" -eq 0 ]; then
        printf "exists   '%s'\n" "$fixture_name"
        count_existing=$((count_existing + 1))
        return 0
    fi
    work="$work_root/work"
    rm -rf "$work"
    mkdir -p "$work"
    staged="$work/staged_$(basename "$fixture_name")"
    if "$fixture_function" "$staged" "$work" >"$work/fixture.log" 2>&1 && [ -e "$staged" ]; then
        rm -rf "$target"
        mkdir -p "$(dirname "$target")"
        mv "$staged" "$target"
        if [ -d "$target" ]; then
            size_text="$(du -sk "$target" | cut -f1) KiB"
        else
            size_text="$(( $(wc -c <"$target") / 1024 )) KiB"
        fi
        printf "made     '%s' (%s)\n" "$fixture_name" "$size_text"
        count_made=$((count_made + 1))
    else
        reason=$(grep -v '^[[:space:]]*$' "$work/fixture.log" | tail -n 1)
        [ -n "$reason" ] || reason="no output produced"
        printf "SKIP     '%s': %s\n" "$fixture_name" "$reason"
        count_failed=$((count_failed + 1))
    fi
    return 0
}

not_producible() {
    printf "SKIP     '%s': not producible with ffmpeg: %s\n" "$1" "$2"
    count_unproducible=$((count_unproducible + 1))
}

# Fixture functions. Each takes STAGED (output path) and WORKDIR. They run inside an `if`, where
# `set -e` does not apply, so multi-step functions chain their steps with && or `|| return 1`.

fixture_xvid_avi_mp3() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 44100 5)" \
        -c:v mpeg4 -vtag XVID -q:v 5 -bf 0 -c:a libmp3lame -b:a 128k -ac 2 \
        -f avi "$1"
}

fixture_xvid_avi_bframes_mp3() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 44100 5)" \
        -c:v mpeg4 -vtag XVID -q:v 5 -bf 2 -c:a libmp3lame -b:a 128k -ac 2 \
        -f avi "$1"
}

fixture_h264_high_mkv_aac() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 48000 5)" \
        $x264_small -c:a aac -b:a 128k -ac 2 -f matroska "$1"
}

fixture_h264_flv_aac() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 44100 5)" \
        $x264_small -c:a aac -b:a 128k -ac 2 -f flv "$1"
}

fixture_h264_avi_mp3() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 44100 5)" \
        $x264_small -c:a libmp3lame -b:a 128k -ac 2 -f avi "$1"
}

fixture_h264_mp4_aac() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 48000 5)" \
        $x264_small -c:a aac -b:a 128k -ac 2 -f mp4 "$1"
}

fixture_h264_long_gop() {
    ff -f lavfi -i "$(video_source 320x240 30000/1001 12)" -f lavfi -i "$(audio_source 48000 12)" \
        $x264_small -g 250 -keyint_min 250 -sc_threshold 0 \
        -c:a aac -b:a 128k -ac 2 -f matroska "$1"
}

fixture_h264_gop_2s() {
    ff -f lavfi -i "$(video_source 320x240 25 6)" -f lavfi -i "$(audio_source 48000 6)" \
        $x264_small -g 50 -keyint_min 50 -sc_threshold 0 \
        -c:a aac -b:a 128k -ac 2 -f matroska "$1"
}

fixture_flv1_flv_mp3() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 44100 5)" \
        -c:v flv -q:v 5 -c:a libmp3lame -b:a 128k -ac 2 -f flv "$1"
}

fixture_flv1_swf_mp3() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 44100 5)" \
        -c:v flv -q:v 5 -c:a libmp3lame -b:a 128k -ac 2 -f swf "$1"
}

fixture_flv1_flv_damaged() {
    good="$out_dir/flv1_flv_mp3.flv"
    if [ ! -s "$good" ]; then
        echo "needs '$good', which was not made"
        return 1
    fi
    cp "$good" "$1" || return 1
    pattern="$2/damage_pattern.bin"
    : >"$pattern"
    byte_count=0
    while [ "$byte_count" -lt 64 ]; do
        printf '\377' >>"$pattern"
        byte_count=$((byte_count + 1))
    done
    file_size=$(wc -c <"$1")
    for offset in $((file_size / 3)) $((file_size / 2)) $((file_size * 2 / 3)); do
        dd if="$pattern" of="$1" bs=1 seek="$offset" count=64 conv=notrunc 2>/dev/null || return 1
    done
    [ "$(wc -c <"$1")" -eq "$file_size" ]
}

fixture_wmv2_asf_wmav2() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 44100 5)" \
        -c:v wmv2 -b:v 600k -c:a wmav2 -b:a 128k -ac 2 -f asf "$1"
}

# DVD-style MPEG-PS: the dvd muxer writes 2048-byte packs and carries AC-3 as private stream 1.
mpeg2_dvd_options="-c:v mpeg2video -b:v 4000k -maxrate 8000k -bufsize 1835k -g 15 -bf 2 -pix_fmt yuv420p -c:a ac3 -b:a 192k -ac 2 -ar 48000 -f dvd -muxrate 10080000 -packetsize 2048"

fixture_mpeg2_ntsc_4x3() {
    ff -f lavfi -i "$(video_source 720x480 30000/1001 5)" -f lavfi -i "$(audio_source 48000 5)" \
        -aspect 4:3 $mpeg2_dvd_options "$1"
}

fixture_mpeg2_ntsc_16x9() {
    ff -f lavfi -i "$(video_source 720x480 30000/1001 5)" -f lavfi -i "$(audio_source 48000 5)" \
        -aspect 16:9 $mpeg2_dvd_options "$1"
}

fixture_mpeg2_pal_576() {
    ff -f lavfi -i "$(video_source 720x576 25 5)" -f lavfi -i "$(audio_source 48000 5)" \
        -aspect 4:3 $mpeg2_dvd_options "$1"
}

fixture_mpeg2_interlaced_tff() {
    ff -f lavfi -i "$(video_source 720x480 60000/1001 5)" -f lavfi -i "$(audio_source 48000 5)" \
        -vf "tinterlace=mode=interleave_top,setfield=tff" -flags +ilme+ildct -field_order tt \
        -aspect 4:3 $mpeg2_dvd_options "$1"
}

fixture_mpeg2_telecined() {
    ff -f lavfi -i "$(video_source 720x480 24000/1001 5)" -f lavfi -i "$(audio_source 48000 5)" \
        -vf "telecine=first_field=top:pattern=23,setfield=tff" -flags +ilme+ildct -field_order tt \
        -aspect 4:3 $mpeg2_dvd_options "$1"
}

fixture_vob_fragments() {
    whole="$2/whole.vob"
    ff -f lavfi -i "$(video_source 720x480 30000/1001 6)" -f lavfi -i "$(audio_source 48000 6)" \
        -aspect 4:3 $mpeg2_dvd_options "$whole" || return 1
    sector_count=$(( $(wc -c <"$whole") / 2048 ))
    first_half=$((sector_count / 2))
    mkdir -p "$1" || return 1
    dd if="$whole" of="$1/VTS_01_1.VOB" bs=2048 count="$first_half" 2>/dev/null || return 1
    dd if="$whole" of="$1/VTS_01_2.VOB" bs=2048 skip="$first_half" 2>/dev/null || return 1
    [ "$(cat "$1/VTS_01_1.VOB" "$1/VTS_01_2.VOB" | wc -c)" -eq "$(wc -c <"$whole")" ]
}

fixture_h264_1080i() {
    ff -f lavfi -i "$(video_source 1920x1080 60000/1001 4)" -f lavfi -i "$(audio_source 48000 4)" \
        -vf "tinterlace=mode=interleave_top,setfield=tff" \
        -c:v libx264 -preset veryfast -crf 30 -profile:v high -pix_fmt yuv420p \
        -flags +ilme+ildct -x264-params tff=1 \
        -c:a ac3 -b:a 192k -ac 2 -f mpegts "$1"
}

fixture_dv_avi() {
    ff -f lavfi -i "$(video_source 720x480 30000/1001 4)" -f lavfi -i "$(audio_source 48000 4)" \
        -c:v dvvideo -pix_fmt yuv411p -aspect 4:3 -c:a pcm_s16le -ac 2 -f avi "$1"
}

fixture_palette_msrle_320() {
    ff -f lavfi -i "$(video_source 320x240 15 5)" -f lavfi -i "$(audio_source 22050 5)" \
        -c:v msrle -pix_fmt pal8 -c:a pcm_s16le -ac 1 -f avi "$1"
}

fixture_palette_msrle_160() {
    ff -f lavfi -i "$(video_source 160x120 15 5)" -f lavfi -i "$(audio_source 22050 5)" \
        -c:v msrle -pix_fmt pal8 -c:a pcm_s16le -ac 1 -f avi "$1"
}

fixture_qtrle_mov() {
    ff -f lavfi -i "$(video_source 320x240 15 5)" -f lavfi -i "$(audio_source 44100 5)" \
        -c:v qtrle -pix_fmt rgb24 -c:a pcm_s16le -ac 2 -f mov "$1"
}

fixture_msvideo1_avi() {
    ff -f lavfi -i "$(video_source 320x240 15 5)" -f lavfi -i "$(audio_source 22050 5)" \
        -c:v msvideo1 -pix_fmt rgb555le -c:a pcm_s16le -ac 1 -f avi "$1"
}

fixture_audio_pcm() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 48000 5)" \
        $x264_small -c:a pcm_s16le -ac 2 -f matroska "$1"
}

fixture_audio_ac3_stereo() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 48000 5)" \
        $x264_small -c:a ac3 -b:a 192k -ac 2 -f matroska "$1"
}

fixture_audio_ac3_surround() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 48000 5)" \
        $x264_small -af "pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0" \
        -c:a ac3 -b:a 448k -f matroska "$1"
}

fixture_audio_vorbis() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 48000 5)" \
        $x264_small -c:a libvorbis -q:a 4 -ac 2 -f matroska "$1"
}

fixture_audio_opus() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 48000 5)" \
        $x264_small -c:a libopus -b:a 96k -ac 2 -f matroska "$1"
}

fixture_audio_offset() {
    ff -f lavfi -i "$(video_source 320x240 25 5)" -itsoffset 0.3 -f lavfi -i "$(audio_source 48000 5)" \
        -map 0:v -map 1:a $x264_small -c:a aac -b:a 128k -ac 2 -f matroska "$1"
}

fixture_drift_long() {
    ff -f lavfi -i "$(video_source 320x240 25 5.12)" -f lavfi -i "$(audio_source 48000 5.1712)" \
        $x264_small -c:a aac -b:a 128k -ac 2 -f matroska "$1"
}

fixture_drift_within() {
    ff -f lavfi -i "$(video_source 320x240 25 5.12)" -f lavfi -i "$(audio_source 48000 5.12)" \
        $x264_small -c:a aac -b:a 128k -ac 2 -f matroska "$1"
}

fixture_vfr() {
    slow="$2/segment_slow.mkv"
    fast="$2/segment_fast.mkv"
    list="$2/segments.txt"
    ff -f lavfi -i "$(video_source 320x240 15 2.5)" $x264_small -f matroska "$slow" || return 1
    ff -f lavfi -i "$(video_source 320x240 30 2.5)" $x264_small -f matroska "$fast" || return 1
    printf "file '%s'\nfile '%s'\n" "$slow" "$fast" >"$list" || return 1
    ff -f concat -safe 0 -i "$list" -f lavfi -i "$(audio_source 48000 5)" \
        -map 0:v -map 1:a -c:v copy -c:a aac -b:a 128k -ac 2 -f matroska "$1"
}

fixture_letterbox_sar_1x1() {
    ff -f lavfi -i "$(video_source 720x360 30000/1001 5)" -f lavfi -i "$(audio_source 48000 5)" \
        -vf "pad=720:480:0:60:black,setsar=1" $x264_small -c:a aac -b:a 128k -ac 2 -f matroska "$1"
}

fixture_letterbox_sar_8x9() {
    ff -f lavfi -i "$(video_source 720x360 30000/1001 5)" -f lavfi -i "$(audio_source 48000 5)" \
        -vf "pad=720:480:0:60:black,setsar=8/9" $x264_small -c:a aac -b:a 128k -ac 2 -f matroska "$1"
}

write_subtitle_file() {
    printf '1\n00:00:00,500 --> 00:00:02,000\nFirst test cue\n\n2\n00:00:02,500 --> 00:00:04,500\nSecond test cue\n' >"$1"
}

fixture_srt_subtitles() {
    write_subtitle_file "$2/cues.srt" || return 1
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 48000 5)" -i "$2/cues.srt" \
        -map 0:v -map 1:a -map 2:s $x264_small -c:a aac -b:a 128k -ac 2 -c:s srt \
        -metadata:s:s:0 language=eng -f matroska "$1"
}

fixture_ass_subtitles() {
    write_subtitle_file "$2/cues.srt" || return 1
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 48000 5)" -i "$2/cues.srt" \
        -map 0:v -map 1:a -map 2:s $x264_small -c:a aac -b:a 128k -ac 2 -c:s ass \
        -metadata:s:s:0 language=eng -f matroska "$1"
}

fixture_chapters() {
    printf ';FFMETADATA1\ntitle=Chapter test clip\n\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=2500\ntitle=Opening\n\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=2500\nEND=5000\ntitle=Ending\n' >"$2/chapters.txt" || return 1
    ff -f lavfi -i "$(video_source 320x240 25 5)" -f lavfi -i "$(audio_source 48000 5)" -i "$2/chapters.txt" \
        -map 0:v -map 1:a -map_metadata 2 -map_chapters 2 \
        $x264_small -c:a aac -b:a 128k -ac 2 -f matroska "$1"
}

fixture_collision_pair() {
    mkdir -p "$1" || return 1
    fixture_xvid_avi_mp3 "$1/movie.avi" "$2" || return 1
    fixture_h264_high_mkv_aac "$1/movie.mkv" "$2"
}

make_fixture xvid_avi_mp3.avi                fixture_xvid_avi_mp3
make_fixture xvid_avi_bframes_mp3.avi        fixture_xvid_avi_bframes_mp3
make_fixture h264_high_mkv_aac.mkv           fixture_h264_high_mkv_aac
make_fixture h264_flv_aac.flv                fixture_h264_flv_aac
make_fixture h264_avi_mp3.avi                fixture_h264_avi_mp3
make_fixture h264_mp4_aac.mp4                fixture_h264_mp4_aac
make_fixture h264_long_gop_mkv_aac.mkv       fixture_h264_long_gop
make_fixture h264_gop_2s_mkv_aac.mkv         fixture_h264_gop_2s
make_fixture flv1_flv_mp3.flv                fixture_flv1_flv_mp3
make_fixture flv1_swf_mp3.swf                fixture_flv1_swf_mp3
make_fixture flv1_flv_mp3_damaged.flv        fixture_flv1_flv_damaged
make_fixture wmv2_asf_wmav2.wmv              fixture_wmv2_asf_wmav2
make_fixture mpeg2_ps_ntsc_dar_4x3_ac3.mpg   fixture_mpeg2_ntsc_4x3
make_fixture mpeg2_ps_ntsc_dar_16x9_ac3.mpg  fixture_mpeg2_ntsc_16x9
make_fixture mpeg2_ps_pal_576_25fps_ac3.mpg  fixture_mpeg2_pal_576
make_fixture mpeg2_ps_interlaced_tff_ac3.mpg fixture_mpeg2_interlaced_tff
make_fixture mpeg2_ps_telecined_ac3.mpg      fixture_mpeg2_telecined
make_fixture vob_fragments_no_ifo            fixture_vob_fragments
make_fixture h264_1080i_tff_ts_ac3.ts        fixture_h264_1080i
make_fixture dv_avi_ntsc_pcm.avi             fixture_dv_avi
make_fixture palette_msrle_avi_320x240.avi   fixture_palette_msrle_320
make_fixture palette_msrle_avi_160x120.avi   fixture_palette_msrle_160
make_fixture qtrle_mov_rgb24_320x240.mov     fixture_qtrle_mov
make_fixture msvideo1_avi_rgb555_320x240.avi fixture_msvideo1_avi
make_fixture h264_mkv_pcm_s16le.mkv          fixture_audio_pcm
make_fixture h264_mkv_ac3_stereo.mkv         fixture_audio_ac3_stereo
make_fixture h264_mkv_ac3_5point1.mkv        fixture_audio_ac3_surround
make_fixture h264_mkv_vorbis.mkv             fixture_audio_vorbis
make_fixture h264_mkv_opus.mkv               fixture_audio_opus
make_fixture h264_mkv_aac_offset_300ms.mkv   fixture_audio_offset
make_fixture h264_mkv_aac_drift_long.mkv     fixture_drift_long
make_fixture h264_mkv_aac_drift_within.mkv   fixture_drift_within
make_fixture h264_mkv_vfr_aac.mkv            fixture_vfr
make_fixture letterbox_720x480_sar_1x1.mkv   fixture_letterbox_sar_1x1
make_fixture letterbox_720x480_sar_8x9.mkv   fixture_letterbox_sar_8x9
make_fixture h264_mkv_srt_subtitles.mkv      fixture_srt_subtitles
make_fixture h264_mkv_ass_subtitles.mkv      fixture_ass_subtitles
make_fixture h264_mkv_chapters.mkv           fixture_chapters
make_fixture collision_pair                  fixture_collision_pair

not_producible xvid_avi_packed_bframes.avi "no encoder writes packed (DivX 5) B-frames"
not_producible swf_vector_no_video.swf "the swf muxer cannot write a vector movie without video"
not_producible flic_animation.fli "ffmpeg has no FLIC encoder (keep a real file in tests/data/)"
not_producible video_ts_with_ifo "IFO files cannot be synthesised (vob_fragments_no_ifo covers no-IFO)"

elapsed=$(( $(date +%s) - start_seconds ))
printf 'Done in %s s: %s made, %s already present, %s failed, %s not producible with ffmpeg.\n' \
    "$elapsed" "$count_made" "$count_existing" "$count_failed" "$count_unproducible"

if [ "$count_failed" -gt 0 ]; then
    exit 1
fi
exit 0

# End of File #
