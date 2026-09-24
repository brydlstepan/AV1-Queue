"""
AV1 Queue Setup Script
Downloads and configures all required binaries, VapourSynth plugins, and Python packages.
"""

import sys
import shutil
import zipfile
import urllib.request
import json
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "bin"
VS_DIR = BASE_DIR / "vs"
PLUGINS_DIR = VS_DIR / "plugins64"
CORE_DIR = BASE_DIR / "core"
TEMP_DIR = BASE_DIR / "_temp"
VENV_DIR = VS_DIR / "python-env"

ERRORS: list[str] = []
WARNINGS: list[str] = []

for d in [BIN_DIR, VS_DIR, PLUGINS_DIR, CORE_DIR, TEMP_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Portable VapourSynth marker
(VS_DIR / "portable.vs").touch()


def note_error(msg: str) -> None:
    ERRORS.append(msg)
    print(f"[!] {msg}")


def note_warning(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"[!] Warning: {msg}")


def fresh_dir(path: Path) -> Path:
    """Wipe and recreate an extract directory so stale nested files cannot be picked."""
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def download_file(url: str, dest: Path, desc: str = "") -> None:
    print(f"[*] Downloading {desc or dest.name}...")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as response, open(dest, "wb") as out_file:
        total_size = int(response.info().get("Content-Length", 0))
        downloaded = 0
        chunk_size = 64 * 1024
        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            out_file.write(chunk)
            downloaded += len(chunk)
            if total_size > 0:
                percent = (downloaded / total_size) * 100
                print(
                    f"\r    Progress: {percent:.1f}% "
                    f"({downloaded // (1024 * 1024)}MB / {total_size // (1024 * 1024)}MB)",
                    end="",
                )
    print("\n    Done.")


def extract_zip(zip_path: Path, dest_dir: Path) -> None:
    print(f"[*] Extracting {zip_path.name}...")
    fresh_dir(dest_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)


def get_latest_github_release(repo: str):
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode("utf-8"))


HANDBRAKE_TRITIUM_REPO = "Uranite/HandBrake-SVT-AV1-Tritium"


def install_handbrake_cli(hb_dir: Path) -> None:
    """HandBrakeCLI-*-win-x86_64.zip from the repo's rolling "win" release."""
    import hashlib
    import re

    url = f"https://api.github.com/repos/{HANDBRAKE_TRITIUM_REPO}/releases/tags/win"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as response:
        rel = json.loads(response.read().decode("utf-8"))
    assets = {a.get("name") or "": a.get("browser_download_url") for a in rel.get("assets", [])}
    zip_name = next((n for n in assets if re.fullmatch(r"HandBrakeCLI-.*-win-x86_64\.zip", n)), None)
    if not zip_name:
        raise RuntimeError("no HandBrakeCLI x86_64 zip in the release")
    if "sha256.txt" not in assets:
        raise RuntimeError("release has no sha256.txt to verify against")

    sums_req = urllib.request.Request(assets["sha256.txt"], headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(sums_req) as response:
        sums = response.read().decode("utf-8", "replace")
    expected = next(
        (line.split()[0].lower() for line in sums.splitlines()
         if len(line.split()) == 2 and line.split()[1] == zip_name),
        None,
    )
    if not expected:
        raise RuntimeError(f"{zip_name} is not listed in sha256.txt")

    zip_dest = TEMP_DIR / "handbrake_cli.zip"
    download_file(assets[zip_name], zip_dest, f"HandBrakeCLI ({zip_name})")
    actual = hashlib.sha256(zip_dest.read_bytes()).hexdigest()
    if actual != expected:
        zip_dest.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {zip_name}")

    extracted = TEMP_DIR / "handbrake_cli_extracted"
    extract_zip(zip_dest, extracted)
    exe = next(extracted.glob("**/HandBrakeCLI.exe"), None)
    if exe is None:
        raise RuntimeError("HandBrakeCLI.exe not found inside the zip")
    hb_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(exe, hb_dir / "HandBrakeCLI.exe")
    for doc in ("COPYING", "LICENSE"):
        for f in extracted.glob(f"**/{doc}"):
            shutil.copy2(f, hb_dir / doc)
            break
    print(f"    Installed HandBrakeCLI: {hb_dir / 'HandBrakeCLI.exe'} (sha256 verified)")


def setup_binaries() -> None:
    print("=" * 60)
    print("STEP 1: Setting up Core Binaries (HandBrakeCLI with SVT-AV1-Tritium, FFmpeg)")
    print("=" * 60)

    # 1. HandBrakeCLI built with SVT-AV1-Tritium — the encoder
    # (core/handbrake_encode.py). A snapshot build under the rolling "win"
    # tag, checked against the release's sha256.txt. Delete bin/handbrake/
    # to pick up a newer snapshot.
    hb_dir = BIN_DIR / "handbrake"
    hb_exe = hb_dir / "HandBrakeCLI.exe"
    if not hb_exe.exists():
        try:
            install_handbrake_cli(hb_dir)
        except Exception as e:
            note_error(f"Failed to install HandBrakeCLI: {e}")

    # 2. FFmpeg & FFprobe (GyanD Essentials) — probe, test-mode segments,
    # subtitle extraction
    ffmpeg_exe = BIN_DIR / "ffmpeg.exe"
    ffprobe_exe = BIN_DIR / "ffprobe.exe"
    if not ffmpeg_exe.exists() or not ffprobe_exe.exists():
        try:
            rel = get_latest_github_release("GyanD/codexffmpeg")
        except Exception as e:
            note_error(f"Could not query FFmpeg releases: {e}")
            rel = {"assets": []}
        asset_url = None
        for a in rel.get("assets", []):
            if "essentials_build.zip" in a["name"]:
                asset_url = a["browser_download_url"]
                break
        if asset_url:
            try:
                zip_dest = TEMP_DIR / "ffmpeg_essentials.zip"
                download_file(asset_url, zip_dest, "FFmpeg Essentials")
                extract_zip(zip_dest, TEMP_DIR / "ffmpeg_extracted")
                for f in (TEMP_DIR / "ffmpeg_extracted").glob("**/bin/ffmpeg.exe"):
                    shutil.copy2(f, ffmpeg_exe)
                for f in (TEMP_DIR / "ffmpeg_extracted").glob("**/bin/ffprobe.exe"):
                    shutil.copy2(f, ffprobe_exe)
                if ffmpeg_exe.exists() and ffprobe_exe.exists():
                    print("    Installed FFmpeg & FFprobe to bin/")
                else:
                    note_error("FFmpeg zip downloaded but ffmpeg.exe / ffprobe.exe not found inside.")
            except Exception as e:
                note_error(f"Failed to install FFmpeg: {e}")
        else:
            note_error("Could not find FFmpeg essentials_build.zip release asset.")


def setup_python_environment() -> None:
    print("=" * 60)
    print("STEP 2: Setting up Python Environment & Dependencies")
    print("=" * 60)

    python_exe = VENV_DIR / "Scripts" / "python.exe"
    if not python_exe.exists():
        print(f"[*] Creating Python virtual environment in {VENV_DIR}...")
        try:
            subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
        except Exception as e:
            note_error(f"Failed to create virtual environment: {e}")
            return

    if not python_exe.exists():
        note_error(f"Virtual environment python missing after create: {python_exe}")
        return

    print("[*] Installing required Python packages...")
    requirements = BASE_DIR / "requirements.txt"
    try:
        subprocess.run([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"], check=True)
        if requirements.is_file():
            subprocess.run(
                [str(python_exe), "-m", "pip", "install", "-r", str(requirements)],
                check=True,
            )
        else:
            note_warning("requirements.txt missing — installing a minimal fallback package set.")
            packages = [
                "wheel",
                "setuptools",
                "vapoursynth",
                "vstools",
                "vsjetpack",
                "rich",
                "fastapi",
                "uvicorn[standard]",
                "websockets",
                "psutil",
                "py7zr",
                "langcodes",
                "guessit",
                "charset-normalizer",
                "watchdog",
            ]
            subprocess.run([str(python_exe), "-m", "pip", "install"] + packages, check=True)
    except Exception as e:
        note_error(f"pip install failed: {e}")


def setup_vapoursynth_plugins() -> None:
    print("=" * 60)
    print("STEP 3: Setting up VapourSynth Plugins (vszip, vship, ffms2)")
    print("=" * 60)

    # pip vapoursynth autoloads from site-packages/vapoursynth/plugins/ — not plugins64 alone.
    autoload_dir = VENV_DIR / "Lib" / "site-packages" / "vapoursynth" / "plugins"
    if not (VENV_DIR / "Scripts" / "python.exe").exists():
        note_error("Skipping plugins — Python venv is missing.")
        return

    autoload_dir.mkdir(parents=True, exist_ok=True)
    python_exe = VENV_DIR / "Scripts" / "python.exe"

    def install_plugin(src: Path, name: str) -> None:
        shutil.copy2(src, autoload_dir / name)
        shutil.copy2(src, PLUGINS_DIR / name)
        print(f"    Installed {name} -> {autoload_dir}")

    # 1. vszip (CPU SSIMULACRA2) — optional for encode, needed for CPU scoring
    if not (autoload_dir / "vszip.dll").exists():
        url = "https://github.com/dnjulek/vapoursynth-zip/releases/download/R13/vapoursynth-zip-r13-windows-x86_64.zip"
        zip_dest = TEMP_DIR / "vszip.zip"
        try:
            download_file(url, zip_dest, "vszip (CPU SSIMULACRA2)")
            extract_zip(zip_dest, TEMP_DIR / "vszip_extracted")
            found = False
            for f in (TEMP_DIR / "vszip_extracted").glob("**/*.dll"):
                install_plugin(f, "vszip.dll")
                found = True
                break
            if not found:
                note_warning("vszip zip downloaded but DLL not found inside.")
        except Exception as e:
            note_warning(f"Could not install vszip: {e}")

    # 2. vship (NVIDIA GPU SSIMULACRA2) — optional
    if not (autoload_dir / "vship.dll").exists():
        url = "https://codeberg.org/Line-fr/Vship/releases/download/v5.1.1/libvship_NVIDIA.zip"
        zip_dest = TEMP_DIR / "libvship_NVIDIA.zip"
        try:
            download_file(url, zip_dest, "Vship (NVIDIA GPU SSIMULACRA2)")
            extract_zip(zip_dest, TEMP_DIR / "vship_extracted")
            found = False
            for f in (TEMP_DIR / "vship_extracted").glob("**/*.dll"):
                install_plugin(f, f.name)
                found = True
            if found:
                print("    Installed Vship GPU plugin")
            else:
                note_warning("Vship zip downloaded but DLL not found inside.")
        except Exception as e:
            note_warning(f"Could not download Vship NVIDIA: {e}")

    # 3. FFMS2 — source filter for SSIMU2 scoring
    if not (autoload_dir / "ffms2.dll").exists():
        url = "https://github.com/FFMS/ffms2/releases/download/5.0/ffms2-5.0-msvc.7z"
        dest_7z = TEMP_DIR / "ffms2.7z"
        try:
            download_file(url, dest_7z, "FFMS2")
            print("[*] Extracting FFMS2 plugin using py7zr...")
            extract_root = fresh_dir(TEMP_DIR / "ffms2_extracted")
            extract_code = f"""
import py7zr, shutil
from pathlib import Path
with py7zr.SevenZipFile(r'{dest_7z}', mode='r') as z:
    z.extractall(r'{extract_root}')
found = False
for f in Path(r'{extract_root}').glob('**/x64/ffms2.dll'):
    shutil.copy2(f, r'{autoload_dir / "ffms2.dll"}')
    shutil.copy2(f, r'{PLUGINS_DIR / "ffms2.dll"}')
    print('Installed ffms2.dll!')
    found = True
    break
raise SystemExit(0 if found else 1)
"""
            r = subprocess.run([str(python_exe), "-c", extract_code])
            if r.returncode != 0 or not (autoload_dir / "ffms2.dll").exists():
                note_warning("FFMS2 downloaded but ffms2.dll was not installed — SSIMU2 scoring won't work.")
        except Exception as e:
            note_warning(f"Error installing FFMS2 ({e}) — SSIMU2 scoring won't work.")


def verify_required() -> None:
    """Fail the run if anything encode-critical is still missing."""
    required = [
        (BIN_DIR / "ffmpeg.exe", "bin/ffmpeg.exe"),
        (BIN_DIR / "ffprobe.exe", "bin/ffprobe.exe"),
        (BIN_DIR / "handbrake" / "HandBrakeCLI.exe", "bin/handbrake/HandBrakeCLI.exe"),
        (VENV_DIR / "Scripts" / "python.exe", "vs/python-env (Python venv)"),
    ]
    for path, label in required:
        if not path.exists():
            note_error(f"Missing required component: {label}")

    ffms2 = VENV_DIR / "Lib" / "site-packages" / "vapoursynth" / "plugins" / "ffms2.dll"
    if not ffms2.exists():
        note_warning("ffms2.dll (VapourSynth source filter) is missing — SSIMU2 scoring won't work")


if __name__ == "__main__":
    print("\n============================================================")
    print("   AV1 QUEUE STUDIO INSTALLER")
    print("============================================================\n")
    setup_binaries()
    # Python env must run before plugins — autoload dir lives inside vapoursynth site-packages.
    setup_python_environment()
    setup_vapoursynth_plugins()
    verify_required()

    if WARNINGS:
        print(f"\n[!] {len(WARNINGS)} warning(s) — see messages above.")
    if ERRORS:
        print(f"\n[!] Setup FAILED with {len(ERRORS)} error(s):")
        for msg in ERRORS:
            print(f"    - {msg}")
        print("Fix the issues above and re-run setup.bat.")
        sys.exit(1)

    print("\n[+] Setup completed successfully!")
    sys.exit(0)
