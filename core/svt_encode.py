#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "vsjetpack",
#   "rich",
# ]
# ///

# Requires manually installing:
# SVT-AV1-Tritium: https://github.com/Uranite/svt-av1-tritium/releases
# in your system PATH or the script's directory, and:
# FFMS2:           https://github.com/FFMS/ffms2/releases
# in the VapourSynth plugin directory

# Direct single-pass SVT-AV1-Tritium encode, replacing the Auto-Boost pipeline.
# Loads the source via VapourSynth (ffms2), applies crop/resize, and pipes
# frames straight to SvtAv1EncApp — no fast pass, no metrics, no CRF zones.

from vstools import vs, core, depth, DitherType
from vs_geometry import apply_crop, resize_to_height
from rich.progress import Progress, TextColumn, BarColumn, TimeElapsedColumn, TimeRemainingColumn, SpinnerColumn
from rich.console import Console
from pathlib import Path
import subprocess
import argparse
import platform
import shlex
import sys
import os
import time

# Keep this process (and later SvtAv1EncApp) off Windows EcoQoS / Efficiency Mode.
try:
    from win_process import boost_current_process, boost_process
except ImportError:
    try:
        from core.win_process import boost_current_process, boost_process
    except ImportError:
        def boost_current_process():
            return False

        def boost_process(_pid):
            return False

boost_current_process()

ver_str = "v1.0"

parser = argparse.ArgumentParser()
parser.add_argument("-i", "--input", required=True, help="Video input filepath (original source file)")
parser.add_argument("-t", "--temp", help="Temporary directory for the script to store files in | Default: video input filename")
parser.add_argument("--preset", type=int, default=4, help="SVT-AV1 --preset value (-3 to 13) | Default: 4")
parser.add_argument("--crf", type=float, default=30.0, help="SVT-AV1 --crf value (1-70, 0.25 steps) | Default: 30")
parser.add_argument("--svt-params", default="", help="Extra raw SvtAv1EncApp flags (tune, film-grain, psychovisual overrides, HDR flags, etc.)")
parser.add_argument("--hdr10plus-json", default=None, help="Path to HDR10+ JSON (passed through to SvtAv1EncApp)")
parser.add_argument("--dolby-vision-rpu", default=None, help="Path to a profile-10 DoVi RPU binary (passed through to SvtAv1EncApp)")
parser.add_argument("--target-height", type=int, default=0, help="Downscale to this height if source is larger (0 = keep source resolution)")
parser.add_argument("--crop", default="", help="Black-bar crop as left,top,right,bottom pixels (even values recommended)")
parser.add_argument("--verbose", action="store_true", help="Enable more verbosity | Default: not active")
parser.add_argument("-v", "--version", action="version", version=f"svt_encode {ver_str}")
args = parser.parse_args()

src_file = Path(args.input).resolve()
if platform.system() == "Windows":
    src_file = type(src_file)(r"\\?" + rf"\{src_file}")

if args.temp is not None:
    tmp_dir = Path(args.temp).resolve()
    if platform.system() == "Windows":
        tmp_dir = type(tmp_dir)(r"\\?" + rf"\{tmp_dir}")
else:
    tmp_dir = src_file.parent / src_file.stem

cache_file = tmp_dir / f"{src_file.stem}.ffindex"
output_file = tmp_dir / f"{src_file.stem}.ivf"

preset = args.preset
crf = args.crf
svt_params = args.svt_params or ""
hdr10plus_json = args.hdr10plus_json
dolby_vision_rpu = args.dolby_vision_rpu
target_height = int(args.target_height or 0)
crop_left = crop_top = crop_right = crop_bottom = 0
if args.crop:
    try:
        parts = [int(float(x.strip())) for x in str(args.crop).split(",")]
        if len(parts) == 4:
            crop_left, crop_top, crop_right, crop_bottom = parts
    except Exception:
        print(f"WARNING: Ignoring invalid --crop value: {args.crop}")
verbose = args.verbose

if not os.path.exists(src_file):
    print("The source input doesn't exist. Double-check the provided path.")
    raise SystemExit(1)

if not os.path.exists(tmp_dir):
    os.makedirs(tmp_dir)

core.max_cache_size = 1024


def build_clip():
    src = core.ffms2.Source(source=str(src_file), cachefile=str(cache_file))
    src = apply_crop(src, crop_left, crop_top, crop_right, crop_bottom)

    bit_to_format = {8: vs.YUV420P8, 10: vs.YUV420P10, 12: vs.YUV420P12}
    bit_to_dither = {8: DitherType.NONE, 10: DitherType.NONE, 12: DitherType.RANDOM}
    fmt = bit_to_format.get(src.format.bits_per_sample, vs.YUV420P16)
    dt = bit_to_dither.get(src.format.bits_per_sample, DitherType.RANDOM)
    src = depth(src.resize.Bilinear(format=fmt), 10, dither_type=dt)

    src = resize_to_height(src, target_height)
    return src


_noninteractive = os.environ.get("SVTENCODE_NONINTERACTIVE") == "1" or not sys.stdout.isatty()
console = Console(force_terminal=not _noninteractive, force_interactive=not _noninteractive)

# Machine-readable progress for the queue UI (parsed by core/queue_manager.py).
# Format: __AV1Q_PROGRESS__ <label> <percent> <fps> <done> <total>
_PROGRESS_PREFIX = "__AV1Q_PROGRESS__"


class _StageProgress:
    """Emit UI ticks ~every 5s; human console lines every ~5% when noninteractive."""

    def __init__(self, label: str, total: int):
        self.label = label
        self.total = max(int(total), 1)
        self._last_ui = time.monotonic()
        self._last_f = 0
        self._last_log_bucket = -1

    def tick(self, done: int, force: bool = False) -> None:
        if not _noninteractive:
            return
        done = max(0, min(int(done), self.total))
        pct = 100.0 * done / self.total
        now = time.monotonic()
        bucket = int(pct // 5)
        ui_due = force or (now - self._last_ui) >= 5.0
        log_due = force or (bucket > self._last_log_bucket and bucket >= 1)

        if not ui_due and not log_due:
            return

        dt = max(now - self._last_ui, 1e-6)
        df = max(done - self._last_f, 0)
        fps = df / dt

        if ui_due:
            console.print(f"{_PROGRESS_PREFIX} {self.label} {pct:.1f} {fps:.2f} {done} {self.total}")
            self._last_ui = now
            self._last_f = done

        if log_due:
            self._last_log_bucket = bucket
            console.print(f"{self.label} {pct:.1f}% {fps:.1f} fps ({done}/{self.total})")


def split_encoder_params(params: str) -> list:
    """
    shlex.split(..., posix=True) treats backslash as an escape character, which
    mangles Windows-style paths by stripping single backslashes. posix=False
    leaves backslashes untouched but keeps surrounding quote chars, so strip those.
    """
    tokens = shlex.split(params, posix=False)
    stripped = []
    for t in tokens:
        if len(t) >= 2 and t[0] == t[-1] and t[0] in ('"', "'"):
            t = t[1:-1]
        stripped.append(t)
    return stripped


def encode() -> None:
    encoder_params = f"--preset {preset} --crf {crf} "
    if svt_params:
        encoder_params += svt_params

    if verbose:
        console.print(f'Encoder params: "{encoder_params}"')

    encoder_params_list = split_encoder_params(encoder_params)
    if hdr10plus_json:
        encoder_params_list.extend(["--hdr10plus-json", str(hdr10plus_json)])
    if dolby_vision_rpu:
        encoder_params_list.extend(["--dolby-vision-rpu", str(dolby_vision_rpu)])

    svt_cmd = [
        "SvtAv1EncApp",
        "-i", "-",
        "--progress", "0",
        *encoder_params_list,
        "-b", str(output_file),
    ]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        refresh_per_second=1 if _noninteractive else 10,
        transient=False,
        disable=False,
    ) as progress:

        task = progress.add_task("[yellow]Initializing", total=None)
        svt_proc = None
        svt_err_path = tmp_dir / "encode_svt_stderr.log"

        try:
            clip = build_clip()
            total_frames = clip.num_frames

            progress.update(task, description="[green]Encoding", completed=0, total=total_frames)
            if _noninteractive:
                console.print(f"Starting encode ({total_frames} frames)...")

            svt_err = open(svt_err_path, "w", encoding="utf-8", errors="replace")
            svt_proc = subprocess.Popen(svt_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=svt_err)
            boost_process(svt_proc.pid)

            _prog = _StageProgress("encode", total_frames)

            def prog_func(current_frame, _total_frames):
                progress.update(task, completed=current_frame)
                _prog.tick(current_frame)

            clip.output(svt_proc.stdin, y4m=True, progress_update=prog_func)

            _prog.tick(total_frames, force=True)
            progress.update(task, description="[green]Finalizing", completed=total_frames - 1)

            try:
                if svt_proc.stdin:
                    svt_proc.stdin.close()
            except Exception:
                pass
            svt_proc.wait(timeout=600)
            try:
                svt_err.close()
            except Exception:
                pass

            if svt_proc.returncode != 0:
                progress.stop()
                err_tail = ""
                try:
                    err_tail = svt_err_path.read_text(encoding="utf-8", errors="replace")[-1200:]
                except Exception:
                    pass
                console.print(f"[red]The encode encountered an error:[/red] SVT-AV1 exited with code {svt_proc.returncode}")
                if err_tail:
                    console.print(err_tail)
                raise SystemExit(1)

            progress.update(task, description="[cyan]Completed", completed=total_frames)

        except KeyboardInterrupt:
            progress.stop()
            console.print("\n[yellow]Interrupted by user (Ctrl+C). Stopping...[/yellow]")
            if svt_proc is not None:
                svt_proc.terminate()
            raise SystemExit(1)
        except subprocess.CalledProcessError as e:
            progress.stop()
            console.print(f"[red]The encode encountered an error:[/red]\n{e}")
            raise SystemExit(1)
        except Exception as e:
            progress.stop()
            if svt_proc is not None and svt_proc.poll() is None:
                try:
                    svt_proc.kill()
                except Exception:
                    pass
            err_tail = ""
            try:
                err_tail = svt_err_path.read_text(encoding="utf-8", errors="replace")[-1200:]
            except Exception:
                pass
            console.print(f"[red]The encode encountered an error:[/red]\n{e}")
            if err_tail:
                console.print(err_tail)
            raise SystemExit(1)


console.print("[bold]Encode start!\n")
encode()
console.print("\n[bold]Encode complete!")
