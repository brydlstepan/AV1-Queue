# AV1 Queue

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

Everything here targets a Jellyfin library that direct-plays on the widest range of clients, with server-side transcoding as a rare fallback rather than a routine cost.

### Video codec: AV1

AV1 gives the best compression-efficiency-per-quality of any practical codec today, and hardware decode is broadly available on ~2021+ devices. A client without it falls back to Jellyfin server-side transcoding — the intended safety net, costing CPU time only on the rare incompatible playback, never storage or re-encoding.

### Container: MP4, not MKV

MP4 is the one container every target client — native apps and browsers alike — can direct-play without a translation step. MKV has better native-app support but no browser can demux it, so an MKV library forces a remux or transcode on every web playback. Modern ffmpeg and current browsers handle Opus-in-MP4 and multi-track audio fine, so MP4's historical weaknesses no longer apply.

### Audio: Opus (5.1 default), E-AC-3 optional

Opus beats E-AC-3 on quality at a given bitrate and is royalty-free, so it's the default for both 5.1 and stereo. Its one constraint is being decode-only — no AVR can bitstream-passthrough it — but every modern software player decodes it natively, and the rare client that can't is cheaply covered by a Jellyfin audio-only transcode. The principle throughout: direct-play video always, and accept a cheap audio-only transcode for the odd client rather than picking a worse codec library-wide. `audio_format` (`opus` / `eac3`) is exposed per-preset for setups that rely on AVR passthrough. Standard players downmix 5.1→stereo correctly, so that needs no special handling either.

### HDR & Dolby Vision

The single rule: **can the source's base layer stand on its own?** If yes, encode it and preserve whatever dynamic metadata it carries; if no, leave the original untouched. Dolby Vision is never emitted. The `dv_bl_signal_compatibility_id` ffprobe field answers this — 0 means no, anything else means yes. Profile 7 / 8.1 base layers are already valid HDR10, so the RPU is dropped cleanly. Profile 5's base layer is stored in DV's IPT-PQc2 colourspace, so tagging it HDR10 would bake in a permanent colour cast and there's no cheap conversion — so P5 sources are skipped and kept as-is. DV over AV1 would require Profile 10, which almost nothing plays today, and DV metadata cannot be converted to HDR10+, so the untouched P5 original stays the best-preserved artifact.

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

#### HDR10+ passthrough

HDR10+ can be carried through but not created. It needs both a libhdr10plus-enabled SVT-AV1-Tritium binary (`--hdr10plus-json`, shipped by default in Tritium's Windows releases) and the HDR10+ JSON extracted by `hdr10plus_tool` from frame SEI. If either is missing, an HDR10+ source encodes as plain HDR10 (or fails preflight when "Fail when HDR metadata cannot be preserved" is on).

#### Sourcing guidance

Prefer at acquisition, best first: HDR10+ (passes through intact) → DV P7 / P8.1 (base layer drops cleanly to HDR10) → HDR10 / HLG → SDR → DV P5 (last resort; skipped and left in its original form).

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
