"""
Extract text subtitle tracks from a source video (logic aligned with SubtitleExtractor).

Writes sidecar files next to the source, e.g.:
  Movie.en.srt
  Movie.cs.forced.ass
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

TEXT_SUB_CODECS = {"subrip", "ass", "ssa", "webvtt"}


def _ffmpeg_file_arg(path: Path) -> str:
    # Avoid ffmpeg globbing on [] in filenames
    return f"file:{path}"


def iso6391(code: Optional[str]) -> str:
    """Map any common language tag to ISO 639-1 (or und)."""
    if not code or code.strip().lower() in ("", "und", "unknown"):
        return "und"
    c = code.strip()
    try:
        from langcodes import Language

        la = Language.get(c)
        # langcodes parses syntactically valid but unassigned tags ("xx") happily,
        # so check the registry before letting one become a ".xx.srt" suffix.
        if la.is_valid():
            alpha2 = la.language
            if alpha2 and len(alpha2) == 2:
                return alpha2
        return "und"
    except Exception:
        pass
    c = c.lower()
    if re.fullmatch(r"[a-z]{2}", c):
        return c
    return "und"


def normalize_subtitle_utf8(path: Path) -> Optional[str]:
    """Normalize subtitle file to UTF-8 with BOM. Returns detected source encoding label."""
    if not path.is_file():
        return None
    data = path.read_bytes()
    if not data:
        return None

    text: Optional[str] = None
    source = "unknown"

    if len(data) >= 3 and data[0:3] == b"\xef\xbb\xbf":
        text = data[3:].decode("utf-8")
        source = "utf8-bom"
    elif len(data) >= 2 and data[0:2] == b"\xff\xfe":
        text = data[2:].decode("utf-16-le")
        source = "utf16-le"
    elif len(data) >= 2 and data[0:2] == b"\xfe\xff":
        text = data[2:].decode("utf-16-be")
        source = "utf16-be"
    else:
        # Strict UTF-8 first: when it succeeds it is definitive, and accidental
        # valid multi-byte UTF-8 in another codepage is vanishingly rare.
        try:
            text = data.decode("utf-8")
            source = "utf8"
        except UnicodeDecodeError:
            text = None
        if text is None:
            # charset-normalizer for Windows-1250 / Shift-JIS / etc.
            try:
                from charset_normalizer import from_bytes

                best = from_bytes(data).best()
                if best is not None:
                    text = str(best)
                    source = best.encoding or "charset-normalizer"
            except Exception:
                pass
        if text is None:
            text = data.decode("cp1250", errors="replace")
            source = "windows-1250"

    # newline="" so already-CRLF content isn't translated again into \r\r\n
    # (which compounds to \r\r\r\n on a re-run and breaks strict SRT parsers).
    path.write_text(text, encoding="utf-8-sig", newline="")
    return source


# Leading/trailing credit / author / site spam commonly prepended to fan-subs.
_CREDIT_LINE_RE = re.compile(
    r"(?is)^\s*(?:"
    r"(?:created|subtitles?|subs?|translated?|translation|timed?|timing|"
    r"synced?|resync(?:ed)?|corrected?|encoded?|ripped?|provided|"
    r"brought|adapted|proofread)\s+by\b|"
    r"copyright\b|\(c\)|©|"
    r"all\s+rights\s+reserved|"
    r"opensubtitles(?:\.org|\.com)?|"
    r"addic7ed|podnapisi|subscene|yify(?:-subs)?|"
    r"www\.|https?://|"
    r"advertise\s+your\s+product|"
    r"support\s+us\b|"
    r"download\s+(?:the\s+)?(?:app|subtitles?)\b|"
    r"watch\s+movies?\s+online|"
    r"visit\s+(?:us|our)\b|"
    r"sub(?:title)?s?\s+by\b"
    r")",
)


def _plain_credit_text(text: str) -> str:
    """Strip simple ASS/HTML tags for credit matching."""
    t = re.sub(r"\{[^}]*\}", "", text or "")
    t = re.sub(r"<[^>]+>", "", t)
    return t.strip()


def _is_credit_block(text: str) -> bool:
    plain = _plain_credit_text(text)
    if not plain:
        return True
    # Whole cue is credit-like if every non-empty line matches, or the block is short spam
    lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
    if not lines:
        return True
    if all(_CREDIT_LINE_RE.match(ln) for ln in lines):
        return True
    # Single-line cue that is only a bare site/handle
    if len(lines) == 1 and (
        _CREDIT_LINE_RE.match(lines[0])
        or re.fullmatch(r"(?i)[\w.-]+\.(com|org|net|info|tv)\b.*", lines[0])
    ):
        return True
    return False


def _strip_srt_credits(text: str) -> tuple[str, int]:
    """Remove leading/trailing credit cues from SRT. Returns (text, removed_count)."""
    # Split on blank lines into blocks; keep structure
    raw_blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip())
    blocks: List[Dict[str, str]] = []
    for raw in raw_blocks:
        lines = raw.split("\n")
        if len(lines) < 2:
            continue
        # index / timestamp / body
        ts_i = 0
        if re.fullmatch(r"\d+", lines[0].strip()):
            ts_i = 1
        if ts_i >= len(lines) or "-->" not in lines[ts_i]:
            continue
        body = "\n".join(lines[ts_i + 1 :]).strip()
        blocks.append({"ts": lines[ts_i].strip(), "body": body})

    if not blocks:
        return text, 0

    removed = 0
    while blocks and _is_credit_block(blocks[0]["body"]):
        blocks.pop(0)
        removed += 1
    while blocks and _is_credit_block(blocks[-1]["body"]):
        blocks.pop()
        removed += 1

    if removed == 0:
        return text, 0

    out_parts: List[str] = []
    for i, b in enumerate(blocks, start=1):
        out_parts.append(f"{i}\n{b['ts']}\n{b['body']}")
    return "\n\n".join(out_parts).rstrip() + "\n", removed


def _strip_ass_credits(text: str) -> tuple[str, int]:
    """Remove leading/trailing Dialogue credit lines from ASS/SSA."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    dialogue_idx = [
        i for i, ln in enumerate(lines)
        if ln.startswith("Dialogue:") or ln.startswith("Comment:")
    ]
    if not dialogue_idx:
        return text, 0

    def _dialogue_text(ln: str) -> str:
        # Format: Dialogue: Layer,Start,End,Style,Name,M,M,M,Effect,Text
        parts = ln.split(",", 9)
        return parts[9] if len(parts) >= 10 else ln

    removed = 0
    drop: set[int] = set()
    # From start of dialogue region
    for i in dialogue_idx:
        if _is_credit_block(_dialogue_text(lines[i])):
            drop.add(i)
            removed += 1
        else:
            break
    # From end
    for i in reversed(dialogue_idx):
        if i in drop:
            continue
        if _is_credit_block(_dialogue_text(lines[i])):
            drop.add(i)
            removed += 1
        else:
            break

    if not drop:
        return text, 0
    new_lines = [ln for i, ln in enumerate(lines) if i not in drop]
    return "\n".join(new_lines), removed


def _strip_vtt_credits(text: str) -> tuple[str, int]:
    """Remove leading/trailing cues from WebVTT."""
    text_n = text.replace("\r\n", "\n").replace("\r", "\n")
    header, _, rest = text_n.partition("\n\n")
    if not header.upper().startswith("WEBVTT"):
        body, n = _strip_srt_credits(text_n)
        return body, n
    raw_blocks = [b for b in rest.split("\n\n") if b.strip()]
    parsed: List[tuple] = []  # (is_cue, body, raw)
    for raw in raw_blocks:
        lines = raw.split("\n")
        ts_i = 0
        if lines and "-->" not in lines[0]:
            ts_i = 1
        if ts_i < len(lines) and "-->" in lines[ts_i]:
            body = "\n".join(lines[ts_i + 1 :])
            parsed.append((True, body, raw))
        else:
            parsed.append((False, "", raw))

    removed = 0
    while parsed and parsed[0][0] and _is_credit_block(parsed[0][1]):
        parsed.pop(0)
        removed += 1
    while parsed and parsed[-1][0] and _is_credit_block(parsed[-1][1]):
        parsed.pop()
        removed += 1
    if removed == 0:
        return text, 0
    body_out = "\n\n".join(p[2] for p in parsed)
    return header.rstrip() + "\n\n" + body_out.rstrip() + "\n", removed


def strip_subtitle_credits(path: Path) -> int:
    """
    Remove copyright / author / site-credit cues from the start and end of a
    subtitle file. Returns number of cues/lines removed (0 = unchanged).
    """
    path = Path(path)
    if not path.is_file():
        return 0
    try:
        text = path.read_text(encoding="utf-8-sig")
    except Exception:
        return 0
    if not text.strip():
        return 0

    ext = path.suffix.lower()
    if ext == ".srt":
        new_text, removed = _strip_srt_credits(text)
    elif ext in (".ass", ".ssa"):
        new_text, removed = _strip_ass_credits(text)
    elif ext == ".vtt":
        new_text, removed = _strip_vtt_credits(text)
    else:
        new_text, removed = _strip_srt_credits(text)

    if removed <= 0 or new_text == text:
        return 0
    path.write_text(new_text, encoding="utf-8-sig")
    return removed


def extract_text_subtitles(
    source_file: Path,
    *,
    ffprobe: Path,
    ffmpeg: Path,
    output_dir: Optional[Path] = None,
    basename: Optional[str] = None,
    languages: Optional[List[str]] = None,
    kinds: Optional[List[str]] = None,
    strip_credits: bool = False,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Extract text subtitle streams from source_file next to the source (or output_dir).

    basename: stem for sidecar files (defaults to source stem). Prefer the
    target encode stem so subs match the output name.

    languages: ISO codes (2 or 3 letter). Empty/None = all languages.
    kinds: subset of {"standard", "forced", "sdh"}. Empty/None = all kinds.
    strip_credits: remove leading/trailing author/copyright cues when True.

    Returns {status, extracted, files, message}.
    """
    source_file = Path(source_file)
    out_dir = Path(output_dir) if output_dir else source_file.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    file_stem = (str(basename).strip() if basename else "") or source_file.stem

    def log(msg: str):
        if progress_cb:
            progress_cb(msg)

    lang_filter = None
    if languages:
        lang_filter = {iso6391(x) for x in languages if x}
        # Also accept 3-letter forms that map to the same 2-letter code
        lang_filter |= {str(x).strip().lower()[:3] for x in languages if x}

    # None = all kinds; [] = extract none
    kind_filter = None
    if kinds is not None:
        kind_filter = {str(k).strip().lower() for k in kinds if k}

    if not source_file.is_file():
        return {"status": "error", "extracted": 0, "files": [], "message": f"Source not found: {source_file}"}

    input_arg = _ffmpeg_file_arg(source_file)
    try:
        probe = subprocess.run(
            [
                str(ffprobe), "-v", "quiet", "-print_format", "json",
                "-show_streams", "-select_streams", "s",
                input_arg,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except Exception as e:
        return {"status": "error", "extracted": 0, "files": [], "message": f"ffprobe failed: {e}"}

    if probe.returncode != 0 or not (probe.stdout or "").strip():
        return {"status": "error", "extracted": 0, "files": [], "message": "ffprobe could not read subtitle streams"}

    try:
        data = json.loads(probe.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "extracted": 0, "files": [], "message": "Could not parse ffprobe JSON"}

    streams = list(data.get("streams") or [])
    if not streams:
        log("No subtitle streams")
        return {"status": "skip", "extracted": 0, "files": [], "message": "no subtitle streams"}

    def sort_key(s: Dict[str, Any]):
        disp = s.get("disposition") or {}
        default = 0 if disp.get("default") == 1 else 1
        return (default, int(s.get("index") or 0))

    streams.sort(key=sort_key)

    used_names: Dict[str, bool] = {}
    ff_args: List[str] = [
        str(ffmpeg), "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", input_arg,
    ]
    outputs: List[Path] = []

    for stream in streams:
        codec = str(stream.get("codec_name") or "").lower()
        if codec not in TEXT_SUB_CODECS:
            continue

        index = stream.get("index")
        if index is None:
            continue

        tags = stream.get("tags") or {}
        lang = iso6391(tags.get("language"))
        lang_raw = str(tags.get("language") or "und").strip().lower()
        title = str(tags.get("title") or "")
        disp = stream.get("disposition") or {}

        if lang_filter is not None:
            if lang not in lang_filter and lang_raw not in lang_filter and lang_raw[:3] not in lang_filter:
                continue

        is_forced = disp.get("forced") == 1
        is_sdh = (
            disp.get("hearing_impaired") == 1
            or bool(re.search(r"\b(sdh|hi|cc)\b", title, re.I))
        )
        if is_forced:
            kind = "forced"
        elif is_sdh:
            kind = "sdh"
        else:
            kind = "standard"

        if kind_filter is not None and kind not in kind_filter:
            continue

        if codec == "subrip":
            ext = "srt"
        elif codec in ("ass", "ssa"):
            ext = "ass"
        elif codec == "webvtt":
            ext = "vtt"
        else:
            ext = "srt"

        suffix = f".{lang}"
        if kind == "forced":
            suffix += ".forced"
        elif kind == "sdh":
            suffix += ".sdh"

        file_name = f"{file_stem}{suffix}.{ext}"
        n = 2
        while file_name.lower() in used_names:
            file_name = f"{file_stem}{suffix}.{n}.{ext}"
            n += 1
        used_names[file_name.lower()] = True

        outfile = out_dir / file_name
        log(f"Subtitle + {suffix}.{ext}  ({lang.upper()} · {kind} · {codec}" + (f" · {title}" if title else "") + ")")

        ff_args.extend(["-map", f"0:{index}", "-c:s", "copy", _ffmpeg_file_arg(outfile)])
        outputs.append(outfile)

    if not outputs:
        log("No matching text subtitle tracks")
        return {"status": "skip", "extracted": 0, "files": [], "message": "no matching text subtitle tracks"}

    log(f"Extracting {len(outputs)} subtitle track(s)…")
    try:
        res = subprocess.run(
            ff_args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except Exception as e:
        return {"status": "error", "extracted": 0, "files": [], "message": f"ffmpeg failed: {e}"}

    written: List[str] = []
    for outfile in outputs:
        if not outfile.is_file():
            continue
        try:
            enc = normalize_subtitle_utf8(outfile)
            if enc and enc not in ("utf8", "utf8-bom"):
                log(f"Normalized {outfile.name} ({enc} → UTF-8)")
        except Exception as e:
            log(f"Encoding normalize failed for {outfile.name}: {e}")
        if strip_credits:
            try:
                n = strip_subtitle_credits(outfile)
                if n:
                    log(f"Removed {n} credit cue(s) from {outfile.name}")
            except Exception as e:
                log(f"Credit strip failed for {outfile.name}: {e}")
        written.append(str(outfile))

    if res.returncode != 0:
        return {
            "status": "error",
            "extracted": len(written),
            "files": written,
            "message": f"ffmpeg exit {res.returncode}",
        }

    log(f"Subtitles done · {len(written)} file(s)")
    return {
        "status": "ok",
        "extracted": len(written),
        "files": written,
        "message": f"extracted {len(written)} file(s)",
    }
