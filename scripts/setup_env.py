"""
AV1 Queue Studio Setup Script
Downloads and configures all required binaries, VapourSynth plugins, and Python packages.
"""

import os
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


def setup_binaries() -> None:
    print("=" * 60)
    print("STEP 1: Setting up Core Binaries (SVT-AV1-Tritium, FFmpeg, hdr10plus_tool)")
    print("=" * 60)

    # 1. SVT-AV1-Tritium — vendored bin/svt/ is the normal path; download only if missing.
    svt_variants_dir = BIN_DIR / "svt"
    have_vendored_variants = svt_variants_dir.is_dir() and any(
        svt_variants_dir.glob("SvtAv1EncApp-*.exe")
    )
    svt_exe = BIN_DIR / "SvtAv1EncApp.exe"
    if have_vendored_variants:
        print("    SVT-AV1-Tritium: using vendored bin/svt/ variants (HDR10+ OK).")
    elif not svt_exe.exists():
        print(
            "[!] bin/svt/ variants are missing (unexpected — they should be tracked "
            "in git). Falling back to a direct download from the latest GitHub Release."
        )
        override = os.environ.get("SVT_TRITIUM_URL", "").strip()
        asset_url = None
        picked = ""
        target_name = None
        if override:
            asset_url, picked = override, "SVT_TRITIUM_URL override"
        else:
            try:
                rel = get_latest_github_release("Uranite/svt-av1-tritium")
            except Exception as e:
                note_error(f"Could not query SVT-AV1-Tritium releases: {e}")
                rel = {"assets": []}
            v3_asset = plain_asset = None
            for a in rel.get("assets", []):
                name = a.get("name") or ""
                low = name.lower()
                if not ("windows" in low and "znver2" in low):
                    continue
                url = a["browser_download_url"]
                if "v3" in low:
                    v3_asset = v3_asset or (url, name)
                else:
                    plain_asset = plain_asset or (url, name)
            if v3_asset:
                asset_url, picked = v3_asset[0], f"Windows x86-64-v3+znver2 build ({v3_asset[1]})"
                target_name = "SvtAv1EncApp-x86-64-v3-znver2.exe"
            elif plain_asset:
                asset_url, picked = plain_asset[0], f"Windows znver2 build ({plain_asset[1]})"
                target_name = "SvtAv1EncApp-znver2.exe"
        if asset_url:
            try:
                archive_dest = TEMP_DIR / "svt_tritium.tar.xz"
                download_file(asset_url, archive_dest, f"SVT-AV1-Tritium — {picked}")
                extract_dir = fresh_dir(TEMP_DIR / "svt_tritium_extracted")
                import tarfile

                with tarfile.open(archive_dest, mode="r:xz") as tf:
                    tf.extractall(extract_dir)
                found_exe = next(extract_dir.glob("**/SvtAv1EncApp.exe"), None)
                if found_exe:
                    svt_variants_dir.mkdir(parents=True, exist_ok=True)
                    dest_name = target_name or "SvtAv1EncApp-znver2.exe"
                    shutil.copy2(found_exe, svt_variants_dir / dest_name)
                    shutil.copy2(found_exe, svt_exe)
                    print(f"    Installed SVT-AV1-Tritium: bin/svt/{dest_name}")
                else:
                    note_error("SVT-AV1-Tritium archive downloaded but SvtAv1EncApp.exe not found inside.")
            except Exception as e:
                note_error(
                    f"Failed to download/extract SVT-AV1-Tritium ({e}). "
                    "Download manually: https://github.com/Uranite/svt-av1-tritium/releases"
                )
        else:
            note_error(
                "Could not find a Windows release asset for SVT-AV1-Tritium. "
                "https://github.com/Uranite/svt-av1-tritium/releases"
            )

        if svt_exe.exists():
            try:
                r = subprocess.run(
                    [str(svt_exe), "--help"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                blob = ((r.stdout or "") + "\n" + (r.stderr or "")).lower()
                has_h10p = "--hdr10plus-json" in blob
            except Exception as e:
                note_warning(f"Could not probe SVT capabilities: {e}")
                has_h10p = False
            if has_h10p:
                print("    SVT-AV1-Tritium supports --hdr10plus-json (HDR10+ passthrough OK).")
            else:
                note_warning(
                    "Installed SVT-AV1-Tritium has NO --hdr10plus-json — HDR10+ "
                    "passthrough will silently degrade to HDR10."
                )

    # 2. hdr10plus_tool
    hdr10plus_exe = BIN_DIR / "hdr10plus_tool.exe"
    if not hdr10plus_exe.exists():
        try:
            rel = get_latest_github_release("quietvoid/hdr10plus_tool")
        except Exception as e:
            note_error(f"Could not query hdr10plus_tool releases: {e}")
            rel = {"assets": []}
        asset_url = None
        for a in rel.get("assets", []):
            name = a.get("name") or ""
            if "x86_64-pc-windows-msvc.zip" in name:
                asset_url = a["browser_download_url"]
                break
        if asset_url:
            try:
                zip_dest = TEMP_DIR / "hdr10plus_tool.zip"
                download_file(asset_url, zip_dest, "hdr10plus_tool")
                extract_zip(zip_dest, TEMP_DIR / "hdr10plus_extracted")
                for f in (TEMP_DIR / "hdr10plus_extracted").glob("**/hdr10plus_tool.exe"):
                    shutil.copy2(f, hdr10plus_exe)
                    break
                if hdr10plus_exe.exists():
                    print(f"    Installed hdr10plus_tool: {hdr10plus_exe}")
                else:
                    note_error("hdr10plus_tool zip downloaded but exe not found inside.")
            except Exception as e:
                note_error(f"Failed to install hdr10plus_tool: {e}")
        else:
            note_error("Could not find Windows release for hdr10plus_tool.")

    # 3. FFmpeg & FFprobe (GyanD Essentials)
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

    # 3. FFMS2 — required for encode source load
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
                note_error("FFMS2 downloaded but ffms2.dll was not installed.")
        except Exception as e:
            note_error(f"Error installing FFMS2: {e}")


def verify_required() -> None:
    """Fail the run if anything encode-critical is still missing."""
    required = [
        (BIN_DIR / "ffmpeg.exe", "bin/ffmpeg.exe"),
        (BIN_DIR / "ffprobe.exe", "bin/ffprobe.exe"),
        (BIN_DIR / "hdr10plus_tool.exe", "bin/hdr10plus_tool.exe"),
        (VENV_DIR / "Scripts" / "python.exe", "vs/python-env (Python venv)"),
    ]
    for path, label in required:
        if not path.exists():
            note_error(f"Missing required component: {label}")

    svt_ok = (BIN_DIR / "SvtAv1EncApp.exe").exists() or any(
        (BIN_DIR / "svt").glob("SvtAv1EncApp-*.exe")
    )
    if not svt_ok:
        note_error("Missing required component: SVT-AV1-Tritium (bin/svt/*.exe or bin/SvtAv1EncApp.exe)")

    ffms2 = VENV_DIR / "Lib" / "site-packages" / "vapoursynth" / "plugins" / "ffms2.dll"
    if not ffms2.exists():
        note_error("Missing required component: ffms2.dll (VapourSynth source filter)")


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
