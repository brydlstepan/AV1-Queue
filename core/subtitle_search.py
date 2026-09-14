"""
Search / download missing text subtitles via OpenSubtitles.com API.

Requires a free API key from https://www.opensubtitles.com/en/consumers
Optional username/password improve download quotas.

Fills gaps for configured languages × subtitle kinds (standard / forced / sdh)
after embedded extract (or instead of it), matching primarily by IMDb ID and
preferring FPS close to the source when ranking results.
"""

from __future__ import annotations

import json
import re
import struct
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.subtitle_extract import iso6391, normalize_subtitle_utf8, strip_subtitle_credits

OPENSUBTITLES_BASE = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "AV1Queue v1.0"
_HASH_BLOCK = 65536

Kind = str  # "standard" | "forced" | "sdh"
Slot = Tuple[str, Kind]  # (iso639-1 lang, kind)

# Tokens that help pick a release-synced subtitle (timing) from the source name.
_RELEASE_SCORE_TOKENS = (
    "remux", "bluray", "blu-ray", "bdrip", "uhd", "2160p", "1080p", "720p",
    "webrip", "web-dl", "webdl", "hdtv", "hdr", "dv", "dovi", "atmos",
    "truehd", "dts", "hybrid", "hmax", "nf", "amzn", "dsnp",
)

# Mutually exclusive release families — mismatch is a strong timing risk.
_RELEASE_FAMILIES = (
    frozenset({"remux", "bluray", "blu-ray", "bdrip", "bdav", "uhdbluray"}),
    frozenset({"webrip", "web-dl", "webdl", "web", "hmax", "nf", "amzn", "dsnp", "hdtv"}),
)


def _http_json(
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    body: Optional[Dict[str, Any]] = None,
    timeout: int = 20,
) -> Dict[str, Any]:
    data = None
    hdrs = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    if not raw.strip():
        return {}
    return json.loads(raw)


def _lang_to_os(code: str) -> str:
    """Map our ISO-ish codes to OpenSubtitles 2-letter language codes."""
    return iso6391(code) or "en"


def compute_opensubtitles_hash(path: Path) -> Optional[Tuple[str, int]]:
    """
    OpenSubtitles moviehash: size + sum(uint64 LE of first/last 64KB).
    Returns (16-char hex hash, filesize) or None if the file is too small / unreadable.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size < _HASH_BLOCK * 2:
        return None
    try:
        h = size & 0xFFFFFFFFFFFFFFFF
        with path.open("rb") as f:
            for _ in range(_HASH_BLOCK // 8):
                buf = f.read(8)
                if len(buf) < 8:
                    break
                (chunk,) = struct.unpack("<Q", buf)
                h = (h + chunk) & 0xFFFFFFFFFFFFFFFF
            f.seek(max(0, size - _HASH_BLOCK))
            for _ in range(_HASH_BLOCK // 8):
                buf = f.read(8)
                if len(buf) < 8:
                    break
                (chunk,) = struct.unpack("<Q", buf)
                h = (h + chunk) & 0xFFFFFFFFFFFFFFFF
        return f"{h:016x}", size
    except OSError:
        return None


def _release_tokens(text: str) -> Set[str]:
    raw = re.split(r"[^a-z0-9]+", (text or "").lower())
    out: Set[str] = set()
    for t in raw:
        if len(t) < 2:
            continue
        out.add(t)
        if t == "web":
            out.add("webdl")
        if t in ("bluray", "blu"):
            out.add("bluray")
    # Normalize common compounds already split (web dl → already tokens)
    if "web" in out and "dl" in out:
        out.add("webdl")
        out.add("web-dl")
    return out


def _family_of(tokens: Set[str]) -> Optional[int]:
    for i, fam in enumerate(_RELEASE_FAMILIES):
        if tokens & fam:
            return i
    return None


def _release_score(candidate_release: str, source_hint: Optional[str]) -> int:
    """
    Overlap between subtitle release label and source filename tokens.
    Penalize cross-family matches (e.g. WEB-DL sub for a BluRay REMUX source).
    """
    if not source_hint:
        return 0
    src = _release_tokens(source_hint)
    cand = _release_tokens(candidate_release)
    if not src or not cand:
        return 0

    interesting = {t for t in src if t in _RELEASE_SCORE_TOKENS or len(t) >= 5}
    if not interesting:
        interesting = src
    score = len(interesting & cand) * 2

    # Resolution preference from source
    for res in ("2160p", "1080p", "720p"):
        if res in src and res in cand:
            score += 3
        elif res in src and any(r in cand for r in ("2160p", "1080p", "720p") if r != res):
            score -= 2

    src_fam = _family_of(src)
    cand_fam = _family_of(cand)
    if src_fam is not None and cand_fam is not None:
        if src_fam == cand_fam:
            score += 8
        else:
            score -= 12  # WEB sub on BluRay source (or reverse) → bad timing risk

    if "remux" in src and "remux" in cand:
        score += 4
    return score


def _normalize_kinds(kinds: Optional[List[str]]) -> List[Kind]:
    allowed = {"standard", "forced", "sdh"}
    if kinds is None:
        return ["standard"]
    out: List[Kind] = []
    for k in kinds:
        kk = str(k or "").strip().lower()
        if kk in allowed and kk not in out:
            out.append(kk)
    return out or ["standard"]


def _sidecar_kind_from_name(name: str, stem: str) -> Optional[Slot]:
    """
    Parse Movie.en.srt / Movie.en.forced.ass / Movie.cs.sdh.2.srt → (lang, kind).
    """
    pat = re.compile(
        rf"^{re.escape(stem)}\.([a-z]{{2,3}})"
        rf"(?:\.(forced|sdh|hi))?(?:\.\d+)?\.(?:srt|ass|ssa|vtt)$",
        re.I,
    )
    m = pat.match(name)
    if not m:
        return None
    lang = iso6391(m.group(1))
    if not lang:
        return None
    tag = (m.group(2) or "").lower()
    if tag == "forced":
        kind: Kind = "forced"
    elif tag in ("sdh", "hi"):
        kind = "sdh"
    else:
        kind = "standard"
    return lang, kind


def existing_sidecar_slots(
    source_file: Path,
    languages: List[str],
    kinds: Optional[List[str]] = None,
    *,
    basename: Optional[str] = None,
    output_dir: Optional[Path] = None,
) -> Set[Slot]:
    """Which (language, kind) sidecars already exist next to the output."""
    stem = (str(basename).strip() if basename else "") or source_file.stem
    parent = Path(output_dir) if output_dir else source_file.parent
    wanted_langs = {_lang_to_os(x) for x in languages if x}
    wanted_kinds = set(_normalize_kinds(kinds))
    found: Set[Slot] = set()
    if not wanted_langs or not parent.is_dir():
        return found

    for p in parent.iterdir():
        if not p.is_file():
            continue
        slot = _sidecar_kind_from_name(p.name, stem)
        if not slot:
            continue
        lang, kind = slot
        if lang in wanted_langs and kind in wanted_kinds:
            found.add((lang, kind))
    return found


def existing_sidecar_langs(
    source_file: Path,
    languages: List[str],
    *,
    basename: Optional[str] = None,
    output_dir: Optional[Path] = None,
) -> Set[str]:
    """Languages that have at least one matching sidecar (any kind)."""
    slots = existing_sidecar_slots(
        source_file, languages, ["standard", "forced", "sdh"],
        basename=basename, output_dir=output_dir,
    )
    return {lang for lang, _ in slots}


def login_opensubtitles(api_key: str, username: str, password: str) -> Optional[str]:
    if not (api_key and username and password):
        return None
    try:
        data = _http_json(
            "POST",
            f"{OPENSUBTITLES_BASE}/login",
            headers={
                "Api-Key": api_key,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            body={"username": username, "password": password},
        )
        token = (data.get("token") or "").strip()
        return token or None
    except Exception:
        return None


def _result_kind(attrs: Dict[str, Any]) -> Kind:
    if bool(attrs.get("hearing_impaired")):
        return "sdh"
    if bool(attrs.get("foreign_parts_only")):
        return "forced"
    return "standard"


def search_subtitles(
    *,
    api_key: str,
    imdb_id: Optional[str],
    languages: List[str],
    query: Optional[str] = None,
    token: Optional[str] = None,
    kind: Optional[Kind] = None,
    media_type: str = "movie",
    season: Optional[int] = None,
    episode: Optional[int] = None,
    moviehash: Optional[str] = None,
    moviebytesize: Optional[int] = None,
) -> List[Dict[str, Any]]:
    if not api_key:
        return []
    langs = ",".join(sorted({_lang_to_os(x) for x in languages if x}))
    if not langs:
        return []

    params: Dict[str, str] = {
        "languages": langs,
        "type": "episode" if media_type == "episode" else "movie",
        "order_by": "download_count",
        "order_direction": "desc",
        "ai_translated": "exclude",
        "machine_translated": "exclude",
    }
    if kind == "sdh":
        params["hearing_impaired"] = "only"
        params["foreign_parts_only"] = "exclude"
    elif kind == "forced":
        params["foreign_parts_only"] = "only"
        params["hearing_impaired"] = "exclude"
    elif kind == "standard":
        params["hearing_impaired"] = "exclude"
        params["foreign_parts_only"] = "exclude"

    # Hash search is the most accurate for timing; do not mix with imdb/query
    # in the same request (OS docs: hash takes precedence / ignores others).
    if moviehash and moviebytesize:
        params["moviehash"] = str(moviehash)
        params["moviebytesize"] = str(int(moviebytesize))
    elif imdb_id:
        iid = str(imdb_id).lower().replace("tt", "").strip()
        if iid:
            params["imdb_id"] = iid
        else:
            return []
    elif query:
        params["query"] = query
    else:
        return []

    if media_type == "episode" and not (moviehash and moviebytesize):
        if season is not None:
            params["season_number"] = str(int(season))
        if episode is not None:
            params["episode_number"] = str(int(episode))

    headers = {
        "Api-Key": api_key,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{OPENSUBTITLES_BASE}/subtitles?" + urllib.parse.urlencode(params)
    try:
        data = _http_json("GET", url, headers=headers)
    except Exception:
        return []

    out: List[Dict[str, Any]] = []
    for item in data.get("data") or []:
        attrs = item.get("attributes") or {}
        files = attrs.get("files") or []
        if not files:
            continue
        lang = iso6391(attrs.get("language") or "")
        try:
            fps_val = float(attrs.get("fps") or 0) or None
        except (TypeError, ValueError):
            fps_val = None
        out.append({
            "id": item.get("id"),
            "language": lang,
            "file_id": files[0].get("file_id"),
            # feature_details can be present but JSON null, so .get(…, {}) isn't enough
            "release": attrs.get("release") or (attrs.get("feature_details") or {}).get("title") or "",
            "hearing_impaired": bool(attrs.get("hearing_impaired")),
            "foreign_parts_only": bool(attrs.get("foreign_parts_only")),
            "ai_translated": bool(attrs.get("ai_translated")),
            "machine_translated": bool(attrs.get("machine_translated")),
            "download_count": int(attrs.get("download_count") or 0),
            "fps": fps_val,
            "kind": _result_kind(attrs),
            "moviehash_match": bool(attrs.get("moviehash_match")) or bool(moviehash),
        })
    return out


def _merge_results(*lists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Dedupe by file_id, preferring earlier lists (hash before imdb)."""
    seen: Set[Any] = set()
    out: List[Dict[str, Any]] = []
    for lst in lists:
        for r in lst or []:
            fid = r.get("file_id")
            if fid is None or fid in seen:
                continue
            seen.add(fid)
            out.append(r)
    return out


def download_subtitle_file(
    *,
    api_key: str,
    file_id: int,
    dest: Path,
    token: Optional[str] = None,
) -> bool:
    headers = {
        "Api-Key": api_key,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        data = _http_json(
            "POST",
            f"{OPENSUBTITLES_BASE}/download",
            headers=headers,
            body={"file_id": int(file_id)},
        )
    except Exception:
        return False

    link = (data.get("link") or "").strip()
    if not link:
        return False

    try:
        req = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception:
        return False


def _fps_score(candidate_fps: Optional[float], target_fps: Optional[float]) -> float:
    """Higher is better. Exact / near match beats unknown."""
    if not target_fps or not candidate_fps or candidate_fps <= 0:
        return 0.0
    diff = abs(float(candidate_fps) - float(target_fps))
    if diff <= 0.02:
        return 3.0
    if diff <= 0.05:
        return 2.0
    if diff <= 0.15:
        return 1.0
    return -1.0  # likely wrong timing


def pick_best_for_slot(
    results: List[Dict[str, Any]],
    lang: str,
    kind: Kind,
    *,
    fps: Optional[float] = None,
    release_hint: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    candidates = [
        r for r in results
        if r.get("language") == lang and r.get("kind") == kind
    ]
    if not candidates and kind == "standard":
        # Fallback: unmarked HI/FPO results still usable as dialogue tracks
        candidates = [
            r for r in results
            if r.get("language") == lang
            and not r.get("hearing_impaired")
            and not r.get("foreign_parts_only")
        ]
    if not candidates:
        return None

    def score(r: Dict[str, Any]):
        return (
            1 if r.get("moviehash_match") else 0,
            _fps_score(r.get("fps"), fps),
            _release_score(str(r.get("release") or ""), release_hint),
            0 if r.get("ai_translated") or r.get("machine_translated") else 1,
            int(r.get("download_count") or 0),
        )

    return sorted(candidates, key=score, reverse=True)[0]


def pick_best_for_lang(results: List[Dict[str, Any]], lang: str) -> Optional[Dict[str, Any]]:
    """Back-compat: best non-HI dialogue track for a language."""
    return pick_best_for_slot(results, lang, "standard")


def _sidecar_filename(stem: str, lang: str, kind: Kind) -> str:
    if kind == "forced":
        return f"{stem}.{lang}.forced.srt"
    if kind == "sdh":
        return f"{stem}.{lang}.sdh.srt"
    return f"{stem}.{lang}.srt"


def search_and_download_missing(
    source_file: Path,
    *,
    languages: List[str],
    imdb_id: Optional[str],
    query: Optional[str],
    api_key: str,
    username: str = "",
    password: str = "",
    progress_cb: Optional[Callable[[str], None]] = None,
    basename: Optional[str] = None,
    output_dir: Optional[Path] = None,
    strip_credits: bool = False,
    kinds: Optional[List[str]] = None,
    fps: Optional[float] = None,
    media_kind: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    release_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """
    For each preferred (language, kind) missing a sidecar, download one file.

    Matching priority for timing:
      1) OpenSubtitles moviehash of the source file (exact release)
      2) FPS close to the source
      3) Overlap between subtitle release name and source filename
         (BluRay/REMUX preferred over WEB-DL when the source is a disc rip)
      4) IMDb / title search as the discovery fallback
    """
    def log(msg: str):
        if progress_cb:
            progress_cb(msg)

    source_file = Path(source_file)
    stem = (str(basename).strip() if basename else "") or source_file.stem
    parent = Path(output_dir) if output_dir else source_file.parent
    parent.mkdir(parents=True, exist_ok=True)
    release_hint = (str(release_hint).strip() if release_hint else "") or source_file.name

    langs_os = sorted({_lang_to_os(x) for x in languages if x})
    kind_list = _normalize_kinds(kinds)
    if not langs_os:
        return {
            "status": "skip", "downloaded": 0, "files": [], "missing": [],
            "message": "no languages configured",
        }
    if not (api_key or "").strip():
        return {
            "status": "skip", "downloaded": 0, "files": [],
            "missing": [f"{l}.{k}" for l in langs_os for k in kind_list],
            "message": "OpenSubtitles API key not set",
        }

    have = existing_sidecar_slots(
        source_file, languages, kind_list, basename=stem, output_dir=parent
    )
    wanted: List[Slot] = [(lang, kind) for lang in langs_os for kind in kind_list]
    missing = [slot for slot in wanted if slot not in have]
    if not missing:
        log("All preferred subtitle languages/types already present")
        return {
            "status": "ok", "downloaded": 0, "files": [], "missing": [],
            "message": "nothing missing",
        }

    missing_label = ", ".join(
        f"{lang.upper()} ({kind})" for lang, kind in missing
    )
    log(f"Missing subtitle slots: {missing_label}")
    if fps:
        log(f"Preferring FPS ≈ {fps:.3f}")

    token = login_opensubtitles(api_key, username, password)
    media_type = "episode" if (
        str(media_kind or "").lower() == "episode"
        or (season is not None and episode is not None)
    ) else "movie"

    hash_info = compute_opensubtitles_hash(source_file)
    moviehash = hash_info[0] if hash_info else None
    moviebytesize = hash_info[1] if hash_info else None
    if moviehash:
        log(f"Source moviehash {moviehash} ({moviebytesize} bytes)")
    else:
        log("Could not compute moviehash — falling back to IMDb / title + FPS")

    results_by_kind: Dict[Kind, List[Dict[str, Any]]] = {}
    missing_langs = sorted({lang for lang, _ in missing})
    for kind in kind_list:
        if not any(k == kind for _, k in missing):
            continue
        by_hash: List[Dict[str, Any]] = []
        if moviehash and moviebytesize:
            by_hash = search_subtitles(
                api_key=api_key,
                imdb_id=None,
                languages=missing_langs,
                token=token,
                kind=kind,
                media_type=media_type,
                moviehash=moviehash,
                moviebytesize=moviebytesize,
            )
            if by_hash:
                log(
                    f"OpenSubtitles {kind} hash match: {len(by_hash)} hit(s)"
                )
        by_imdb = search_subtitles(
            api_key=api_key,
            imdb_id=imdb_id,
            languages=missing_langs,
            query=query,
            token=token,
            kind=kind,
            media_type=media_type,
            season=season,
            episode=episode,
        )
        log(
            f"OpenSubtitles {kind} IMDb/title: {len(by_imdb)} hit(s) "
            f"for {', '.join(missing_langs)}"
        )
        results_by_kind[kind] = _merge_results(by_hash, by_imdb)

    if not any(results_by_kind.values()):
        return {
            "status": "skip",
            "downloaded": 0,
            "files": [],
            "missing": [f"{l}.{k}" for l, k in missing],
            "message": "no OpenSubtitles matches",
        }

    written: List[str] = []
    still_missing: List[str] = []

    for lang, kind in missing:
        best = pick_best_for_slot(
            results_by_kind.get(kind) or [],
            lang,
            kind,
            fps=fps,
            release_hint=release_hint,
        )
        if not best or not best.get("file_id"):
            still_missing.append(f"{lang}.{kind}")
            continue

        dest = parent / _sidecar_filename(stem, lang, kind)
        n = 2
        while dest.exists():
            if kind == "forced":
                dest = parent / f"{stem}.{lang}.forced.{n}.srt"
            elif kind == "sdh":
                dest = parent / f"{stem}.{lang}.sdh.{n}.srt"
            else:
                dest = parent / f"{stem}.{lang}.{n}.srt"
            n += 1

        why = []
        if best.get("moviehash_match"):
            why.append("hash")
        if best.get("fps"):
            why.append(f"{best['fps']} fps")
        rel = (best.get("release") or "")[:60]
        if rel:
            why.append(rel)
        log(
            f"Downloading {lang.upper()} {kind} · file_id={best['file_id']}"
            + (f" · {' · '.join(why)}" if why else "")
        )
        ok = download_subtitle_file(
            api_key=api_key,
            file_id=int(best["file_id"]),
            dest=dest,
            token=token,
        )
        if ok:
            try:
                normalize_subtitle_utf8(dest)
            except Exception:
                pass
            if strip_credits:
                try:
                    removed = strip_subtitle_credits(dest)
                    if removed:
                        log(f"Removed {removed} credit cue(s) from {dest.name}")
                except Exception as e:
                    log(f"Credit strip failed for {dest.name}: {e}")
            written.append(str(dest))
            log(f"Saved {dest.name}")
        else:
            still_missing.append(f"{lang}.{kind}")
            log(f"Download failed for {lang}.{kind}")

    status = "ok" if written else ("skip" if still_missing else "ok")
    return {
        "status": status,
        "downloaded": len(written),
        "files": written,
        "missing": still_missing,
        "message": (
            f"downloaded {len(written)}; "
            f"still missing {', '.join(still_missing) or 'none'}"
        ),
    }
