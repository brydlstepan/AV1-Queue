# AGENTS.md — AV1 Queue Studio

Guidance for AI agents working in this repository. Prefer this file over re-deriving project intent from scattered sources; the README has deeper rationale.

## What this project is

Local **Windows** AV1 encoding queue with a browser UI. It wraps **SVT-AV1-Tritium** direct **single-pass** encoding in a FastAPI backend plus a vanilla dark web frontend for batch jobs, presets, audio selection, HDR/DoVi handling, and live progress.

Target use: a Jellyfin library optimized for **direct-play** (AV1 + MP4 + Opus). Video transcode avoidance is the design goal; rare audio-only Jellyfin transcodes are acceptable.

## Stack (do not reinvent)

| Layer | Location / tech |
|--------|------------------|
| UI | `server/static/` — HTML / CSS / JS, **no build step** |
| API | `server/app.py` — FastAPI + Uvicorn, REST + WebSocket `/ws/live` |
| Queue / orchestration | `core/queue_manager.py` |
| Encode | `core/svt_encode.py` → **SvtAv1EncApp** (SVT-AV1-Tritium) |
| Pipeline (probe, audio, mux, cleanup) | `core/pipeline.py` |
| HDR / Dolby Vision | `core/hdr_dovi.py` |
| Binaries | `bin/` (ffmpeg, ffprobe, hdr10plus_tool, SVT under `bin/svt/`) |
| VapourSynth env | `vs/` (created by setup; plugins autoload from the venv, copies also in `vs/plugins64/`) |

Python packages live in `vs/python-env/` after `setup.bat` / `.\scripts\setup.ps1`.

## Encode pipeline (fixed shape)

Single-pass only — **no** fast pass, metrics-driven CRF zones, or multi-pass AV1 workflow.

1. **Audio** — Selected tracks → Opus by default (`eac3` per preset), or video-only
2. **Encode** — VapourSynth (ffms2) load → optional crop/resize → pipe to SvtAv1EncApp (`--preset` / `--crf` / `--svt-params`)
3. **Mux** — IVF + audio → MP4 (`+faststart`) or WebM
4. **Subtitles (optional)** — Extract from **original** beside the source; background thread so the next job can start
5. **SSIMU2 (optional)** — Post-mux score via Vship (GPU) or vszip (CPU)

## Format & HDR invariants (do not casually change)

These are product decisions, not preferences:

- **Video:** AV1 via SVT-AV1-Tritium
- **Container default:** MP4 (browser + native direct-play). WebM is an option; do not push MKV as the library default
- **Audio default:** Opus (5.1 / stereo). `audio_format` may be `eac3` per preset for AVR bitstream setups
- **Dolby Vision is never emitted**
- Encode only if the source **base layer stands alone**:
  - DoVi **P5** / `dv_bl_signal_compatibility_id` **0** → **skip**, leave original untouched
  - DoVi **P4** → **quarantine** for manual review
  - Filename says DoVi but probe did **not** confirm DoVi side_data → **quarantine**
  - Confirmed DoVi with unknown profile/compat (not P7/P8) → **quarantine**
  - DoVi P7 / P8.1 → encode base as HDR10; **discard RPU**
  - HDR10+ → passthrough via `hdr10plus_tool` JSON + Tritium `--hdr10plus-json` (both required)
- Chapters/subtitles are stripped from the muxed output (sidecars are separate)

Full tables and rationale: README → *Format philosophy* → *HDR & Dolby Vision*.

## Directory map

```
├── setup.bat               # Double-click installer
├── runGUI.bat              # Tray launcher → http://127.0.0.1:8765
├── core/                   # Encode, queue, HDR, subtitles, watch folder
├── server/
│   ├── app.py
│   ├── static/             # Queue UI
│   ├── presets/builtin/    # Tracked shared presets
│   └── presets/local/      # UI-created, gitignored
├── bin/                    # Tooling (mostly setup-downloaded)
├── scripts/                # setup.ps1 / setup_env.py, start, tray
├── logs/, _temp/, history/ # Runtime — do not commit
└── .agents/skills/         # On-demand agent skills (e.g. commit)
```

## Commands agents should know

```cmd
setup.bat                    # Preferred: double-click installer
runGUI.bat                   # Background tray process (127.0.0.1:8765)
```

```powershell
.\scripts\setup.ps1          # Same setup without the .bat wrapper
.\scripts\start_queue.ps1    # Foreground server (debug)
```

Server (as launchers run it): `uvicorn server.app:app --host 127.0.0.1 --port 8765` (see `start_queue.ps1` for PATH / `PYTHONPATH`).

Useful API: `/api/queue`, `/api/queue/add`, `/api/queue/update`, `/api/queue/requeue`, `/api/presets`, `/api/probe`, `/api/system`, `/api/history`, WS `/ws/live`.

## Coding conventions for this repo

- **Platform:** Windows x64 assumptions in scripts and portable VS layout — keep PowerShell / path handling compatible
- **UI changes:** edit `server/static/*` and refresh; no bundler
- **Presets:** builtin JSON under `server/presets/builtin/` are shared defaults; local presets are machine-only
- **SVT overrides:** non-defaults go through preset `svt_params` → `--svt-params` in `core/svt_encode.py`
- Prefer extending existing `core/` modules over adding parallel encode stacks
- Do not expand scope into DV→HDR10+ conversion, Profile 10 emission, or full colour-managed HDR remastering — out of scope by design

## When changing behavior

1. Preserve single-pass Tritium encode and the HDR skip/passthrough rules above unless the user explicitly redesigns them
2. Update README sections that document user-facing behavior when you change pipeline/format/HDR semantics
3. Keep agent instructions here short; put long rationale in the README

Commit workflow: `.agents/skills/commit.md` (load when the user asks to commit). Runtime/local paths are covered by `.gitignore`.
