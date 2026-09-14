# AV1 Queue plan

A local Windows AV1 encoding queue with a browser UI, for one person preparing a Jellyfin library.

## Goal

Files in the library direct-play on Jellyfin clients (AV1 + MP4 + Opus), so video never has to be transcoded. Batches of movies can be queued and left to run, with HDR and Dolby Vision handled safely without checking each file by hand.

## Users

- The owner of a home Jellyfin library, who wants small files, consistent quality, and a queue that doesn't need watching.

## Scope

**In:**

- Batch queue with live progress, logs, and history in a browser UI
- Shareable encoder presets: CRF, speed, resolution target, audio bitrates, SVT options
- Single-pass AV1 encoding through HandBrakeCLI built with SVT-AV1-Tritium
- Skip or quarantine rules for Dolby Vision and HDR sources whose base layer can't stand alone
- Audio and subtitle track selection, text subtitle sidecars, and an optional OpenSubtitles fill
- Test Mode, optional SSIMU2 scoring, and a watch folder

**Out:**

- Fast-pass, multi-pass, or metrics-driven CRF zone workflows
- A second encode stack alongside HandBrake
- MKV as the library default
- Platforms other than Windows x64

## Stack and constraints

- FastAPI + Uvicorn backend, vanilla HTML / CSS / JS frontend, no build step
- HandBrakeCLI, ffmpeg, and ffprobe binaries under `bin/`, fetched by setup
- Python packages and VapourSynth (SSIMU2 only) in a local environment created by setup
- Runs on the owner's machine, bound to `127.0.0.1` by default

## First milestone

A movie added in the browser comes out as a direct-playable AV1 MP4 with its HDR intact and the source left untouched, and a run of queued movies completes without supervision.

## Decisions

| # | Decision | Answer |
|---|---|---|
| 1 | Encode workflow | Single pass only |
| 2 | Default container | MP4, for browser and native direct-play |
| 3 | Default audio | Opus; E-AC-3 optional for AVR setups |
| 4 | HDR output check | An HDR source whose output isn't HDR fails the job |

## Open questions

- None at the time of writing.
