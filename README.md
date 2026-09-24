# AV1 Queue

Local **AV1 encoding queue** with a browser UI. Drives [HandBrakeCLI built with SVT-AV1-Tritium](https://github.com/Uranite/HandBrake-SVT-AV1-Tritium) (single-pass) from a FastAPI backend plus a dark web frontend for batch jobs, presets, audio selection, HDR/DoVi handling, and live progress.

---

## Features

- **Single-pass encode** — HandBrakeCLI with SVT-AV1-Tritium does crop/resize, encode, HDR10+ / Dolby Vision passthrough, audio and mux in one process (no fast pass, no metrics stage, no CRF zones)
- **Queue studio** — Drag/drop or browse files, reorder, start/stop, edit queued jobs, reset cancelled/failed jobs
- **Presets** — CRF/preset/tune, Opus bitrates, SVT-AV1-Tritium advanced params (non-defaults → HandBrake `-x` encoder options)
- **Audio** — Preferred languages + best-track-only in presets; Opus 5.1 (incl. 7.1 downmix) / stereo; video-only if none selected
- **Subtitles** — Per-job track choice alongside audio (pre-selected by subtitle languages and kinds); extracted to text sidecars named after the output, optional OpenSubtitles fill for what's missing
- **HDR / Dolby Vision** — See [Format philosophy → HDR & Dolby Vision](#hdr--dolby-vision) (HDR10 / HLG / HDR10+ passthrough; DoVi base layer → HDR10, with DoVi RPU passthrough on by default; P5 skipped; P4 / unconfirmed filename-DoVi / unknown profile-compat quarantined)

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
| Encode | **HandBrakeCLI** built with **SVT-AV1-Tritium** (`bin/handbrake/`), single-pass — encode, HDR10+ / Dolby Vision passthrough, Opus / E-AC-3 audio, MP4 / WebM mux |
| Probe / test segments / subtitles | **FFmpeg** / **ffprobe** |
| Quality metrics (optional) | **VapourSynth** + **FFMS2**, **vszip** (CPU), **Vship** (NVIDIA GPU) — post-encode SSIMU2 |
| System metrics | **psutil** |

Python packages (installed into `vs/python-env/` by setup from `requirements.txt`): `fastapi`, `uvicorn[standard]`, `websockets`, `psutil`, `vapoursynth`, `vstools`, `vsjetpack`, `py7zr`, `langcodes`, `guessit`, `watchdog`, …

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

Starts the server as a background process with a **system tray icon** — no console window, not shown on the taskbar. Right-click the tray icon for Open Studio / Start Queue / Pause/Resume Queue / View Log / **Open Live Log Console** / Stop Server. On first launch it opens **http://localhost:8765** in your browser automatically; if the server is already running, it just reuses it. Binds to **127.0.0.1** only. Server output goes to `logs/server.log`; **View Log** opens a static snapshot in Notepad, **Open Live Log Console** opens a console window that tails it in real time.

**Launcher settings (`.env`):** the variables below can go in a `.env` file in the project root — copy `.env.example` to `.env` and uncomment what you need (one `KEY=VALUE` per line, `#` comments). It's gitignored, so credentials stay local. Both `runGUI.bat` and `scripts\start_queue.ps1` read it at launch; restart the server after editing. A variable already set in the real environment takes precedence over the file.

**Port:** set `AV1QUEUE_PORT` (in `.env`, or as an environment variable before launching) to use a port other than 8765 (e.g. `set AV1QUEUE_PORT=9000 && runGUI.bat`, or add that `set` to a desktop shortcut's Target). Applies to both `runGUI.bat` and `scripts\start_queue.ps1`. An unset or invalid value falls back to 8765. Stop any instance already running on the old port first — the tray launcher only recognizes a server on the port it's currently configured for.

**Network access (Tailscale / LAN):** the server binds to `127.0.0.1` only by default, so it's unreachable from another machine (including over Tailscale) no matter the port — loopback never accepts remote connections. Set `AV1QUEUE_HOST` to widen that, e.g. `set AV1QUEUE_HOST=0.0.0.0 && runGUI.bat` (all interfaces) or a specific interface IP such as your Tailscale address (`100.x.x.x`, from `tailscale ip -4`). Loopback stays bound too, on top of whatever you add — so setting just your Tailscale IP gives you Tailscale + localhost **without** exposing the LAN (uvicorn's own `--host` flag can't express that combination, which is why `core/run_server.py` opens the sockets itself instead). By itself there is no login on this server — anything that can reach the bound address gets full control: adding/cancelling jobs, changing settings, reading stored TMDB/OpenSubtitles API keys. Set `AV1QUEUE_USERNAME` and `AV1QUEUE_PASSWORD` to add one (HTTP Basic Auth — your browser's native login prompt, no custom login page). It's skipped entirely for loopback requests (127.0.0.1), so the tray and a local browser tab need no credentials; it only gates the surface `AV1QUEUE_HOST` opens up, HTTP routes and the `/ws/live` WebSocket alike. Only widen the host on a network you trust; a Tailscale tailnet (which gates reachability by your own ACLs) is a reasonable case, the open internet is not — and a login is not a substitute for that judgment, just a second lock. The tray's local controls (health check, Open Studio, Start/Pause Queue) always use `127.0.0.1` regardless of either setting, since loopback keeps working once the server is listening.

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
├── .env.example               # Launcher settings template (copy to .env)
├── bin/                       # ffmpeg, ffprobe; HandBrakeCLI under bin/handbrake/
├── vs/
│   ├── portable.vs
│   ├── python-env/            # Isolated Python venv (VS plugins also autoload here)
│   └── plugins64/             # Plugin copies for PATH / DLL resolution
├── core/
│   ├── queue_manager.py       # Job lifecycle & encode orchestration
│   ├── handbrake_encode.py    # HandBrakeCLI command builder + applied-crop parser
│   ├── pipeline.py            # Probe, audio selection, test segment, HandBrake runner, SSIMU2, cleanup
│   ├── hdr_dovi.py            # HDR / Dolby Vision detection (skip policy, output check)
│   ├── media_tagging.py       # Release-name parsing, TMDB lookup, library output names
│   ├── subtitle_extract.py    # Text subtitle sidecars from the source
│   ├── subtitle_search.py     # OpenSubtitles search / download for missing sidecars
│   ├── watch_folder.py        # Auto-queue new files from a watched folder
│   ├── presets_store.py       # Preset JSON files (builtin/ + local/)
│   ├── app_settings.py        # settings.json + per-job config defaults
│   ├── measure_ssimu2.py      # SSIMU2 scorer (VapourSynth, run as a subprocess)
│   ├── vs_geometry.py         # Crop / downscale helpers for SSIMU2
│   ├── win_process.py         # Windows EcoQoS opt-out for encode processes
│   └── run_server.py          # uvicorn wrapper used by the launchers
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
    ├── setup_env.py           # Download HandBrakeCLI, FFmpeg, plugins, venv
    ├── start_queue.ps1        # Foreground launcher (visible console, for debugging)
    ├── tray_launcher.ps1      # Background launcher used by runGUI.bat (tray icon)
    ├── load_env.ps1           # Loads .env into both launchers
    ├── launch_hidden.vbs      # Runs tray_launcher.ps1 with zero window flash
    └── survey_library.py      # One-off report of the HDR layers a library carries
```

---

## Encode stages

1. **Prepare** — HDR / Dolby Vision analysis (drives the skip / quarantine rules below), audio track selection, test-mode segment cut
2. **Encode** — [HandBrakeCLI built with SVT-AV1-Tritium](https://github.com/Uranite/HandBrake-SVT-AV1-Tritium) in one process: autocrop, downscale, single-pass SVT-AV1-Tritium (`--encoder-preset` / `-q`, preset SVT params as `-x key=value:…`), HDR10+ / Dolby Vision passthrough (`--hdr-dynamic-metadata`), selected audio → Opus (or E-AC-3 per preset), MP4 (`+faststart`) or WebM mux. No fast pass, no metrics, no CRF zones
3. **Verify & publish** — The output is written as `*.partial.mp4` beside the destination and only renamed into place after checking that a source that was HDR came out HDR
4. **Subtitles (optional)** — The job's chosen text tracks (the **Subtitles** section of the import inspector / edit dialog; new jobs start with the tracks matching your subtitle languages and kinds, image-based PGS / VobSub tracks are listed but can't be extracted) are read from the **original** file and written next to the **encoded output** (SRT / ASS / WebVTT are copied as-is; MP4 `mov_text` is converted to SRT), named after it (`<output name>.en.srt`, `.forced.srt`, `.sdh.srt`); languages / kinds still missing can then be fetched from OpenSubtitles (moviehash of the original first, then FPS and release-name match, IMDb / title search to find candidates). Runs in a **background thread** after SSIMU2, so the next queue job can start immediately. Test Mode encodes get no sidecars unless **Settings → Subtitles → Extract subtitles in Test Mode** is on (off by default): then this step runs unchanged, online search included, with sidecars named after the test file. It's a check that subtitles are found and exported: they're full-length, not cut to the test segment
5. **SSIMU2 (optional)** — Post-encode quality score via Vship (GPU) or vszip (CPU), when enabled in settings

**What the app decides vs. what HandBrake decides.** The app owns probe, the DoVi skip / quarantine policy, audio track selection and order, resolution target (`--maxHeight`, downscale only, with `--loose-anamorphic` so the width scales along and pixels stay square: a 3840×1600 source at 1080p becomes 2592×1080), test-mode segments, SSIMU2, subtitles and the watch folder. HandBrake owns autocrop, the encode itself, HDR10 static metadata, HDR10+ and Dolby Vision passthrough (including the P7 → 8.1 RPU conversion and the RPU's active-area fix after cropping), audio encoding and the container. Preset `svt_params` and the lp / low-memory settings reach the Tritium library as HandBrake encoder options under the same names SvtAv1EncApp takes. Chapters and subtitles are stripped from the output (`--no-markers`, `-s none`); audio tracks are left unnamed (`--no-keep-aname`), so players label them from language, codec and layout. HandBrake's full activity log for a job is `_temp/job_<id>/encode/handbrake.log` while it runs.

**Autocrop** runs in HandBrake with `--crop-mode conservative` over 30 sampled frames (`--previews 30:0`; HandBrake's default is 10). Conservative keeps the least crop found, so a title that changes aspect ratio (IMAX sections, for example) keeps its full frame instead of losing picture. Tested on synthetic titles: with 30 samples a full-frame section only 3% of the runtime was kept; with the default 10 it was cropped away. The crop HandBrake applied is read back from its log (`parse_applied_crop`) for SSIMU2 and the job stats; if it can't be read, SSIMU2 is skipped for that job rather than scored on mismatched frames. **Settings → Autocrop black bars** off sends `--crop-mode none`.

**HandBrakeCLI build.** `setup.bat` installs the x86_64 CLI from the repo's rolling `win` snapshot release and verifies it against the release's `sha256.txt`. Snapshots can change twice a week; setup never replaces an existing copy, so delete `bin/handbrake/` to move to a newer one.

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

The single rule: **can the source's base layer stand on its own?** If yes, encode it and preserve whatever dynamic metadata it carries; if no, leave the original untouched. The `dv_bl_signal_compatibility_id` ffprobe field answers this — 0 means no, anything else means yes. Profile 7 / 8.1 base layers are already valid HDR10, so they always encode as HDR10 regardless of whether the RPU is also carried. Profile 5's base layer is stored in DV's IPT-PQc2 colourspace, so tagging it HDR10 would bake in a permanent colour cast and there's no cheap conversion — so P5 sources are skipped and kept as-is, with or without RPU passthrough enabled.

Since a non-DoVi player simply reads the HDR10 base layer and ignores an RPU it doesn't understand, carrying the RPU alongside costs nothing on unsupported devices and only helps on the (currently rare, likely growing) DoVi-capable AV1 clients — so it's on by default. **Settings → Preserve Dolby Vision RPU** (toggle off if you'd rather not) maps to HandBrake's `--hdr-dynamic-metadata all` (on) or `hdr10plus` (off). Best-effort — if the output ends up without Dolby Vision the job still completes as HDR10 with a warning, never a hard failure. It doesn't change P5/P4/quarantine handling at all, since those rows aren't about the RPU.

| Source | Base layer standalone? | Output (RPU passthrough off) | Output (RPU passthrough on, default) |
|--------|------------------------|----------------------------------------|-------------------------------|
| SDR (BT.709) | yes | SDR AV1 | unchanged (no DoVi to carry) |
| HLG | yes | HLG AV1 (ARIB B67 transfer preserved) | unchanged |
| HDR10 (static) | yes | HDR10 AV1 (MDL + MaxCLL/MaxFALL carried by HandBrake) | unchanged |
| HDR10+ | yes | **HDR10+ AV1** (HandBrake passthrough) | unchanged |
| DoVi P8.1 / P7 | yes | HDR10 AV1 — base layer encoded, RPU discarded | HDR10 AV1 **+ DoVi RPU** (P7 rewritten to 8.1); non-DoVi clients still just see the HDR10 layer |
| DoVi P8.1 / P7 + HDR10+ | yes | HDR10+ AV1 — RPU discarded, HDR10+ kept | HDR10+ AV1 **+ DoVi RPU** — both metadata tracks carried |
| DoVi **P5** (compat 0) | **no** | **skipped** — original file left untouched | unchanged — RPU passthrough can't help; the base layer itself isn't valid HDR10 |
| DoVi P4 (legacy) | unknown | **quarantined** for manual review | unchanged |
| Filename says DoVi, not probe-confirmed | unknown | **quarantined** — P5 cannot be ruled out | unchanged |
| DoVi confirmed, profile/compat unknown | unknown | **quarantined** for manual review | unchanged |

#### HDR10+ and Dolby Vision passthrough

Both are HandBrake's: it reads the HDR10+ SEI and the Dolby Vision RPU from the source, writes them into the AV1 stream, and signals Dolby Vision in the MP4 container. It applies the same rules this app used to implement itself: a profile-7 RPU (or profile 8 with Blu-ray compat id 6) is rewritten to profile 8.1 because the enhancement layer isn't encoded (FEL detail is discarded), and after a crop the RPU's active area is corrected to match the cropped picture. WebM cannot signal Dolby Vision.

**Not yet verified here on real sources:** Dolby Vision and HDR10+ passthrough have only been exercised on synthetic clips so far — check the job log's "Output check" line on your first DoVi / HDR10+ encodes.

**What's checked afterwards:** a source that was HDR must come out HDR, or the output is discarded and the job fails. Dolby Vision missing from the output only warns, since it's best-effort. HDR10+ can't be confirmed afterwards (the bundled ffprobe can't read AV1 frame metadata), so HandBrake is trusted to carry it, and the job log says so.

**Chroma siting:** the source's 4:2:0 chroma location is passed as the SVT `chroma-sample-position` option (`left` → `vertical`, `topleft` → `colocated`), since HandBrake otherwise leaves it unset, so the AV1 sequence header matches the source instead of saying "unknown". Applies to SDR and HDR alike; other locations stay unknown.

#### Sourcing guidance

Prefer at acquisition, best first: HDR10+ (passes through intact) → DV P7 / P8.1 (base layer drops cleanly to HDR10, RPU optionally carried too) → HDR10 / HLG → SDR → DV P5 (last resort; skipped and left in its original form).

---

## Upstream & resources

| Project | Role | Link |
|---------|------|------|
| HandBrake-SVT-AV1-Tritium | Encoder: HandBrakeCLI built with SVT-AV1-Tritium | [Uranite/HandBrake-SVT-AV1-Tritium](https://github.com/Uranite/HandBrake-SVT-AV1-Tritium) |
| SVT-AV1-Tritium | The AV1 encoder library inside that HandBrake build | [Uranite/svt-av1-tritium](https://github.com/Uranite/svt-av1-tritium) |
| FFmpeg | Probe, test segments, subtitle extraction | [GyanD/codexffmpeg](https://github.com/GyanD/codexffmpeg) (essentials build used by setup) |
| Vship | GPU SSIMULACRA2 | [Line-fr/Vship](https://codeberg.org/Line-fr/Vship) |
| vapoursynth-zip (vszip) | CPU metrics | [dnjulek/vapoursynth-zip](https://github.com/dnjulek/vapoursynth-zip) |
| FFMS2 | Source filter for VS (SSIMU2) | [FFMS/ffms2](https://github.com/FFMS/ffms2) |
| VapourSynth / vstools | Scripting / helpers | [vapoursynth](https://www.vapoursynth.com/), [vsjetpack](https://github.com/Jaded-Encoding-Thaumaturgy/vs-jetpack) |

`scripts/setup_env.py` pulls current Windows assets from these release APIs where possible.

---

## Configuration notes

- **Presets** — Individual JSON files under `server/presets/builtin/` (git-tracked) and `server/presets/local/` (UI-created, gitignored). Each has `crf` / `preset` plus advanced SVT overrides as `svt_params`, passed to HandBrake as `-x key=value:…` encoder options (`core/handbrake_encode.py`). Built-in presets are read-only in the UI unless **Settings → App → Enable built-in preset edits** is on. The preset editor shows a live preview of the non-default encoder options.
- **Queue** — Survives restarts via `server/queue.json`. Cancelled/failed jobs can be **Reset** back to queued.
- **Test mode** — Global toggle + duration settings in the UI; jobs can run a short trim instead of the full file.
- **Audio defaults** — With no explicit selection, a job takes the best track per preferred language (Settings → Audio, English by default; best = surround first, then channel count, then bitrate), or the single best track overall if none match. An explicit empty selection in the job editor means video-only.
- **HDR** — See [Format philosophy → HDR & Dolby Vision](#hdr--dolby-vision).

---

## Development

Server entrypoint (as used by the launchers): `core/run_server.py` (a thin uvicorn wrapper — see its docstring for why it opens sockets itself instead of using uvicorn's `--host`/`--port` CLI flags directly). Localhost only by default. Port and bind host are overridable via `AV1QUEUE_PORT` / `AV1QUEUE_HOST`, with optional HTTP Basic Auth via `AV1QUEUE_USERNAME` / `AV1QUEUE_PASSWORD` (see [Launch the studio](#2-launch-the-studio) — widen the host only on a trusted network, a login is a second lock, not a substitute).

Useful API surface (non-exhaustive): `/api/queue`, `/api/queue/add`, `/api/queue/update`, `/api/queue/requeue`, `/api/presets`, `/api/probe`, `/api/system`, `/api/history`, WebSocket `/ws/live`.

Static UI has no build step — edit `server/static/*` and refresh the browser.
