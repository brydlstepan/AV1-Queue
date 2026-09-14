"""
Parse scene / release filenames and build library-style output names.

Input (typical):
  Dune.2021.2160p.BluRay.REMUX.HEVC.DV.DTS-HD.MA.TrueHD.7.1.Atmos-TNT.mkv

Output:
  Dune (2021) [imdbid-tt1160419] - [2160p HDR10].mp4
"""

from __future__ import annotations

import json
import re
import threading
from functools import lru_cache
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Tokens that mark the start of the technical / release suffix (not the title)
_RELEASE_TAG = re.compile(
    r"^(?:"
    r"\d{3,4}p|4k|uhd|hd|"
    r"bluray|blu-?ray|bdrip|brrip|bdmv|bdav|webrip|web-?dl|webdl|web|hdtv|hdrip|dvdrip|dvd|remux|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|av1|xvid|divx|"
    r"hdr10\+?|hdr|hlg|sdr|dv|dovi|dolby|vision|"
    r"aac|ac3|eac3|ddp|dd|dts|truehd|atmos|flac|lpcm|pcm|opus|"
    r"ma|hr|hd\.?ma|hd\.?hr|"
    r"proper|repack|internal|limited|complete|hybrid|remastered|criterion|"
    r"extended|theatrical|unrated|directors?\.?cut|dc|"
    r"multi|dual|nf|amzn|dsnp|hulu|atvp|itunes|hbo|max|"
    r"10bit|8bit|12bit|"
    r"repoleased|readnfo"
    r")$",
    re.I,
)

_AUDIO_CHANNEL = re.compile(r"^\d(?:\.\d)?$", re.I)  # 5.1, 7.1, 2.0
_IMDB_RE = re.compile(r"(?:\[(?:imdbid[-_]?|imdb[-_]?)?(tt\d{7,8})\]|(?:imdbid[-_]?|imdb[-_]?)(tt\d{7,8})|(?<![a-z0-9])(tt\d{7,8})(?![a-z0-9]))", re.I)
_YEAR_TOKEN = re.compile(r"^(?:19|20)\d{2}$")
_BRACKET_TAG_RE = re.compile(r"\[[^\]]*\]")
_GROUP_SUFFIX = re.compile(r"-[A-Za-z0-9]+$")


def parse_imdb_id(text: str) -> Optional[str]:
    if not text:
        return None
    m = _IMDB_RE.search(text)
    if not m:
        return None
    for g in m.groups():
        if g:
            return g.lower()
    return None


def _normalize_stem(filename: str) -> str:
    name = Path(filename).name.strip()
    # Trailing dots (common when copying names) — not a real extension
    while name.endswith(".") and name.count(".") > 1:
        # keep stripping only a bare trailing dot, not .mkv
        if Path(name).suffix.lower() in {
            ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".webm", ".wmv", ".mpg", ".mpeg"
        }:
            break
        name = name[:-1]
    stem = Path(name).stem
    stem = re.sub(r"_av1_boost$", "", stem, flags=re.I)
    stem = re.sub(r"_test_\d+-\d+_av1_boost$", "", stem, flags=re.I)
    stem = stem.strip(" .")
    # Scene group tag: …Atmos-TNT. Only strip it on names that actually look like
    # a scene release — otherwise the pattern eats the last word of a hyphenated
    # title ("Spider-Man" -> "Spider", "Ant-Man" -> "Ant").
    if any(_is_release_tag(t) for t in _tokenize(stem)):
        stem = _GROUP_SUFFIX.sub("", stem)
    return stem


def _tokenize(stem: str) -> List[str]:
    # Dots / underscores / spaces as separators; keep hyphens inside words (Spider-Man)
    s = stem.replace("_", ".").replace(" ", ".")
    s = re.sub(r"\.+", ".", s).strip(".")
    return [t for t in s.split(".") if t]


def _is_release_tag(token: str) -> bool:
    t = token.strip()
    if not t:
        return False
    if _RELEASE_TAG.match(t):
        return True
    if _AUDIO_CHANNEL.match(t):
        return True
    # DTS-HD, DD+, TrueHD already partly covered; also "DTS-HD.MA" split to DTS-HD + MA
    if re.match(r"^(?:dd|ddp|dts|truehd)(?:-?hd)?(?:\+)?$", t, re.I):
        return True
    return False


def parse_release_hints(stem: str) -> Dict[str, Any]:
    """Pull resolution / HDR labels from scene tags in the filename."""
    tokens = _tokenize(stem)
    res = ""
    hdr = ""
    joined = ".".join(tokens).lower()

    for t in tokens:
        tl = t.lower()
        if tl in ("2160p", "4k", "uhd"):
            res = "2160p"
        elif tl == "1080p":
            res = res or "1080p"
        elif tl == "720p":
            res = res or "720p"

    if re.search(r"(?:^|[.\-_])(?:dv|dovi|dolby\.?vision)(?:$|[.\-_])", joined):
        hdr = "DoVi"
    elif re.search(r"(?:^|[.\-_])hdr10(?:\+|plus(?:$|[.\-_]))", joined):
        # "+" needs no trailing boundary (catches "HDR10+DV"); "Plus" does.
        hdr = "HDR10plus"
    elif re.search(r"(?:^|[.\-_])hdr10(?:$|[.\-_])", joined):
        hdr = "HDR10"
    # Bare "hdr" intentionally ignored (false positives like HDREMUX)

    return {"resolution": res, "hdr": hdr}


def parse_release_name(filename: str) -> Dict[str, Any]:
    """
    Extract title / year / imdb from a scene-style or library-style filename.
    Returns {title, year, imdb_id, title_dots, source, hints}.
    """
    stem = _normalize_stem(filename)
    imdb_id = parse_imdb_id(stem)

    # Library style: Title.Name (2019) [imdbid-tt…] - [2160p HDR10]
    paren_year = re.search(r"\(((?:19|20)\d{2})\)", stem)
    if paren_year:
        title_raw = stem[: paren_year.start()]
        title_raw = _IMDB_RE.sub(" ", title_raw)
        title_raw = _BRACKET_TAG_RE.sub(" ", title_raw)
        title_raw = re.sub(r"[._]+", " ", title_raw)
        title = re.sub(r"\s+", " ", title_raw).strip(" -")
        year = int(paren_year.group(1))
        hints = parse_release_hints(stem)
        return {
            "title": title or "Unknown",
            "title_dots": re.sub(r"\s+", ".", title).strip(".") if title else "Unknown",
            "year": year,
            "imdb_id": imdb_id,
            "source": "filename",
            "hints": hints,
        }

    # Scene style: Title.Name.2021.2160p.BluRay.REMUX.HEVC.DV…-GROUP
    work = _IMDB_RE.sub(".", stem)
    work = _BRACKET_TAG_RE.sub(".", work)
    tokens = _tokenize(work)

    year = None
    title_tokens: List[str] = []
    cut_at = len(tokens)

    for i, tok in enumerate(tokens):
        if _YEAR_TOKEN.match(tok):
            # A title can itself end in a year ("Blade Runner 2049", "1917",
            # "2012"), followed by the real release year. Take the last of a
            # consecutive run and leave the earlier ones in the title.
            j = i
            while j + 1 < len(tokens) and _YEAR_TOKEN.match(tokens[j + 1]):
                j += 1
            # Prefer a year that is followed by release tags (2160p, BluRay, …)
            rest = tokens[j + 1 : j + 4]
            if any(_is_release_tag(t) for t in rest) or j > 0:
                year = int(tokens[j])
                cut_at = j
                break
        if _is_release_tag(tok) and i > 0:
            # Title ended before first technical tag (no year)
            cut_at = i
            break

    title_tokens = tokens[:cut_at]
    # If we never found a year but later tokens include one, take first year-like token
    if year is None:
        for i, tok in enumerate(tokens):
            if _YEAR_TOKEN.match(tok) and i > 0:
                j = i
                while j + 1 < len(tokens) and _YEAR_TOKEN.match(tokens[j + 1]):
                    j += 1
                year = int(tokens[j])
                title_tokens = tokens[:j]
                break

    title = " ".join(title_tokens).strip() or "Unknown"
    # Normalize odd leftovers
    title = re.sub(r"\s+", " ", title).strip(" -.")
    title_dots = re.sub(r"\s+", ".", title).strip(".") or "Unknown"
    hints = parse_release_hints(stem)

    result = {
        "title": title,
        "title_dots": title_dots,
        "year": year,
        "imdb_id": imdb_id,
        "source": "filename" if (imdb_id or year or title != "Unknown") else "unknown",
        "hints": hints,
    }

    # guessit fallback when our lightweight parser is weak
    if title == "Unknown" or year is None:
        enriched = _guessit_fallback(filename, result)
        if enriched:
            return enriched
    return result


def _guessit_fallback(filename: str, base: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fill gaps via guessit when the built-in parser misses title/year."""
    g = _guessit_cached(filename)
    if not isinstance(g, dict):
        return None

    title = str(base.get("title") or "Unknown")
    year = base.get("year")
    hints = dict(base.get("hints") or {})

    g_title = g.get("title")
    if title == "Unknown" and g_title:
        if isinstance(g_title, list):
            g_title = " ".join(str(x) for x in g_title)
        title = str(g_title).strip() or title

    if year is None and g.get("year") is not None:
        try:
            year = int(g["year"] if not isinstance(g["year"], list) else g["year"][0])
        except Exception:
            pass

    if not hints.get("resolution"):
        screen = g.get("screen_size")
        if screen:
            s = str(screen).lower()
            if "2160" in s or s in ("4k", "uhd"):
                hints["resolution"] = "2160p"
            elif "1080" in s:
                hints["resolution"] = "1080p"
            elif "720" in s:
                hints["resolution"] = "720p"

    if not hints.get("hdr"):
        # guessit may set other / HDR formats
        other = g.get("other") or []
        if not isinstance(other, list):
            other = [other]
        other_l = " ".join(str(x).lower() for x in other)
        if "dolby vision" in other_l or "dolbyvision" in other_l:
            hints["hdr"] = "DoVi"
        elif "hdr10+" in other_l or "hdr10plus" in other_l:
            hints["hdr"] = "HDR10plus"
        elif "hdr10" in other_l or "hdr" in other_l:
            hints["hdr"] = "HDR10"

    if title == (base.get("title") or "Unknown") and year == base.get("year"):
        return None

    title_dots = re.sub(r"\s+", ".", title).strip(".") or "Unknown"
    return {
        "title": title,
        "title_dots": title_dots,
        "year": year,
        "imdb_id": base.get("imdb_id"),
        "source": "guessit",
        "hints": hints,
    }


def resolution_label(width: Optional[int], height: Optional[int]) -> str:
    w = int(width or 0)
    h = int(height or 0)
    long_edge = max(w, h)
    short_edge = min(w, h) if w and h else 0
    if long_edge >= 3800 or short_edge >= 2100 or h >= 2160:
        return "2160p"
    if long_edge >= 1900 or h >= 1080:
        return "1080p"
    if long_edge >= 1200 or h >= 720:
        return "720p"
    if long_edge > 0:
        return f"{h}p" if h else f"{w}w"
    return ""


def hdr_label(hdr_info: Optional[Dict[str, Any]]) -> str:
    """
    Filename HDR tag for the *encoded output*. "" for SDR (no tag written).

    Dolby Vision is never emitted in the AV1 bitstream (RPU discarded; P5/P4
    sources are skipped). DoVi sources that encode are tagged HDR10, or
    HDR10plus when that layer is kept. Dual-layer DoVi+HDR10+ → "HDR10plus".
    """
    info = hdr_info or {}
    if info.get("is_dovi"):
        if info.get("is_hdr10plus"):
            return "HDR10plus"
        return "HDR10"
    if info.get("is_hdr10plus"):
        return "HDR10plus"
    if info.get("is_hdr"):
        return "HDR10"
    return ""


def resolution_from_target(
    resolution_target: Optional[str],
    video: Optional[Dict[str, Any]] = None,
    hints: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Filename resolution label from preset Resolution Target.
    2160p/4k/uhd → 2160p; 1440p/1080p as-is; source → probe / filename hint.
    """
    target = str(resolution_target or "source").strip().lower()
    if target in ("2160p", "4k", "uhd"):
        return "2160p"
    if target == "1440p":
        return "1440p"
    if target == "1080p":
        return "1080p"
    if target == "720p":
        return "720p"
    # source (or unknown): use actual video / filename
    res = resolution_label((video or {}).get("width"), (video or {}).get("height"))
    if res:
        return res
    return str((hints or {}).get("resolution") or "")


def quality_tag(
    video: Optional[Dict[str, Any]],
    hdr_info: Optional[Dict[str, Any]],
    hints: Optional[Dict[str, Any]] = None,
    resolution_target: Optional[str] = None,
) -> str:
    """Resolution from preset target; HDR from probe. Filename hints only fill SDR gaps."""
    hints = hints or {}
    res = resolution_from_target(resolution_target, video, hints)
    hdr = hdr_label(hdr_info)
    hint_hdr = str(hints.get("hdr") or "")
    # never let a filename "DoVi"/"HDR10plus" claim override a probe that
    # found no such layer (would misname encodes without RPU/HDR10+ JSON).
    # hdr_label returns "" for SDR, so this only ever fills a genuine gap.
    # Filename DoVi hints map to HDR10 — we never emit Dolby Vision.
    if not hdr and hint_hdr:
        hdr = "HDR10" if hint_hdr == "DoVi" else hint_hdr
    parts = [p for p in (res, hdr) if p]
    return " ".join(parts) if parts else "AV1"


def title_to_dots(title: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "", title or "")
    cleaned = re.sub(r"\s+", ".", cleaned.strip())
    return cleaned.strip(".") or "Unknown"



DEFAULT_MOVIE_TEMPLATE = "[name] ([year]) [imdbid-[imdbid]] - [[quality]]"
DEFAULT_EPISODE_TEMPLATE = "[show] - S[season]E[episode] - [epname] - [[quality]]"

_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename_part(val: str) -> str:
    s = _ILLEGAL_FS.sub("", str(val or "")).strip()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .")


def _pad2(n: Any) -> str:
    try:
        return f"{int(n):02d}"
    except Exception:
        return str(n or "")


def _template_values(
    *,
    title: str = "",
    year: Optional[int] = None,
    imdb_id: Optional[str] = None,
    quality: str = "",
    ext: str = "mp4",
    width: Optional[int] = None,
    height: Optional[int] = None,
    original: str = "",
    show: str = "",
    epname: str = "",
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> Dict[str, str]:
    q = (quality or "").strip()
    res = ""
    hdr = ""
    if q:
        parts = q.split(None, 1)
        res = parts[0] if parts else ""
        hdr = parts[1] if len(parts) > 1 else ""
    iid = ""
    if imdb_id:
        iid = str(imdb_id).lower()
        if not iid.startswith("tt"):
            iid = f"tt{iid}"
    season_s = _pad2(season) if season is not None else ""
    episode_s = _pad2(episode) if episode is not None else ""
    sxxexx = f"S{season_s}E{episode_s}" if season_s and episode_s else ""
    name = _sanitize_filename_part(title) or "Unknown"
    show_s = _sanitize_filename_part(show) or name
    return {
        "name": name,
        "title": name,
        "year": str(int(year)) if year else "",
        "imdbid": iid,
        "imdb": iid,
        "quality": _sanitize_filename_part(q),
        "width": str(int(width)) if width else "",
        "height": str(int(height)) if height else "",
        "resolution": res,
        "hdr": hdr,
        "ext": (ext or "mp4").lstrip("."),
        "original": _sanitize_filename_part(original) or name,
        "show": show_s,
        "epname": _sanitize_filename_part(epname),
        "season": season_s,
        "episode": episode_s,
        "sxxexx": sxxexx,
    }


def apply_name_template(template: str, values: Dict[str, str]) -> str:
    """Replace [token] placeholders; strip empty parentheticals/brackets.

    Extension is always appended from the container setting — not a template token.
    """
    out = template or DEFAULT_MOVIE_TEMPLATE
    # Drop legacy [ext] / .[ext] from saved templates
    out = re.sub(r"\.?\[ext\]", "", out, flags=re.I)
    # Match whole tokens only so [imdbid-[imdbid]] keeps the outer "imdbid-" text
    keys = [k for k in values.keys() if k != "ext"]
    token_re = re.compile(
        r"\[("
        + "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
        + r")\]"
    )

    def _sub(m: re.Match) -> str:
        return values.get(m.group(1), "") or ""

    out = token_re.sub(_sub, out)
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"\[\s*\]", "", out)
    out = re.sub(r"(?:\s*-\s*){2,}", " - ", out)  # empty token left " - - "
    out = re.sub(r"\s{2,}", " ", out)
    out = out.strip(" .-")
    ext = (values.get("ext") or "mp4").lstrip(".")
    # Always append container extension (never configurable via template)
    stem = Path(out).name
    # If user somehow left a known video ext in the stem, strip it first
    for known in ("mp4", "webm", "mkv", "m4v", "mov"):
        if stem.lower().endswith("." + known):
            out = out[: -len(known) - 1].rstrip(" .")
            break
    return f"{out}.{ext}" if out else f"Unknown.{ext}"


_EPISODE_RE = re.compile(
    r"(?:^|[.\-_\[\(])(?:s(?P<season>\d{1,2})e(?P<episode>\d{1,3})|(?P<season2>\d{1,2})x(?P<episode2>\d{1,3}))(?:$|[.\-_\]\)])",
    re.I,
)


@lru_cache(maxsize=512)
def _guessit_cached(name: str) -> Optional[Dict[str, Any]]:
    """guessit is ~100-300ms per call and the same name is parsed several times
    per add (release parse, episode detect, parent-dir fallback)."""
    try:
        from guessit import guessit as _guessit

        g = _guessit(name)
        return dict(g) if isinstance(g, dict) else None
    except Exception:
        return None


def detect_episode_info(filename: str) -> Optional[Dict[str, Any]]:
    """Return episode fields if filename looks like a series episode."""
    name = Path(filename).name
    try:
        g = _guessit_cached(name)
        if isinstance(g, dict) and str(g.get("type") or "").lower() == "episode":
            season = g.get("season")
            episode = g.get("episode")
            if isinstance(season, list):
                season = season[0] if season else None
            if isinstance(episode, list):
                episode = episode[0] if episode else None
            show = g.get("title") or ""
            if isinstance(show, list):
                show = " ".join(str(x) for x in show)
            epname = g.get("episode_title") or ""
            if isinstance(epname, list):
                epname = " ".join(str(x) for x in epname)
            year = g.get("year")
            if isinstance(year, list):
                year = year[0] if year else None
            return {
                "kind": "episode",
                "show": str(show).strip(),
                "epname": str(epname).strip(),
                "season": int(season) if season is not None else None,
                "episode": int(episode) if episode is not None else None,
                "year": int(year) if year is not None else None,
            }
    except Exception:
        pass
    m = _EPISODE_RE.search(name)
    if not m:
        return None
    season = m.group("season") or m.group("season2")
    episode = m.group("episode") or m.group("episode2")
    # Title before season marker
    pre = name[: m.start()]
    pre = re.sub(r"[.\-_]+", " ", pre).strip(" -.")
    return {
        "kind": "episode",
        "show": pre or "Unknown",
        "epname": "",
        "season": int(season) if season else None,
        "episode": int(episode) if episode else None,
        "year": None,
    }

@lru_cache(maxsize=256)
def _small_nfos_in(parent: Path) -> tuple:
    """Memoized per directory — a batch add of one folder otherwise re-globs and
    re-stats the whole directory once per file."""
    try:
        return tuple(
            p for p in parent.glob("*.nfo") if p.stat().st_size < 512_000
        )
    except Exception:
        return ()


def find_imdb_in_sidecars(video_path: Path) -> Optional[str]:
    """Scan nearby .nfo / .xml / .txt for an IMDb id (common with scene releases)."""
    video_path = Path(video_path)
    parent = video_path.parent
    stem = video_path.stem
    candidates: List[Path] = []
    for pattern in (
        f"{stem}.nfo",
        f"{stem}.xml",
        "movie.nfo",
        "tvshow.nfo",
    ):
        p = parent / pattern
        if p.is_file():
            candidates.append(p)
    # Any small .nfo in the same folder
    try:
        for p in _small_nfos_in(parent):
            if p not in candidates:
                candidates.append(p)
    except Exception:
        pass

    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        found = parse_imdb_id(text)
        if found:
            return found
        # Kodi/Emby style
        m = re.search(r"<uniqueid[^>]*type=[\"']imdb[\"'][^>]*>\s*(tt\d{7,8})", text, re.I)
        if m:
            return m.group(1).lower()
        m = re.search(r"<id>\s*(tt\d{7,8})\s*</id>", text, re.I)
        if m:
            return m.group(1).lower()
    return None


def _title_score(query: str, candidate: str) -> float:
    q = re.sub(r"[^a-z0-9]+", " ", (query or "").lower()).split()
    c = re.sub(r"[^a-z0-9]+", " ", (candidate or "").lower()).split()
    if not q or not c:
        return 0.0
    qs, cs = set(q), set(c)
    return len(qs & cs) / max(len(qs), 1)


def _tmdb_auth(api_key: str) -> Dict[str, str]:
    """
    TMDB accepts either:
      - v3 API Key  → ?api_key=...
      - v4 Read Access Token (JWT, starts with eyJ) → Authorization: Bearer ...
    """
    key = (api_key or "").strip()
    headers = {"User-Agent": "AV1Queue/1.0", "Accept": "application/json"}
    params: Dict[str, str] = {}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    elif key:
        params["api_key"] = key
    return {"headers": headers, "params": params}


def _tmdb_get(path: str, api_key: str, extra_params: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    auth = _tmdb_auth(api_key)
    if not auth["params"].get("api_key") and "Authorization" not in auth["headers"]:
        return None
    params = dict(auth["params"])
    if extra_params:
        params.update(extra_params)
    url = f"https://api.themoviedb.org/3{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers=auth["headers"])
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None


def _tmdb_search(api_key: str, title: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
    params: Dict[str, str] = {
        "query": title,
        "include_adult": "false",
        "language": "en-US",
    }
    if year:
        params["primary_release_year"] = str(int(year))
        params["year"] = str(int(year))
    data = _tmdb_get("/search/movie", api_key, params)
    if not data:
        return []
    return list(data.get("results") or [])


# Memoizes tmdb_find_movie by (title, year, api_key) so a batch-add of many
# files sharing a title (e.g. episodes of the same season) doesn't repeat the
# same up-to-4 sequential TMDB round-trips for every file.
_tmdb_cache: Dict[Tuple[str, Optional[int], str], Optional[Dict[str, Any]]] = {}
_tmdb_cache_lock = threading.Lock()


def tmdb_find_movie(title: str, year: Optional[int], api_key: str) -> Optional[Dict[str, Any]]:
    """Search TMDB and return {title, year, imdb_id, tmdb_id} or None."""
    key = (api_key or "").strip()
    if not key or not title:
        return None

    cache_key = (title.strip().lower(), year, key)
    with _tmdb_cache_lock:
        if cache_key in _tmdb_cache:
            return _tmdb_cache[cache_key]

    result = _tmdb_find_movie_uncached(title, year, key)

    with _tmdb_cache_lock:
        _tmdb_cache[cache_key] = result
    return result


def _tmdb_find_movie_uncached(title: str, year: Optional[int], key: str) -> Optional[Dict[str, Any]]:
    results = _tmdb_search(key, title, year)
    # Retry without year if nothing matched (wrong year in release name, etc.)
    if not results and year:
        results = _tmdb_search(key, title, None)
    if not results:
        # Soften query: drop Part Two → still try original; try without punctuation noise
        soft = re.sub(r"\b(part|pt)\b\.?\s*\d+\b", "", title, flags=re.I)
        soft = re.sub(r"\s+", " ", soft).strip(" -")
        if soft and soft.lower() != title.lower():
            results = _tmdb_search(key, soft, year) or _tmdb_search(key, soft, None)
    if not results:
        return None

    def rank(r: Dict[str, Any]) -> tuple:
        rd = str(r.get("release_date") or "")[:4]
        year_hit = 1 if year and rd == str(year) else 0
        score = _title_score(title, str(r.get("title") or ""))
        pop = float(r.get("popularity") or 0)
        return (year_hit, score, pop)

    pick = sorted(results, key=rank, reverse=True)[0]
    tmdb_id = pick.get("id")
    if not tmdb_id:
        return None

    imdb_id = None
    ext = _tmdb_get(f"/movie/{int(tmdb_id)}/external_ids", key)
    if ext:
        imdb_id = parse_imdb_id(str(ext.get("imdb_id") or ""))

    release = str(pick.get("release_date") or "")
    out_year = int(release[:4]) if re.match(r"^(?:19|20)\d{2}", release) else year
    out_title = str(pick.get("title") or title).strip() or title

    return {
        "title": out_title,
        "year": out_year,
        "imdb_id": imdb_id,
        "tmdb_id": int(tmdb_id),
        "source": "tmdb",
    }


def build_media_tag(
    input_path: Path,
    *,
    video: Optional[Dict[str, Any]] = None,
    hdr_info: Optional[Dict[str, Any]] = None,
    settings: Optional[Dict[str, Any]] = None,
    container: str = "mp4",
    resolution_target: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build tagging metadata used for queue display and output naming.
    Looks up TMDB when enabled and imdb_id is missing.
    Resolution label comes from preset resolution_target (e.g. 2160p).
    """
    settings = settings or {}
    parsed = parse_release_name(input_path.name)
    # Also scan parent folder name for imdb / year hints
    parent_parsed = parse_release_name(input_path.parent.name)
    if not parsed.get("imdb_id") and parent_parsed.get("imdb_id"):
        parsed["imdb_id"] = parent_parsed["imdb_id"]
    if not parsed.get("year") and parent_parsed.get("year"):
        parsed["year"] = parent_parsed["year"]
    if parsed.get("title") in ("Unknown", "") and parent_parsed.get("title"):
        parsed["title"] = parent_parsed["title"]
        parsed["title_dots"] = parent_parsed["title_dots"]

    lookup_note = ""
    source = "filename"

    if not parsed.get("imdb_id"):
        nfo_id = find_imdb_in_sidecars(input_path)
        if nfo_id:
            parsed["imdb_id"] = nfo_id
            source = "nfo"

    tmdb: Optional[Dict[str, Any]] = None
    if settings.get("tmdb_lookup", True) and not parsed.get("imdb_id"):
        api_key = str(settings.get("tmdb_api_key") or "").strip()
        if not api_key:
            lookup_note = "Add a TMDB API key in Settings > Naming to resolve IMDb IDs"
        else:
            tmdb = tmdb_find_movie(parsed["title"], parsed.get("year"), api_key)
            if tmdb:
                source = "tmdb"
                if tmdb.get("title"):
                    parsed["title"] = tmdb["title"]
                    parsed["title_dots"] = title_to_dots(tmdb["title"])
                if tmdb.get("year"):
                    parsed["year"] = tmdb["year"]
                if tmdb.get("imdb_id"):
                    parsed["imdb_id"] = tmdb["imdb_id"]
                else:
                    lookup_note = "TMDB match found but no IMDb id returned"
            else:
                lookup_note = f"No TMDB match for '{parsed['title']}'" + (
                    f" ({parsed['year']})" if parsed.get("year") else ""
                )

    quality = quality_tag(
        video,
        hdr_info,
        parsed.get("hints"),
        resolution_target=resolution_target,
    )
    ext = "webm" if str(container).lower() == "webm" else "mp4"
    video = video or {}
    ep = detect_episode_info(input_path.name)
    kind = "episode" if ep else "movie"
    if ep and ep.get("show") and parsed.get("title") in ("Unknown", "", None):
        parsed["title"] = ep["show"]
        parsed["title_dots"] = title_to_dots(ep["show"])
    if ep and ep.get("year") and not parsed.get("year"):
        parsed["year"] = ep["year"]

    values = _template_values(
        title=parsed["title"],
        year=parsed.get("year"),
        imdb_id=parsed.get("imdb_id"),
        quality=quality,
        ext=ext,
        width=video.get("width"),
        height=video.get("height"),
        original=input_path.stem,
        show=(ep or {}).get("show") or parsed["title"],
        epname=(ep or {}).get("epname") or "",
        season=(ep or {}).get("season"),
        episode=(ep or {}).get("episode"),
    )
    tpl = (
        str(settings.get("name_template_episode") or DEFAULT_EPISODE_TEMPLATE)
        if kind == "episode"
        else str(settings.get("name_template_movie") or DEFAULT_MOVIE_TEMPLATE)
    )
    library_name = apply_name_template(tpl, values)

    return {
        "title": parsed["title"],
        "title_dots": parsed.get("title_dots") or title_to_dots(parsed["title"]),
        "year": parsed.get("year"),
        "imdb_id": parsed.get("imdb_id"),
        "tmdb_id": (tmdb or {}).get("tmdb_id"),
        "quality": quality,
        "resolution_target": resolution_target or "source",
        "library_name": library_name,
        # Persisted so refresh_media_tag_quality re-renders [original] from the
        # same value this pass used, instead of falling back to the title.
        "original": input_path.stem,
        "media_kind": kind,
        "show": values.get("show") or "",
        "epname": values.get("epname") or "",
        "season": (ep or {}).get("season"),
        "episode": (ep or {}).get("episode"),
        "source": source if parsed.get("imdb_id") else ("partial" if parsed.get("year") else "guess"),
        "verified": False,
        "hints": parsed.get("hints") or {},
        "lookup_note": lookup_note,
    }


def refresh_media_tag_quality(
    media_tag: Dict[str, Any],
    *,
    video: Optional[Dict[str, Any]] = None,
    hdr_info: Optional[Dict[str, Any]] = None,
    resolution_target: Optional[str] = None,
    container: str = "mp4",
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Update quality / library_name when preset resolution target changes."""
    from core.app_settings import load_settings

    tag = dict(media_tag or {})
    settings = settings if isinstance(settings, dict) else load_settings()
    quality = quality_tag(
        video,
        hdr_info,
        tag.get("hints"),
        resolution_target=resolution_target,
    )
    tag["quality"] = quality
    tag["resolution_target"] = resolution_target or "source"
    ext = "webm" if str(container).lower() == "webm" else "mp4"
    video = video or {}
    kind = tag.get("media_kind") or "movie"
    values = _template_values(
        title=tag.get("title") or "Unknown",
        year=tag.get("year"),
        imdb_id=tag.get("imdb_id"),
        quality=quality,
        ext=ext,
        width=video.get("width"),
        height=video.get("height"),
        original=str(tag.get("original") or tag.get("title") or "Unknown"),
        show=tag.get("show") or tag.get("title") or "Unknown",
        epname=tag.get("epname") or "",
        season=tag.get("season"),
        episode=tag.get("episode"),
    )
    tpl = (
        str(settings.get("name_template_episode") or DEFAULT_EPISODE_TEMPLATE)
        if kind == "episode"
        else str(settings.get("name_template_movie") or DEFAULT_MOVIE_TEMPLATE)
    )
    tag["library_name"] = apply_name_template(tpl, values)
    return tag



def library_output_path(
    input_path: Path,
    media_tag: Dict[str, Any],
    *,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """Output path next to source using library naming (test mode keeps a test suffix).

    The basename is whatever ``build_media_tag``/``refresh_media_tag_quality``
    already rendered into ``library_name``. Re-rendering the template here would
    silently disagree with it — this function has no probe, so [width]/[height]
    would resolve empty and the label shown in the UI would not match the file
    actually written to disk.
    """
    cfg = config or {}
    ext = "webm" if str(cfg.get("container", "mp4")).lower() == "webm" else "mp4"
    base = (media_tag or {}).get("library_name") or f"{input_path.stem}.{ext}"
    if Path(base).suffix.lower() != f".{ext}":
        base = f"{Path(base).stem}.{ext}"
    if cfg.get("test_mode"):
        start_tag = str(cfg.get("trim_start", "start")).replace(":", "")
        end_tag = str(cfg.get("trim_end", "end")).replace(":", "")
        stem = Path(base).stem
        base = f"{stem}.test_{start_tag}-{end_tag}.{ext}"
    return str(input_path.parent / base)
