# Video-Format-Funnel

A command-line tool (`vidfunnel`) that funnels legacy video files — AVI with any codec, FLV, SWF with embedded video, FLIC, MPEG-1/2 and DVD VOB, WMV/ASF, old QuickTime MOV, RealMedia, MKV, and anything else ffmpeg can decode — into one archival output profile: H.264 High 4.1 and AAC in fragmented MP4, with closed keyframe groups every two seconds and optional PAR2 recovery data. Files that are already H.264 are remuxed, not re-encoded. Originals are never deleted by the conversion itself; a separate `retire` step does that only after verification, and only when asked.

Status: design complete, not yet built. See `HANDOFF.md` for the full specification and `CLAUDE.md` for the working conventions.

Requires Python 3.9 or newer and an ffmpeg build with libx264 and libdvdread. `vidfunnel check` reports what is missing and how to install it on Fedora, openSUSE, Debian/Ubuntu, Arch and macOS. A `Containerfile` is provided for hosts without Python.

License: GPL-3.0-or-later.

# End of File #
