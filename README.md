# AV1 Queue Studio

Local **AV1 encoding queue** with a browser UI. Wraps [SVT-AV1-Tritium](https://github.com/Uranite/svt-av1-tritium) direct single-pass encoding into a FastAPI backend plus a dark web frontend for batch jobs, presets, audio selection, HDR/DoVi handling, and live progress.

---

## Features

- **Single-pass encode** — VapourSynth source load/crop/resize → direct SVT-AV1-Tritium encode (no fast pass, no metrics stage, no CRF zones)
- **Queue studio** — Drag/drop or browse files, reorder, start/stop, edit queued jobs, reset cancelled/failed jobs
- **Presets** — CRF/preset/tune, Opus bitrates, SVT-AV1-Tritium advanced params (non-defaults → `--svt-params`)
- **Audio** — Preferred languages + best-track-only in presets; Opus 5.1 (incl. 7.1 downmix) / stereo; video-only if none selected
- **HDR / Dolby Vision** — See [Format philosophy → HDR & Dolby Vision](#hdr--dolby-vision) (HDR10 / HLG / HDR10+ passthrough; DoVi base layer → HDR10; P5 skipped; P4 / unconfirmed filename-DoVi / unknown profile-compat quarantined)

- **Test mode** — Short time-range encodes for quick preset checks
- **Pipeline options** — SSIMU2 CPU/GPU/auto, autocrop, optional post-encode score, background subtitle extract after encode
- **Live UI** — WebSocket progress, stage stepper, logs, CPU/GPU/RAM gauges
- **Output** — Web-optimized MP4 (`+faststart`) or WebM; chapters/subtitles stripped

---

## Tech stack

| Layer | Stack |
|--------|--------|
| UI | Vanilla HTML / CSS / JS (`server/static/`) |
| API | **FastAPI** + **Uvicorn**, REST + WebSocket (`/ws/live`) |
| Queue | Python `QueueManager` (`core/queue_manager.py`) |
| Encode | **SvtAv1EncApp** (SVT-AV1-Tritium), direct single-pass |
| Video filter graph | **VapourSynth** + **FFMS2**, **vszip** (CPU metrics), **Vship** (NVIDIA GPU metrics) |
| Mux / audio / probe | **FFmpeg** / **ffprobe** (libopus) |
| HDR10+ passthrough | **hdr10plus_tool** (quietvoid) extracts the HDR10+ JSON; SVT-AV1-Tritium injects it via `--hdr10plus-json` (HDR10+ and Dolby Vision RPU passthrough both ship enabled by default in Tritium's prebuilt Windows binaries). Dolby Vision is detected but never emitted |
| System metrics | **psutil** |

Python packages (installed into `vs/python-env/` by setup from `requirements.txt`): `fastapi`, `uvicorn[standard]`, `websockets`, `psutil`, `vapoursynth`, `vstools`, `vsjetpack`, `rich`, `py7zr`, …

---

## Requirements

- **Windows** x64 (scripts and portable VS layout assume this)
- **Python 3.12+** on PATH (for first-time venv creation)
- Enough disk for temp job folders under `_temp/`
- **NVIDIA GPU** recommended for Vship-accelerated post-encode SSIMU2 scoring; CPU (vszip) works without it

---

## Quick start

### 1. Install / update environment

Double-click **`setup.bat`** in the project folder (keeps a console open so you can see progress and errors).

Or from PowerShell / cmd:

```cmd
setup.bat
```

```powershell
.\scripts\setup.ps1
```

This runs `scripts/setup_env.py`, which downloads missing binaries and VapourSynth plugins, creates `vs/python-env`, and installs/updates Python deps. Re-run anytime: existing binaries/plugins are left alone (delete them to force a re-download); pip packages are refreshed each run. Requires **Python 3.12+** on PATH (or the Windows `py` launcher).

### 2. Launch the studio

```cmd
runGUI.bat
```

Starts the server as a background process with a **system tray icon** — no console window, not shown on the taskbar. Right-click the tray icon for Open Studio / Start Queue / Pause/Resume Queue / View Log / Stop Server. On first launch it opens **http://localhost:8765** in your browser automatically; if the server is already running, it just reuses it. Binds to **127.0.0.1** only. Server output goes to `logs/server.log`.

Or run it in the foreground with a visible console (for debugging — live log output, Ctrl+C to stop):

```powershell
.\scripts\start_queue.ps1
```

---

## Directory layout

```
AV1-Queue/
├── setup.bat                  # Double-click installer (console stays open)
├── runGUI.bat                 # Launch server as a background tray-icon process
├── bin/                       # ffmpeg, ffprobe, hdr10plus_tool; SVT under bin/svt/
├── vs/
│   ├── portable.vs
│   ├── python-env/            # Isolated Python venv (VS plugins also autoload here)
│   └── plugins64/             # Plugin copies for PATH / DLL resolution
├── core/
│   ├── svt_encode.py
│   ├── pipeline.py            # Probe, audio, test segment, mux, cleanup
│   ├── queue_manager.py       # Job lifecycle & encode orchestration
│   ├── hdr_dovi.py            # HDR / Dolby Vision helpers
│   └── measure_ssimu2.py
├── server/
│   ├── app.py                 # FastAPI routes & WebSocket
│   ├── static/                # Queue UI (index.html, app.js, app.css)
│   ├── presets/               # Quality presets (builtin/ tracked, local/ gitignored)
│   │   ├── builtin/           # Shared / shipped presets (one JSON file each)
│   │   └── local/             # UI-created presets (not tracked)
│   ├── queue.json             # Persisted active queue (local)
│   └── settings.json          # API keys / feature toggles (local)
├── history/                   # Finished encode records
├── _temp/                     # Per-job working directories
└── scripts/
    ├── setup.ps1              # Called by setup.bat
    ├── setup_env.py           # Download binaries, plugins, venv
    ├── start_queue.ps1        # Foreground launcher (visible console, for debugging)
    ├── tray_launcher.ps1      # Background launcher used by runGUI.bat (tray icon)
    └── launch_hidden.vbs      # Runs tray_launcher.ps1 with zero window flash
```

---

## Encode stages

1. **Audio** — Selected tracks → Opus by default, or E-AC-3 when the preset sets `audio_format` (or skipped for video-only)
2. **Encode** — VapourSynth loads the source (**ffms2**), applies optional crop/resize, and pipes frames directly to **SvtAv1EncApp** (`--preset` / `--crf`, plus `--svt-params` for tune and other non-defaults; single pass, no fast pass, no metrics, no CRF zones)
3. **Mux** — IVF + audio → MP4 (`+faststart`) or WebM; HDR10 static color tags applied via FFmpeg; HDR10+ dynamic metadata carried in the AV1 bitstream
4. **Subtitles (optional)** — Text tracks extracted from the **original** file next to it (`.en.srt`, etc.); runs in a **background thread** so the next queue job can start immediately
5. **SSIMU2 (optional)** — Post-mux quality score via Vship (GPU) or vszip (CPU), when enabled in settings

---

## Format philosophy

Why AV1, MP4, and Opus specifically — the tradeoffs considered and rejected, for a
Jellyfin library that has to keep playing correctly as client hardware changes over
the next several years.

### Video codec: AV1

Best compression-efficiency-per-quality of any codec practical today, and the
direction both the industry and hardware decoders are moving. Hardware AV1 decode is
broadly available on ~2021+ silicon (Apple TV 4K 3rd-gen+, most 2021+ smart TVs,
RDNA2+/Intel 11th-gen+/RTX 30-series+ GPUs). An older or weaker client that lacks it
falls back to Jellyfin server-side transcoding — that's the intended safety net, not
something to design the library around; it only costs CPU/GPU time on the rare
incompatible playback, not storage or re-encoding.

### Container: MP4, not MKV

MKV is the de facto standard for AV1+HDR rips and has the best native-app / TV-app
compatibility (Kodi, Android/iOS/tvOS apps, desktop players — all use their own
demuxers). But **no major browser can demux MKV** via `<video>`/MSE, so Jellyfin Web
always has to remux or transcode the container regardless of what codecs are inside
it — an MKV library forces a browser-side conversion step on every web playback.

MP4 lets both native apps *and* the browser direct-play the same file. The
historical downside — weaker multi-audio-track/subtitle support and shakier
Opus-in-MP4 muxing than MKV — isn't a real trap in practice: modern ffmpeg muxing and
current browsers/players handle it fine (`audio/mp4; codecs=opus` has been supported
in Chrome/Firefox for years). MP4 is the pick because it's the one container every
target client — web included — can direct-play without a translation step.

### Audio: Opus (5.1 default), E-AC-3 as an explicit opt-out

Opus at a given bitrate beats E-AC-3 on quality, is royalty-free, and is what this
pipeline defaults to for both 5.1 and stereo. The one real constraint: Opus is
**decode-only** — no AVR/soundbar can bitstream-passthrough it, the *playing device*
has to decode it to PCM first. That's a non-issue for essentially every modern
software player (Kodi, Jellyfin's own desktop client via mpv, Android apps via
ExoPlayer all decode Opus natively) — a device that can't decode Opus at all is rare
enough, and cheap enough for Jellyfin to fix with an audio-only transcode, that it
isn't worth compromising the whole library's audio codec for.

That's the general rule this pipeline follows: **direct-play video always; let
Jellyfin do a cheap audio-only transcode for the rare client that can't handle the
audio codec, rather than picking a worse codec library-wide to avoid it.** Video
transcoding is the expensive operation everything here is built to avoid; audio
transcoding is not. `audio_format` (`opus` / `eac3`) is exposed per-preset for
setups that specifically rely on AVR bitstream passthrough instead of client-side
decode.

5.1→stereo downmixing on 2-channel outputs is handled correctly by standard
OS/browser/player audio stacks (ITU-R BS.775 coefficients) — not something worth
avoiding Opus or 5.1 over.

### HDR & Dolby Vision

The one rule: **can the source's base layer stand on its own?** If yes, encode it and
preserve whatever dynamic metadata it carries. If no, don't encode it — keep the
original. Only Dolby Vision **Profile 5** answers "no". **Dolby Vision is never
emitted.**

The source answers this directly in one ffprobe field, `dv_bl_signal_compatibility_id`:
**0 means no, anything else means yes.** For P7/P8.1 the base layer is already a valid
HDR10 picture, so discarding the RPU is free and correct. P5's base layer is stored in
DV's IPT-PQc2 colourspace — dropping the RPU and tagging the result HDR10 gets it
decoded as ordinary YCbCr, producing a permanent green/purple cast. There is no cheap
fix: profile 8.1 is *definitionally* "profile 5's RPU plus an HDR10-compatible base
layer," so if a P5 file's base layer were usable as HDR10, the file would already be
P8.1. A correct conversion needs a DV-aware renderer (e.g. ffmpeg's `libplacebo` filter
with a Vulkan GPU) as a deliberate, separate pre-processing step — out of scope for
this pipeline, which orchestrates specialist tools rather than doing HDR/colour work
itself.

| Source | Base layer standalone? | Output |
|--------|------------------------|--------|
| SDR (BT.709) | yes | SDR AV1 |
| HLG | yes | HLG AV1 (ARIB B67 transfer preserved) |
| HDR10 (static) | yes | HDR10 AV1 (MDL + MaxCLL/MaxFALL passed to SVT) |
| HDR10+ | yes | **HDR10+ AV1** (`--hdr10plus-json` passthrough) |
| DoVi P8.1 / P7 | yes | HDR10 AV1 — base layer encoded, **RPU discarded** |
| DoVi P8.1 / P7 + HDR10+ | yes | HDR10+ AV1 — RPU discarded, HDR10+ JSON kept |
| DoVi **P5** (compat 0) | **no** | **skipped** — original file left untouched |
| DoVi P4 (legacy) | unknown | **quarantined** for manual review |
| Filename says DoVi, not probe-confirmed | unknown | **quarantined** — P5 cannot be ruled out |
| DoVi confirmed, profile/compat unknown | unknown | **quarantined** for manual review |

#### HDR10+ passthrough — the one dynamic-metadata path kept

HDR10+ is **passthrough-only**: it can be carried through but not created. It needs two
halves, both or neither:

1. A libhdr10plus-enabled SVT-AV1-Tritium binary (`--hdr10plus-json`). Tritium's
   prebuilt Windows Release assets ship with this compiled in by default — no
   special build needed. `scripts/setup_env.py` downloads the latest Windows
   release if the vendored `bin/svt/` binaries are missing, honours a
   `SVT_TRITIUM_URL` override, and warns if the installed binary lacks the flag.
2. The HDR10+ JSON, extracted by `hdr10plus_tool`. HDR10+ lives in frame SEI, so the
   pipeline samples frames (not just stream side-data) to detect it.

If either half is missing, an HDR10+ source encodes as plain HDR10 (or fails preflight
when "Fail when HDR metadata cannot be preserved" is on).

> **Why no Dolby Vision output?** Dolby Vision profiles are codec-bound — DV **Profile
> 10** is the only one that runs on AV1, and TV support for it is essentially nonexistent
> today: Apple has the strongest silicon story (Profile 10 hardware from A17 Pro / M3),
> no TV manufacturer publicly documents Profile 10 for local file playback, and player
> apps (Infuse, Kodi, Plex/Jellyfin) only have open feature requests, not shipped support.
> The strongest evidence: **Netflix**, which holds professional DV masters and has more
> device-certification leverage than anyone, ships **zero frames of Dolby Vision over
> AV1** — DV stays on their HEVC ladder, while their AV1 ladder pairs with **HDR10+**
> instead (driven by Samsung, whose TVs skip DV but back HDR10+). DV → HDR10+ conversion
> is also impossible outright: DV's RPU is a *display-referred* model (per-frame reshape
> coefficients, per-target-display trims) while HDR10+ is a *content-referred* statistical
> model (luminance percentiles, Bezier tone-curve anchors) — the fields don't map, and no
> tool performs this conversion. Keeping the untouched P5 original is the future-proofing:
> a P5 file re-encoded to AV1 with the RPU retained is profile 10.0 (non-backward-compatible,
> plays on almost nothing, no HDR10 fallback), so the artifact worth holding onto is the
> source, not a lossy 2026 re-encode.

#### Sourcing guidance

P5 is a dead end for this pipeline — prefer it least at acquisition time, in order:

1. **HDR10+** — top tier, passes through intact
2. **DV P7 or P8.1** — base layer is already valid HDR10, drops cleanly
3. **HDR10 / HLG** — encodes correctly, no special handling
4. **SDR** — trivial
5. **DV P5** — last resort; will be skipped and stay un-unified

Where a title exists only as P5, it stays in its original form — partial unification is
the correct outcome, not a failure.

#### References

- [Uranite/svt-av1-tritium — Releases](https://github.com/Uranite/svt-av1-tritium/releases) (prebuilt Windows binaries with HDR10+ / DoVi RPU passthrough)
- [AOM — HDR10+ AV1 Metadata Handling Specification](https://aomediacodec.github.io/av1-hdr10plus/)
- [quietvoid/dovi_tool issue #23 — DV RPU → HDR10+ (never implemented)](https://github.com/quietvoid/dovi_tool/issues/23)
- [Netflix TechBlog — HDR10+ Now Streaming](https://netflixtechblog.com/hdr10-now-streaming-on-netflix-c9ab1f4bd72b)
- [Dolby — Introduction to Profile 10](https://professionalsupport.dolby.com/s/article/Introduction-to-Dolby-Vision-Profile-10)
- [gyan.dev FFmpeg builds — essentials vs full](https://www.gyan.dev/ffmpeg/builds/) (libplacebo is full-build-only)

---

## Upstream & resources

| Project | Role | Link |
|---------|------|------|
| SVT-AV1-Tritium | AV1 encoder (direct single-pass, HDR10+/DoVi RPU passthrough) | [Uranite/svt-av1-tritium](https://github.com/Uranite/svt-av1-tritium) |
| hdr10plus_tool | HDR10+ JSON extract / verify | [quietvoid/hdr10plus_tool](https://github.com/quietvoid/hdr10plus_tool) |
| FFmpeg | Decode, Opus, mux | [GyanD/codexffmpeg](https://github.com/GyanD/codexffmpeg) (essentials build used by setup) |
| Vship | GPU SSIMULACRA2 | [Line-fr/Vship](https://codeberg.org/Line-fr/Vship) |
| vapoursynth-zip (vszip) | CPU metrics | [dnjulek/vapoursynth-zip](https://github.com/dnjulek/vapoursynth-zip) |
| FFMS2 | Source filter for VS | [FFMS/ffms2](https://github.com/FFMS/ffms2) |
| VapourSynth / vstools | Scripting / helpers | [vapoursynth](https://www.vapoursynth.com/), [vsjetpack](https://github.com/Jaded-Encoding-Thaumaturgy/vs-jetpack) |

`scripts/setup_env.py` pulls current Windows assets from these release APIs where possible.

---

## Configuration notes

- **Presets** — Individual JSON files under `server/presets/builtin/` (git-tracked) and `server/presets/local/` (UI-created, gitignored). Each has `crf` / `preset` plus advanced SVT overrides as `svt_params`, merged into the `--svt-params` passed to `core/svt_encode.py`. Built-in presets are read-only in the UI unless **Settings → App → Enable built-in preset edits** is on. The preset editor shows a live **command preview** of non-default SVT flags.
- **Queue** — Survives restarts via `server/queue.json`. Cancelled/failed jobs can be **Reset** back to queued.
- **Test mode** — Global toggle + duration settings in the UI; jobs can run a short trim instead of the full file.
- **Audio defaults** — On add, empty selection falls back to English/Czech heuristics. Explicit empty selection in the job editor means video-only.
- **HDR** — See [Format philosophy → HDR & Dolby Vision](#hdr--dolby-vision).

---

## Development

Server entrypoint (as used by the launchers): `uvicorn server.app:app --host 127.0.0.1 --port 8765` (see `scripts/start_queue.ps1` for PATH/`PYTHONPATH`). Localhost only by default.

Useful API surface (non-exhaustive): `/api/queue`, `/api/queue/add`, `/api/queue/update`, `/api/queue/requeue`, `/api/presets`, `/api/probe`, `/api/system`, `/api/history`, WebSocket `/ws/live`.

Static UI has no build step — edit `server/static/*` and refresh the browser.
