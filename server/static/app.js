// AV1 Queue - Dynamic Frontend Application
document.addEventListener("DOMContentLoaded", () => {
  // State
  let ws = null;
  let queueState = {
    isRunning: false,
    isPaused: false,
    currentJobId: null,
    jobs: []
  };
  const DEFAULT_PRESET_KEY = "av1queue_default_preset";
  const SSIMU2_KEY = "av1queue_ssimu2";
  const AUTOCROP_KEY = "av1queue_autocrop";
  const SSIMU2_POST_KEY = "av1queue_ssimu2_post";
  const EXTRACT_SUBS_KEY = "av1queue_extract_subtitles";
  const STRIP_SUB_CREDITS_KEY = "av1queue_subtitle_strip_credits";
  const CONTAINER_KEY = "av1queue_container";
  const SVT_LP_KEY = "av1queue_svt_lp";
  const SVT_LOW_MEMORY_KEY = "av1queue_svt_low_memory";
  const DEFAULT_NAME_TEMPLATE_MOVIE = "[name] ([year]) [imdbid-[imdbid]] - [[quality]]";
  const DEFAULT_NAME_TEMPLATE_EPISODE = "[show] - S[season]E[episode] - [epname] - [[quality]]";

  function stripExtToken(tpl) {
    return String(tpl || "").replace(/\.?\[ext\]/gi, "").replace(/\s+$/g, "").replace(/\.$/, "");
  }

  /** Mirror core/media_tagging._sanitize_filename_part */
  function sanitizeFilenamePart(val) {
    return String(val || "")
      .replace(/[<>:"/\\|?*\x00-\x1f]/g, "")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/^[.\s]+|[.\s]+$/g, "");
  }

  /** Mirror core/media_tagging.apply_name_template */
  function applyNameTemplate(template, values) {
    let out = template || DEFAULT_NAME_TEMPLATE_MOVIE;
    out = out.replace(/\.?\[ext\]/gi, "");
    const keys = Object.keys(values || {}).filter((k) => k !== "ext")
      .sort((a, b) => b.length - a.length);
    if (keys.length) {
      const tokenRe = new RegExp(
        "\\[(" + keys.map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")\\]",
        "g"
      );
      out = out.replace(tokenRe, (_, key) => values[key] || "");
    }
    out = out.replace(/\(\s*\)/g, "").replace(/\[\s*\]/g, "");
    out = out.replace(/(?:\s*-\s*){2,}/g, " - ").replace(/\s{2,}/g, " ");
    out = out.replace(/^[\s.\-]+|[\s.\-]+$/g, "");
    const ext = String((values && values.ext) || "mp4").replace(/^\./, "");
    for (const known of ["mp4", "webm", "mkv", "m4v", "mov"]) {
      if (out.toLowerCase().endsWith("." + known)) {
        out = out.slice(0, -(known.length + 1)).replace(/[\s.]+$/, "");
        break;
      }
    }
    return out ? `${out}.${ext}` : `Unknown.${ext}`;
  }

  /** Mirror core/media_tagging.resolution_from_target (simplified). */
  function resolutionFromTarget(target, video, hints) {
    const t = String(target || "source").trim().toLowerCase();
    if (t === "2160p" || t === "4k" || t === "uhd") return "2160p";
    if (t === "1440p") return "1440p";
    if (t === "1080p") return "1080p";
    if (t === "720p") return "720p";
    const w = Number((video || {}).width) || 0;
    const h = Number((video || {}).height) || 0;
    const longEdge = Math.max(w, h);
    const shortEdge = w && h ? Math.min(w, h) : 0;
    if (longEdge >= 3800 || shortEdge >= 2100 || h >= 2160) return "2160p";
    if (longEdge >= 1900 || h >= 1080) return "1080p";
    if (longEdge >= 1200 || h >= 720) return "720p";
    if (longEdge > 0) return h ? `${h}p` : `${w}w`;
    return String((hints || {}).resolution || "");
  }

  const AUDIO_LANGS_KEY = "av1queue_audio_languages";
  const AUDIO_BEST_ONLY_KEY = "av1queue_audio_best_only";
  const AUDIO_FORMAT_KEY = "av1queue_audio_format";
  const SUB_LANGS_KEY = "av1queue_subtitle_languages";
  const SUB_KINDS_KEY = "av1queue_subtitle_kinds";
  const ACTIVE_JOB_STATUSES = [
    "EXTRACTING", "FINAL_ENCODE", "REMUXING"
  ];
  // Backend stage_num (from core/queue_manager.py) already matches the 3-node
  // stepper index 1:1 (EXTRACTING=1, FINAL_ENCODE=2, REMUXING=3) — just clamp.
  function stageNumToStep(n) {
    const v = Number(n);
    if (!Number.isFinite(v) || v <= 0) return 0;
    return Math.min(3, v);
  }
  let selectedPreset = localStorage.getItem(DEFAULT_PRESET_KEY) || "";
  let currentBrowserPath = "";
  let currentProbedMedia = null;

  // SVT-AV1-Tritium defaults (fork previously vendored as SVT-AV1-Essential;
  // flag surface confirmed by running `SvtAv1EncApp.exe --help` directly —
  // Tritium has no --full-help, --help alone lists everything).
  // control: toggle (0/1), select (small int range), slider (wide bounded range), number (free / wide unbounded)
  // Labels are always the raw flag name (no --) — no separate human label field.
  // Basic tab already owns: --preset, --crf, --tune
  // Not exposed here, deliberately:
  //  - lp / low-memory: owned by global Settings
  //  - zones: value is a structured frame-range/strength string, not a
  //    toggle/select/slider/number — doesn't fit this array's control shape.
  //    Hand-edit server/presets/builtin/<id>.json svt_params to add a raw "zones" string.
  //  - roi-map-file, fgs-table, qpfile: file-path inputs, same shape problem.
  //  - qindex-offsets, chroma-qindex-offsets, lambda-scale-factors,
  //    sframe-posi/-qp/-qp-offset, force-key-frames, frame-resz-*: comma-
  //    separated structured lists, not a single value.
  //  - luma-y-dc/chroma-u-dc/chroma-u-ac/chroma-v-dc/chroma-v-ac-qindex-offset,
  //    recode-loop: --help prints no default/range for these — not worth
  //    guessing bounds for.
  //  - rc, qp, tbr, mbr, use-q-file, max-qp, min-qp, undershoot-pct,
  //    overshoot-pct, mbr-overshoot-pct, minsection-pct, maxsection-pct,
  //    buf-sz, buf-initial-sz, buf-optimal-sz, pass, stats, passes: VBR/CBR
  //    or multi-pass only — this app always runs single-pass CRF.
  //  - width/height/forced-max-*, frames, skip, nb, color-format, profile,
  //    level, fps-num/-denom, input-depth, inj, inj-frm-rt, enable-stat-report,
  //    asm: inferred from the VapourSynth pipe / not meaningful to override here.
  //  - color-primaries, transfer-characteristics, matrix-coefficients,
  //    color-range, chroma-sample-position, mastering-display, content-light,
  //    dolby-vision-rpu, hdr10plus-json: computed by core/hdr_dovi.py from the
  //    probed source — hand-overriding these would fight the HDR pipeline.
  //  - i/input, output, config, errlog, recon, stat-file, progress,
  //    no-progress, hide-banner, help, color-help, version, svtav1-params:
  //    owned by core/svt_encode.py's own invocation, not user-facing knobs.
  const ESSENTIAL_SVT_SETTINGS = [
    // Film Grain & Noise — AV1 film-grain signaling first, then the independent
    // noise-table family, then RD/filter noise adaptation.
    { key: "film-grain", category: "Film Grain & Noise", control: "slider", min: 0, max: 50, step: 1, default: 0,
      help: "Synthetic film grain. 0 = off; 1–50 = grain strength (and denoising level if film-grain-denoise is on)." },
    { key: "film-grain-denoise", category: "Film Grain & Noise", control: "toggle", min: 0, max: 1, step: 1, default: 0,
      help: "Required for film-grain to have any visible effect. 0 = film-grain does nothing; 1 = denoises the source at the film-grain level and replaces it with synthetic grain (uses more CPU/RAM)." },
    { key: "adaptive-film-grain", category: "Film Grain & Noise", control: "toggle", min: 0, max: 1, step: 1, default: 1,
      help: "Adapts film grain block size based on resolution." },
    { key: "noise", category: "Film Grain & Noise", control: "number", min: 0, max: 200, step: 1, default: 0,
      help: "Generates a synthetic noise table independent of film-grain. 0 = off; 1–200 = strength." },
    { key: "noise-chroma", category: "Film Grain & Noise", control: "number", min: -1, max: 200, step: 1, default: -1,
      help: "Chroma noise strength when noise is set. -1 = ~60% of luma automatically; 0 = off; 1–200 = explicit strength." },
    { key: "noise-chroma-from-luma", category: "Film Grain & Noise", control: "toggle", min: 0, max: 1, step: 1, default: 0,
      help: "Derives chroma noise from the luma plane instead of applying it independently." },
    { key: "noise-size", category: "Film Grain & Noise", control: "select", min: -1, max: 13, step: 1, default: -1,
      help: "Grain size for the noise table. -1 = auto." },
    { key: "noise-norm-strength", category: "Film Grain & Noise", control: "select", min: 0, max: 4, step: 1, default: 1,
      help: "Strength of noise normalization applied during rate-distortion decisions (0 = off)." },
    { key: "noise-adaptive-filtering", category: "Film Grain & Noise", control: "select", min: 0, max: 4, step: 1, default: 2,
      help: "Disables CDEF/restoration when noise is high. 0 off; 1 both; 2 default tune behavior; 3 CDEF only; 4 restoration only." },

    { key: "enable-dlf", category: "Loop Filters & Deringing", control: "select", min: 0, max: 2, step: 1, default: 1,
      help: "Deblocking loop filter control. 0 off; 1 on (default); 2 more accurate filter (modest extra cost)." },
    { key: "enable-variance-boost", category: "Variance & Adaptive Quality", control: "toggle", min: 0, max: 1, step: 1, default: 1,
      help: "Improves quality allocation based on local variance. Strength 3 is PQ/HDR-oriented." },
    { key: "variance-boost-strength", category: "Variance & Adaptive Quality", control: "select", min: 1, max: 4, step: 1, default: 2,
      help: "Strength of Variance Boost (1–4). Curve 3 is optimized for PQ HDR content." },
    { key: "variance-octile", category: "Variance & Adaptive Quality", control: "select", min: 1, max: 8, step: 1, default: 5,
      help: "Octile used by Variance Boost." },
    { key: "variance-boost-curve", category: "Variance & Adaptive Quality", control: "select", min: 0, max: 3, step: 1, default: 0,
      help: "Selects which response curve Variance Boost uses when mapping local variance to QP adjustment." },
    { key: "qp-scale-compress-strength", category: "Variance & Adaptive Quality", control: "slider", min: 0, max: 8, step: 0.05, default: 1.0,
      help: "Increases temporal quality consistency, especially with film grain or fast motion. Usually not advised above 3." },
    { key: "max-tx-size", category: "Transform & Mode Decision", control: "select", min: 32, max: 64, step: 32, default: 64,
      help: "Limits allowed transform sizes to 32 or 64 (default 64)." },
    { key: "ac-bias", category: "Transform & Mode Decision", control: "slider", min: 0, max: 8, step: 0.05, default: 1.0,
      help: "Strength of AC bias in rate-distortion (0–8)." },
    { key: "kf-tf-strength", category: "Temporal Filtering", control: "select", min: 0, max: 4, step: 1, default: 1,
      help: "Adjust temporal filtering strength specifically for keyframes (mirrors tf-strength, keyframe-only)." },
    { key: "alt-lambda-factors", category: "Transform & Mode Decision", control: "toggle", min: 0, max: 1, step: 1, default: 1,
      help: "Enable alternate rate-distortion lambda weighting factors." },
    { key: "sharp-tx", category: "Transform & Mode Decision", control: "toggle", min: 0, max: 1, step: 1, default: 1,
      help: "Biases transform selection toward sharper detail retention." },
    { key: "alt-ssim-tuning", category: "Transform & Mode Decision", control: "toggle", min: 0, max: 1, step: 1, default: 0,
      help: "Enables alternate SSIM-oriented rate-distortion tuning adjustments (pairs well with --tune 2/SSIM)." },
    { key: "hbd-mds", category: "Transform & Mode Decision", control: "select", min: 0, max: 2, step: 1, default: 0,
      help: "High bit-depth mode decision. 0 = preset-determined; 1 = 10-bit; 2 = hybrid 8/10-bit." },
    { key: "tx-bias", category: "Transform & Mode Decision", control: "select", min: 0, max: 3, step: 1, default: 0,
      help: "Transform size/type bias. 0 off; 1 full; 2 size only; 3 interpolation only." },
    { key: "complex-hvs", category: "Transform & Mode Decision", control: "toggle", min: 0, max: 1, step: 1, default: 0,
      help: "Highest-complexity HVS model. Can improve psychovisual quality at extra CPU cost." },
    { key: "cdef-scaling", category: "Loop Filters & Deringing", control: "slider", min: 1, max: 30, step: 1, default: 15,
      help: "Scales CDEF strength computation. 15 = 1× (default); 8 ≈ 0.5×; 30 = 2×; 1 = minimum." },
    { key: "auto-tiling", category: "Tiling, Decode & Screen Content", control: "toggle", min: 0, max: 1, step: 1, default: 1,
      help: "Automatically sets tiles for the source resolution to improve decode performance with minimal efficiency loss." },
    { key: "enable-tf", category: "Temporal Filtering", control: "select", min: 0, max: 3, step: 1, default: 1,
      help: "Enable ALT-REF (temporally filtered) frames. Mode 3 enables a stronger filter on all frames (useful as a temporal denoiser)." },
    { key: "enable-overlays", category: "Temporal Filtering", control: "toggle", min: 0, max: 1, step: 1, default: 0,
      help: "Insert overlay pictures as an extra reference for the base layer." },
    { key: "enable-cdef", category: "Loop Filters & Deringing", control: "toggle", min: 0, max: 1, step: 1, default: 1,
      help: "Constrained Directional Enhancement Filter (deringing)." },
    { key: "enable-restoration", category: "Loop Filters & Deringing", control: "toggle", min: 0, max: 1, step: 1, default: 1,
      help: "Loop restoration filter." },
    { key: "enable-mfmv", category: "Temporal Filtering", control: "select", min: -1, max: 1, step: 1, default: -1,
      help: "Motion field motion vector prediction. -1 auto (default); 0 off; 1 on." },
    { key: "enable-dg", category: "Temporal Filtering", control: "toggle", min: 0, max: 1, step: 1, default: 1,
      help: "Dynamic GoP control." },
    { key: "scm", category: "Tiling, Decode & Screen Content", control: "select", min: 0, max: 3, step: 1, default: 2,
      help: "Screen content detection. 0 off, 1 on, 2 content-adaptive (default), 3 adaptive + anti-alias aware." },
    { key: "enable-alt-cdef", category: "Loop Filters & Deringing", control: "select", min: 0, max: 3, step: 1, default: 0,
      help: "Alternative CDEF trade-offs: typically weaker deringing but better fidelity. Higher values can raise distortion at fast presets. 2–3 force best CDEF quality (slower)." },
    { key: "enable-alt-dlf", category: "Loop Filters & Deringing", control: "select", min: 0, max: 3, step: 1, default: 0,
      help: "Alternative DLF trade-offs: typically weaker deblocking but better fidelity. Pair with enable-dlf 2 for best loop-filter quality (slower)." },
    { key: "enable-daala", category: "Loop Filters & Deringing", control: "select", min: 0, max: 4, step: 1, default: 0,
      help: "Daala perceptual distortion metric (frequency-domain masking). 0 off; 1 CDEF; 2 + TX search / MDS3; 3 + DCT TX; 4 + MDS0 / IFS RD / OBMC." },
    { key: "enable-daala-rd", category: "Loop Filters & Deringing", control: "toggle", min: 0, max: 1, step: 1, default: 0,
      help: "Use Daala distortion in model RD curvfit. Requires enable-daala." },
    { key: "enable-daala-filtering", category: "Loop Filters & Deringing", control: "select", min: 0, max: 3, step: 1, default: 0,
      help: "Use Daala distortion in filtering decisions (0–3). Requires enable-daala." },
    { key: "fast-decode", category: "Tiling, Decode & Screen Content", control: "select", min: 0, max: 2, step: 1, default: 0,
      help: "Fast decoder levels. Higher values favor easier decode at some efficiency/quality cost." },

    // --- Rate Control ---
    { key: "aq-mode", category: "Variance & Adaptive Quality", control: "select", min: 0, max: 2, step: 1, default: 2,
      help: "Adaptive QP allocation. 0 off; 1 variance-based (AV1 segments); 2 deltaQ prediction-efficiency." },
    { key: "use-fixed-qindex-offsets", category: "Rate Control & QP Bias", control: "select", min: 0, max: 2, step: 1, default: 0,
      help: "Overrides the encoder's per-layer hierarchical QP assignment with fixed Q-index offsets instead." },
    { key: "key-frame-qindex-offset", category: "Rate Control & QP Bias", control: "number", min: -256, max: 255, step: 1, default: 0,
      help: "Q-index offset applied to keyframes only." },
    { key: "key-frame-chroma-qindex-offset", category: "Rate Control & QP Bias", control: "number", min: -256, max: 255, step: 1, default: 0,
      help: "Chroma Q-index offset applied to keyframes only." },
    { key: "gop-constraint-rc", category: "Rate Control & QP Bias", control: "toggle", min: 0, max: 1, step: 1, default: 0,
      help: "Constrains rate control to hit the target rate within each GOP individually, rather than averaging across the whole encode." },
    { key: "enable-qm", category: "Quantization Matrices", control: "toggle", min: 0, max: 1, step: 1, default: 1,
      help: "Enables quantization matrices, which bias quantization by frequency for better perceptual quality." },
    { key: "qm-min", category: "Quantization Matrices", control: "select", min: 0, max: 15, step: 1, default: 6,
      help: "Minimum quant-matrix flatness." },
    { key: "qm-max", category: "Quantization Matrices", control: "select", min: 0, max: 15, step: 1, default: 10,
      help: "Maximum quant-matrix flatness." },
    { key: "chroma-qm-min", category: "Quantization Matrices", control: "select", min: 0, max: 15, step: 1, default: 8,
      help: "Minimum chroma quant-matrix flatness." },
    { key: "chroma-qm-max", category: "Quantization Matrices", control: "select", min: 0, max: 15, step: 1, default: 15,
      help: "Maximum chroma quant-matrix flatness." },
    { key: "tf-strength", category: "Temporal Filtering", control: "select", min: 0, max: 4, step: 1, default: 1,
      help: "Adjusts temporal-filtering strength on non-keyframes (see kf-tf-strength for keyframes)." },
    { key: "luminance-qp-bias", category: "Rate Control & QP Bias", control: "number", min: 0, max: 100, step: 1, default: 0,
      help: "Biases a frame's QP based on its average luma value." },
    { key: "sharpness", category: "Rate Control & QP Bias", control: "select", min: -7, max: 7, step: 1, default: 1,
      help: "Biases the encoder toward decreased (negative) or increased (positive) sharpness." },

    // --- GOP size and type ---
    { key: "keyint", category: "GOP & Keyframes", control: "number", min: -2, max: 100000, step: 1, default: -2,
      help: "Max GOP size in frames. -2 = ~10 seconds (up to 305 frames); -1 = infinite (CRF only); 0 = same as -1." },
    { key: "min-keyint", category: "GOP & Keyframes", control: "number", min: -1, max: 100000, step: 1, default: -1,
      help: "Min GOP size in frames. -1 = automatic (a multiple of the mini-GOP length); 0 = no minimum." },
    { key: "irefresh-type", category: "GOP & Keyframes", control: "select", min: 1, max: 2, step: 1, default: 2,
      help: "Intra refresh type. 1 = forward frame (open GOP); 2 = key frame (closed GOP)." },
    { key: "scd", category: "GOP & Keyframes", control: "toggle", min: 0, max: 1, step: 1, default: 1,
      help: "Scene-change detection, used to place keyframes at hard cuts." },
    { key: "lookahead", category: "GOP & Keyframes", control: "number", min: -1, max: 120, step: 1, default: -1,
      help: "Frames of lookahead beyond the mini-GOP, temporal filtering, and rate control. -1 = auto." },
    { key: "hierarchical-levels", category: "GOP & Keyframes", control: "select", min: 2, max: 5, step: 1, default: 4,
      help: "Temporal layers beyond the base layer (2 = 3 layers ... 5 = 6 layers). Tritium's own unset default is preset-dependent (5 at presets ≤ M12, else 4) — the value shown here is just this app's own baseline." },
    { key: "pred-struct", category: "GOP & Keyframes", control: "select", min: 1, max: 2, step: 1, default: 2,
      help: "Prediction structure. 1 = low-delay; 2 = random access." },
    { key: "rtc", category: "GOP & Keyframes", control: "toggle", min: 0, max: 1, step: 1, default: 0,
      help: "Forces low-delay prediction with fast real-time-conferencing settings. Not relevant for offline library encoding." },
    { key: "startup-mg-size", category: "GOP & Keyframes", control: "number", min: 0, max: 4, step: 1, default: 0,
      help: "Alternate mini-GOP size for the first mini-GOP after a keyframe. 0 = off; valid values are 0, 2, 3, or 4 (1 is invalid)." },
    { key: "startup-qp-offset", category: "GOP & Keyframes", control: "number", min: -63, max: 63, step: 1, default: 0,
      help: "QP offset applied to the startup GOP before picture-QP derivation." },

    // --- AV1-specific ---
    { key: "tile-rows", category: "Tiling, Decode & Screen Content", control: "select", min: 0, max: 6, step: 1, default: 1,
      help: "Tile rows as a power of two (TileRow = 2^n). Ignored while auto-tiling is on." },
    { key: "tile-columns", category: "Tiling, Decode & Screen Content", control: "select", min: 0, max: 4, step: 1, default: 1,
      help: "Tile columns as a power of two (TileCol = 2^n). Ignored while auto-tiling is on." },
    { key: "lossless", category: "S-Frames & Special Modes", control: "toggle", min: 0, max: 1, step: 1, default: 0,
      help: "Enables lossless coding. Massively increases file size — not meant for a delivery library." },
    { key: "avif", category: "S-Frames & Special Modes", control: "toggle", min: 0, max: 1, step: 1, default: 0,
      help: "Enables still-picture (AVIF) coding mode. Not applicable to video encodes." },
    { key: "superres-mode", category: "Super-Resolution & Resize", control: "select", min: 0, max: 4, step: 1, default: 0,
      help: "Encode at a lower resolution and upscale on decode. 0 = off; 4 = auto-select mode." },
    { key: "superres-denom", category: "Super-Resolution & Resize", control: "select", min: 8, max: 16, step: 1, default: 8,
      help: "Super-resolution denominator (8 = no scaling, 16 = half). Only applies when superres-mode = 1." },
    { key: "superres-kf-denom", category: "Super-Resolution & Resize", control: "select", min: 8, max: 16, step: 1, default: 8,
      help: "Super-resolution denominator for keyframes only. Only applies when superres-mode = 1." },
    { key: "superres-qthres", category: "Super-Resolution & Resize", control: "slider", min: 0, max: 63, step: 1, default: 43,
      help: "Q-index threshold that triggers super-resolution. Only applies when superres-mode = 3." },
    { key: "superres-kf-qthres", category: "Super-Resolution & Resize", control: "slider", min: 0, max: 63, step: 1, default: 43,
      help: "Q-index threshold for keyframes. Only applies when superres-mode = 3." },
    { key: "sframe-dist", category: "S-Frames & Special Modes", control: "number", min: 0, max: 100000, step: 1, default: 0,
      help: "S-Frame interval in frames. 0 = off." },
    { key: "sframe-mode", category: "S-Frames & Special Modes", control: "select", min: 1, max: 3, step: 1, default: 2,
      help: "How an S-Frame is chosen once sframe-dist is set ([1–3]). 2 = the next ALT-REF frame becomes the S-Frame (default)." },
    { key: "resize-mode", category: "Super-Resolution & Resize", control: "select", min: 0, max: 4, step: 1, default: 0,
      help: "Reference-frame resize mode. 0 off; 1 fixed scale; 2 random scale; 3 dynamic scale; 4 random access." },
    { key: "resize-denom", category: "Super-Resolution & Resize", control: "select", min: 8, max: 16, step: 1, default: 8,
      help: "Resize denominator (8 = no scaling, 16 = half). Only applies when resize-mode = 1." },
    { key: "resize-kf-denom", category: "Super-Resolution & Resize", control: "select", min: 8, max: 16, step: 1, default: 8,
      help: "Resize denominator for keyframes only. Only applies when resize-mode = 1." }
  ];

  // Display order for the Advanced tab's category sections (grouping is by
  // def.category above; anything with an unlisted category sorts to the end).
  const SVT_CATEGORY_ORDER = [
    "Film Grain & Noise",
    "Variance & Adaptive Quality",
    "Transform & Mode Decision",
    "Loop Filters & Deringing",
    "Temporal Filtering",
    "Rate Control & QP Bias",
    "Quantization Matrices",
    "GOP & Keyframes",
    "Tiling, Decode & Screen Content",
    "Super-Resolution & Resize",
    "S-Frames & Special Modes",
  ];

  function helpTipHtml(text) {
    if (!text) return "";
    // Collapse whitespace within each line, but keep line breaks (e.g. the
    // --flag-name / description / Default: X convention) — rendered via
    // white-space: pre-line on .setting-help-tip.
    const tip = String(text).split("\n").map(l => l.replace(/\s+/g, " ").trim()).join("\n");
    return `<span class="setting-help" tabindex="0" aria-label="Help"><span class="setting-help-icon">?</span><span class="setting-help-tip" role="tooltip">${escapeHtml(tip)}</span></span>`;
  }

  function labelWithHelp(labelText, helpText) {
    return `<span class="setting-label-text">${escapeHtml(labelText)}</span>${helpTipHtml(helpText)}`;
  }

  function hideSettingHelpTip(tip) {
    if (!tip) return;
    tip.classList.remove("is-visible");
    tip.style.left = "";
    tip.style.top = "";
  }

  function positionSettingHelpTip(helpEl) {
    const tip = helpEl?.querySelector?.(".setting-help-tip");
    const icon = helpEl?.querySelector?.(".setting-help-icon") || helpEl;
    if (!tip || !icon) return;
    tip.classList.add("is-visible");
    // Measure after visible so size is correct
    const iconRect = icon.getBoundingClientRect();
    const tipRect = tip.getBoundingClientRect();
    const margin = 8;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    let left = iconRect.left;
    let top = iconRect.bottom + margin;

    // Prefer below; flip above if clipped by viewport bottom
    if (top + tipRect.height > vh - margin && iconRect.top - margin - tipRect.height >= margin) {
      top = iconRect.top - tipRect.height - margin;
    }
    // Clamp horizontally
    if (left + tipRect.width > vw - margin) {
      left = Math.max(margin, vw - tipRect.width - margin);
    }
    if (left < margin) left = margin;
    // Clamp vertically as last resort
    if (top + tipRect.height > vh - margin) {
      top = Math.max(margin, vh - tipRect.height - margin);
    }
    if (top < margin) top = margin;

    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(top)}px`;
  }

  function wireSettingHelpTips(root = document) {
    root.querySelectorAll(".setting-help").forEach(helpEl => {
      if (helpEl.dataset.helpWired === "1") return;
      helpEl.dataset.helpWired = "1";
      const tip = helpEl.querySelector(".setting-help-tip");
      if (!tip) return;

      const show = () => positionSettingHelpTip(helpEl);
      const hide = () => hideSettingHelpTip(tip);

      helpEl.addEventListener("mouseenter", show);
      helpEl.addEventListener("mouseleave", hide);
      helpEl.addEventListener("focus", show);
      helpEl.addEventListener("blur", hide);
    });
  }

  function hideAllSettingHelpTips() {
    document.querySelectorAll(".setting-help-tip.is-visible").forEach(hideSettingHelpTip);
  }

  // Keep fixed tips from floating away while scrolling preset panels
  document.querySelectorAll(
    "#presetBasicSection .preset-settings-fields, #presetAudioSection .preset-settings-fields, #presetAdvancedSection .preset-advanced-fields"
  ).forEach(scroller => {
    scroller.addEventListener("scroll", hideAllSettingHelpTips, { passive: true });
  });
  window.addEventListener("resize", hideAllSettingHelpTips);

  // DOM Elements
  const cpuVal = document.getElementById("cpuVal");
  const cpuBar = document.getElementById("cpuBar");
  const gpuVal = document.getElementById("gpuVal");
  const gpuBar = document.getElementById("gpuBar");
  const ramVal = document.getElementById("ramVal");
  const ramBar = document.getElementById("ramBar");

  const btnStartQueue = document.getElementById("btnStartQueue");
  const btnPauseQueue = document.getElementById("btnPauseQueue");
  const btnStopQueue = document.getElementById("btnStopQueue");


  const activeJobSection = document.getElementById("activeJobSection");
  const activeFileName = document.getElementById("activeFileName");
  const activeFps = document.getElementById("activeFps");
  const activeElapsed = document.getElementById("activeElapsed");
  const activeRemaining = document.getElementById("activeRemaining");
  const activeStageText = document.getElementById("activeStageText");
  const activePct = document.getElementById("activePct");
  const activeProgressFill = document.getElementById("activeProgressFill");
  const terminalLogs = document.getElementById("terminalLogs");
  const terminalBox = document.getElementById("terminalBox");
  const btnToggleTerminal = document.getElementById("btnToggleTerminal");

  // Final-encode ETA: average FPS samples while stage_num === 2 (FINAL_ENCODE)
  const finalEtaState = { jobId: null, fpsSum: 0, fpsN: 0 };

  const queueCountBadge = document.getElementById("queueCountBadge");
  const queueList = document.getElementById("queueList");
  const dropZone = document.getElementById("dropZone");
  const tabQueueBadge = document.getElementById("tabQueueBadge");
  const tabHistoryBadge = document.getElementById("tabHistoryBadge");

  // Modal Elements
  const browseModal = document.getElementById("browseModal");
  const btnCloseModal = document.getElementById("btnCloseModal");
  const btnBrowserUp = document.getElementById("btnBrowserUp");
  const browserCurrentPath = document.getElementById("browserCurrentPath");
  const browserItemsList = document.getElementById("browserItemsList");
  const inspectorPanel = document.getElementById("inspectorPanel");
  const browserChrome = document.getElementById("browserChrome");
  const inspFileName = document.getElementById("inspFileName");
  const inspVideoMeta = document.getElementById("inspVideoMeta");
  const inspAudioTracks = document.getElementById("inspAudioTracks");
  const btnConfirmAddJob = document.getElementById("btnConfirmAddJob");

  // Preset Manager Elements
  const presetListContainer = document.getElementById("presetListContainer");
  const btnNewPreset = document.getElementById("btnNewPreset");
  const presetModal = document.getElementById("presetModal");
  const btnClosePresetModal = document.getElementById("btnClosePresetModal");
  const btnCancelPresetModal = document.getElementById("btnCancelPresetModal");
  const btnSavePreset = document.getElementById("btnSavePreset");
  const presetModalTitle = document.getElementById("presetModalTitle");
  const pEditId = document.getElementById("pEditId");
  const pEditName = document.getElementById("pEditName");
  const pEditCrf = document.getElementById("pEditCrf");
  const pEditPreset = document.getElementById("pEditPreset");
  const pEditResolution = document.getElementById("pEditResolution");
  const pEditAudio51 = document.getElementById("pEditAudio51");
  const pEditAudioStereo = document.getElementById("pEditAudioStereo");
  const pEditIsDefault = document.getElementById("pEditIsDefault");
  const pEditTune = document.getElementById("pEditTune");
  const globalSsimu2 = document.getElementById("globalSsimu2");

  const AUDIO_LANG_OPTIONS = [
    { code: "eng", label: "English" },
    { code: "ces", label: "Czech" },
    { code: "deu", label: "German" },
    { code: "fra", label: "French" },
    { code: "spa", label: "Spanish" },
    { code: "jpn", label: "Japanese" },
    { code: "kor", label: "Korean" },
    { code: "zho", label: "Chinese" },
    { code: "ita", label: "Italian" },
    { code: "pol", label: "Polish" },
    { code: "rus", label: "Russian" },
    { code: "und", label: "Undetermined" }
  ];
  const DEFAULT_AUDIO_LANGUAGES = ["eng"];
  const DEFAULT_SUB_LANGUAGES = ["eng"];
  const ALL_SUB_KINDS = ["standard", "forced", "sdh"];
  const DEFAULT_SUB_KINDS = ["standard"];

  function langFamily(lang) {
    const l = String(lang || "und").toLowerCase().replace("_", "-");
    if (["eng", "en", "en-us", "en-gb"].includes(l)) return "eng";
    if (["ces", "cze", "cs", "cz"].includes(l)) return "ces";
    if (["deu", "ger", "de"].includes(l)) return "deu";
    if (["fra", "fre", "fr"].includes(l)) return "fra";
    if (["spa", "es", "es-es", "es-419"].includes(l)) return "spa";
    if (["jpn", "ja", "jp"].includes(l)) return "jpn";
    if (["kor", "ko"].includes(l)) return "kor";
    if (["zho", "chi", "zh", "zh-cn", "zh-tw", "cmn"].includes(l)) return "zho";
    if (["ita", "it"].includes(l)) return "ita";
    if (["pol", "pl"].includes(l)) return "pol";
    if (["rus", "ru"].includes(l)) return "rus";
    if (["und", "unknown", ""].includes(l)) return "und";
    const primary = l.split("-")[0];
    if (primary !== l) return langFamily(primary);
    return l;
  }

  const LANG_TONE_KNOWN = new Set([
    "eng", "ces", "deu", "fra", "spa", "jpn", "kor", "zho", "ita", "pol", "rus", "und"
  ]);

  /** CSS tone class for language codes (ENG / CES / …). */
  function langToneClass(lang) {
    const fam = langFamily(lang) || "und";
    if (LANG_TONE_KNOWN.has(fam)) return `lang-${fam}`;
    let h = 0;
    for (let i = 0; i < fam.length; i++) h = (h * 31 + fam.charCodeAt(i)) >>> 0;
    return `lang-tone-${h % 8}`;
  }

  function loadLangList(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return [...fallback];
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [...fallback];
      return parsed.map(langFamily).filter(Boolean);
    } catch (_) {
      return [...fallback];
    }
  }

  function saveLangList(key, list) {
    localStorage.setItem(key, JSON.stringify(list));
  }

  function loadSubKinds() {
    try {
      const raw = localStorage.getItem(SUB_KINDS_KEY);
      if (!raw) return [...DEFAULT_SUB_KINDS];
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [...DEFAULT_SUB_KINDS];
      const allowed = new Set(ALL_SUB_KINDS);
      return parsed.map(k => String(k).toLowerCase()).filter(k => allowed.has(k));
    } catch (_) {
      return [...DEFAULT_SUB_KINDS];
    }
  }

  let pipelineAudioLanguages = loadLangList(AUDIO_LANGS_KEY, DEFAULT_AUDIO_LANGUAGES);
  let pipelineSubLanguages = loadLangList(SUB_LANGS_KEY, DEFAULT_SUB_LANGUAGES);
  let pipelineSubKinds = loadSubKinds();

  let allPresets = [];

  function getSsimu2Mode() {
    return (globalSsimu2 && globalSsimu2.value) || localStorage.getItem(SSIMU2_KEY) || "auto";
  }

  function getContainerFormat() {
    const el = document.getElementById("globalContainer");
    const raw = (el && el.value) || localStorage.getItem(CONTAINER_KEY) || "mp4";
    return raw === "webm" ? "webm" : "mp4";
  }

  function containerLabel(fmt) {
    return fmt === "webm" ? "WebM" : "MP4";
  }

  function syncContainerUi() {
    const fmt = getContainerFormat();
    const formatVal = document.getElementById("pipelineFormatVal");
    if (formatVal) formatVal.textContent = containerLabel(fmt);
    const muxLbl = document.getElementById("stepMuxLabel");
    if (muxLbl) muxLbl.textContent = "Finalize";
  }

  function getAutocropEnabled() {
    const el = document.getElementById("globalAutocrop");
    if (el && el.getAttribute("aria-checked") != null) {
      return el.getAttribute("aria-checked") === "true";
    }
    const saved = localStorage.getItem(AUTOCROP_KEY);
    return saved === null ? true : saved === "1";
  }

  function getSsimu2PostEnabled() {
    const el = document.getElementById("globalSsimu2Post");
    if (el && el.getAttribute("aria-checked") != null) {
      return el.getAttribute("aria-checked") === "true";
    }
    const saved = localStorage.getItem(SSIMU2_POST_KEY);
    return saved === null ? false : saved === "1";
  }

  function getExtractSubtitlesEnabled() {
    const el = document.getElementById("globalExtractSubtitles");
    if (el && el.getAttribute("aria-checked") != null) {
      return el.getAttribute("aria-checked") === "true";
    }
    const saved = localStorage.getItem(EXTRACT_SUBS_KEY);
    return saved === null ? true : saved === "1";
  }

  function getStripSubCreditsEnabled() {
    const el = document.getElementById("globalStripSubCredits");
    if (el && el.getAttribute("aria-checked") != null) {
      return el.getAttribute("aria-checked") === "true";
    }
    const saved = localStorage.getItem(STRIP_SUB_CREDITS_KEY);
    return saved === null ? true : saved === "1";
  }

  const SUB_KIND_TOGGLE_IDS = {
    standard: "globalSubKindStandard",
    forced: "globalSubKindForced",
    sdh: "globalSubKindSdh"
  };

  function getAudioFormat() {
    const el = document.getElementById("globalAudioFormat");
    const raw = (el && el.value) || localStorage.getItem(AUDIO_FORMAT_KEY) || "opus";
    return raw === "eac3" ? "eac3" : "opus";
  }

  function audioFormatLabel(fmt) {
    return fmt === "eac3" ? "E-AC-3" : "Opus";
  }

  /** Output layout class from channel count (mirrors pipeline.layout_desc). */
  function audioLayoutDesc(track) {
    const tagged = String(track?.layout_desc || "").trim();
    if (tagged) return tagged;
    const ch = Number(track?.channels || 0);
    if (ch >= 5) return "5.1 Surround";
    if (ch === 2) return "Stereo";
    if (ch > 0) return "Mono";
    return "";
  }

  function audioSourceLayoutLabel(track) {
    const layout = String(track?.channel_layout || "").trim();
    if (layout) return layout;
    const ch = Number(track?.channels || 0);
    return ch ? `${ch}ch` : "";
  }

  /** Badge: source channels → encode layout (optional format / bitrate suffix). */
  function audioTrackEncodeBadge(track, opts = {}) {
    const src = audioSourceLayoutLabel(track);
    const dest = audioLayoutDesc(track);
    let badge = src && dest ? `${src} → ${dest}` : (dest || src || "");
    const extras = [opts.formatLabel, opts.bitrate].filter(Boolean).join(" ");
    if (extras) badge = badge ? `${badge} · ${extras}` : extras;
    return badge;
  }

  /** Encoded layout labels for selected tracks: "5.1", "stereo", "mono" (unique, surround-first). */
  function encodedAudioChannelLabels(item) {
    const tracks = item?.media_info?.audio_tracks || [];
    if (!tracks.length) return [];

    const order = item?.config?.audio_tracks_order;
    let selectedTracks;
    if (Array.isArray(order)) {
      if (order.length === 0) return [];
      const byIdx = new Map(tracks.map((t) => [t.stream_index, t]));
      selectedTracks = order.map((i) => byIdx.get(i)).filter(Boolean);
    } else {
      const cfg = item?.config || {};
      const prefs = {
        languages: Array.isArray(cfg.audio_languages) && cfg.audio_languages.length
          ? cfg.audio_languages.map(langFamily)
          : [...DEFAULT_AUDIO_LANGUAGES],
        bestOnly: cfg.audio_best_only !== false
      };
      const auto = getAutoSelectedTrackIds(tracks, prefs);
      selectedTracks = tracks.filter((t) => auto.has(t.stream_index));
    }

    let has51 = false;
    let hasStereo = false;
    let hasMono = false;
    for (const t of selectedTracks) {
      const ch = Number(t.channels || 0);
      if (ch >= 5) has51 = true;
      else if (ch === 2) hasStereo = true;
      else if (ch > 0) hasMono = true;
    }
    const labels = [];
    if (has51) labels.push("5.1");
    if (hasStereo) labels.push("stereo");
    if (hasMono) labels.push("mono");
    return labels;
  }

  function getAudioBestOnly() {
    const el = document.getElementById("globalAudioBestOnly");
    if (el && el.getAttribute("aria-checked") != null) {
      return el.getAttribute("aria-checked") === "true";
    }
    const saved = localStorage.getItem(AUDIO_BEST_ONLY_KEY);
    return saved === null ? true : saved === "1";
  }

  function getPipelineAudioLanguages() {
    return pipelineAudioLanguages.length ? [...pipelineAudioLanguages] : [...DEFAULT_AUDIO_LANGUAGES];
  }

  function getPipelineSubLanguages() {
    return [...pipelineSubLanguages];
  }

  function getPipelineSubKinds() {
    return [...pipelineSubKinds];
  }

  function syncSettingFieldRow(el, resetSelector, toggleClass) {
    if (!el) return;
    const row = el.closest(".setting-row");
    if (!row || !row.querySelector(resetSelector)) return;
    let atDefault = false;
    if (el.classList.contains(toggleClass)) {
      const defOn = el.getAttribute("data-default") === "true";
      atDefault = (el.getAttribute("aria-checked") === "true") === defOn;
    } else {
      const def = el.getAttribute("data-default");
      atDefault = def !== null && String(el.value) === String(def);
    }
    row.classList.toggle("is-default", atDefault);
  }

  // Mirrors core/media_tagging.hdr_label — the tag that goes in the filename.
  // "" for SDR (no tag is written). Filename always tags the HDR10 base layer,
  // even when preserve_dovi_rpu also carries the RPU — see README.md "HDR & Dolby Vision".
  function hdrFilenameTag(hdr) {
    if (!hdr) return "";
    if (hdr.is_dovi) return hdr.is_hdr10plus ? "HDR10plus" : "HDR10";
    if (hdr.is_hdr10plus) return "HDR10plus";
    if (hdr.is_hdr) return "HDR10";
    return "";
  }

  // What the *source* actually carries — more specific than the filename tag,
  // used where we describe the input rather than the finished encode.
  function hdrSourceLabel(hdr) {
    if (!hdr) return "SDR 10-bit";
    if (hdr.is_dovi) {
      const profNum = hdr.dovi_profile != null ? Number(hdr.dovi_profile) : null;
      const compat = hdr.dovi_compat_id != null ? Number(hdr.dovi_compat_id) : null;
      const prof = Number.isFinite(profNum) ? `Profile ${profNum}` : "Profile 8";
      // Policy: P5 (base-layer compat 0) is skipped; every other profile
      // encodes as HDR10 (HDR10+ kept if present). RPU passthrough alongside
      // that HDR10 base layer is opt-in via settings.preserve_dovi_rpu and
      // best-effort — see README.md "HDR & Dolby Vision".
      if (compat === 0 || profNum === 5) {
        return `Dolby Vision (${prof}) — will be skipped (base layer not standalone)`;
      }
      if (profNum === 4 || (!Number.isFinite(compat) && !(profNum === 7 || profNum === 8))) {
        return `Dolby Vision (${prof}) — quarantined (profile/compat unclear)`;
      }
      const rpuSuffix = appSettings.preserve_dovi_rpu ? " + DoVi RPU" : "";
      return hdr.is_hdr10plus
        ? `Dolby Vision (${prof}) + HDR10+ → HDR10+ AV1${rpuSuffix}`
        : `Dolby Vision (${prof}) → HDR10 AV1${rpuSuffix}`;
    }
    if (hdr.dovi_unverified || hdr.dovi_filename_hint) {
      return "DoVi (filename) — quarantined until probe confirms profile";
    }
    if (hdr.is_hdr10plus) return "HDR10+ (BT.2020 PQ)";
    if (hdr.is_hlg) return "HLG (BT.2020)";
    if (hdr.hdr10plus_filename_hint) return "HDR10+ (filename) · HDR10 flags";
    if (hdr.is_hdr) return "HDR10 (BT.2020 PQ)";
    return "SDR 10-bit";
  }

  function syncPipelineFieldRow(el) {
    syncSettingFieldRow(el, ".pipeline-reset", "pipeline-toggle");
    if (el && el.type === "range") {
      const valEl = document.getElementById(`${el.id}_val`);
      if (valEl) valEl.textContent = el.value;
    }
  }

  function setToggle(el, on) {
    if (!el) return;
    const isOn = !!on;
    el.setAttribute("aria-checked", isOn ? "true" : "false");
    el.classList.toggle("is-on", isOn);
    const label = el.querySelector(".svt-toggle-label");
    if (label) label.textContent = isOn ? "On" : "Off";
  }

  function setPipelineToggle(el, on) {
    setToggle(el, on);
  }

  const PIPELINE_TOGGLE_KEYS = {};

  function wireLocalStoragePipelineToggle(el, key, defaultOn, onChange) {
    if (!el) return;
    PIPELINE_TOGGLE_KEYS[el.id] = key;
    const saved = localStorage.getItem(key);
    setPipelineToggle(el, saved === null ? defaultOn : saved === "1");
    syncPipelineFieldRow(el);
    if (onChange) onChange();
    el.addEventListener("click", () => {
      const next = el.getAttribute("aria-checked") !== "true";
      setPipelineToggle(el, next);
      localStorage.setItem(key, next ? "1" : "0");
      syncPipelineFieldRow(el);
      if (onChange) onChange();
    });
  }

  if (globalSsimu2) {
    globalSsimu2.value = localStorage.getItem(SSIMU2_KEY) || "auto";
    syncPipelineFieldRow(globalSsimu2);
    globalSsimu2.addEventListener("change", () => {
      localStorage.setItem(SSIMU2_KEY, globalSsimu2.value);
      syncPipelineFieldRow(globalSsimu2);
    });
  }

  function syncExtractSubsDependentRows() {
    const on = getExtractSubtitlesEnabled();
    [
      "pipelineSubtitleTypesGroup",
      "pipelineSubLangsRow",
      "pipelineStripSubCreditsRow"
    ].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.classList.toggle("hidden", !on);
    });
  }

  wireLocalStoragePipelineToggle(document.getElementById("globalAutocrop"), AUTOCROP_KEY, true);
  wireLocalStoragePipelineToggle(document.getElementById("globalSsimu2Post"), SSIMU2_POST_KEY, false);
  wireLocalStoragePipelineToggle(document.getElementById("globalExtractSubtitles"), EXTRACT_SUBS_KEY, true, syncExtractSubsDependentRows);
  wireLocalStoragePipelineToggle(document.getElementById("globalStripSubCredits"), STRIP_SUB_CREDITS_KEY, true);
  wireLocalStoragePipelineToggle(document.getElementById("globalAudioBestOnly"), AUDIO_BEST_ONLY_KEY, true);

  function reconcileAudioFormatAndContainer() {
    if (getAudioFormat() === "eac3" && getContainerFormat() === "webm") {
      const container = document.getElementById("globalContainer");
      if (container) {
        container.value = "mp4";
        localStorage.setItem(CONTAINER_KEY, "mp4");
        syncPipelineFieldRow(container);
        syncContainerUi();
      }
    }
  }

  const globalAudioFormat = document.getElementById("globalAudioFormat");
  if (globalAudioFormat) {
    const savedFmt = localStorage.getItem(AUDIO_FORMAT_KEY);
    globalAudioFormat.value = savedFmt === "eac3" ? "eac3" : "opus";
    syncPipelineFieldRow(globalAudioFormat);
    globalAudioFormat.addEventListener("change", () => {
      localStorage.setItem(AUDIO_FORMAT_KEY, getAudioFormat());
      syncPipelineFieldRow(globalAudioFormat);
      reconcileAudioFormatAndContainer();
    });
  }

  const globalContainer = document.getElementById("globalContainer");
  if (globalContainer) {
    const savedContainer = localStorage.getItem(CONTAINER_KEY);
    globalContainer.value = savedContainer === "webm" ? "webm" : "mp4";
    syncPipelineFieldRow(globalContainer);
    globalContainer.addEventListener("change", () => {
      if (getContainerFormat() === "webm" && getAudioFormat() === "eac3") {
        const audioFmt = document.getElementById("globalAudioFormat");
        if (audioFmt) {
          audioFmt.value = "opus";
          localStorage.setItem(AUDIO_FORMAT_KEY, "opus");
          syncPipelineFieldRow(audioFmt);
        }
      }
      localStorage.setItem(CONTAINER_KEY, getContainerFormat());
      syncPipelineFieldRow(globalContainer);
      syncContainerUi();
    });
  }
  syncContainerUi();
  reconcileAudioFormatAndContainer();

  document.querySelectorAll("#pipelineSettingsModal .pipeline-reset").forEach(btn => {
    btn.addEventListener("click", () => {
      const target = document.getElementById(btn.getAttribute("data-target"));
      if (!target) return;
      if (target.classList.contains("pipeline-toggle")) {
        const on = target.getAttribute("data-default") === "true";
        setPipelineToggle(target, on);
        if (PIPELINE_TOGGLE_KEYS[target.id]) localStorage.setItem(PIPELINE_TOGGLE_KEYS[target.id], on ? "1" : "0");
        if (target.id === "globalExtractSubtitles") syncExtractSubsDependentRows();
        if (target.id === "globalSvtLowMemory") localStorage.setItem(SVT_LOW_MEMORY_KEY, on ? "1" : "0");
        if (Object.values(SUB_KIND_TOGGLE_IDS).includes(target.id)) persistSubKindsFromUi();
        // Naming toggles: UI only until Save
      } else {
        target.value = target.getAttribute("data-default") || "auto";
        if (target.id === "globalSsimu2") localStorage.setItem(SSIMU2_KEY, target.value);
        if (target.id === "globalSvtLp") localStorage.setItem(SVT_LP_KEY, target.value);
        if (target.id === "globalAudioFormat") {
          localStorage.setItem(AUDIO_FORMAT_KEY, target.value);
          reconcileAudioFormatAndContainer();
        }
        if (target.id === "globalContainer") {
          localStorage.setItem(CONTAINER_KEY, target.value);
          if (target.value === "webm" && getAudioFormat() === "eac3") {
            const audioFmt = document.getElementById("globalAudioFormat");
            if (audioFmt) {
              audioFmt.value = "opus";
              localStorage.setItem(AUDIO_FORMAT_KEY, "opus");
              syncPipelineFieldRow(audioFmt);
            }
          }
          syncContainerUi();
        }
      }
      syncPipelineFieldRow(target);
    });
  });

  const globalSsimu2Target = document.getElementById("globalSsimu2Target");
  if (globalSsimu2Target) {
    globalSsimu2Target.addEventListener("input", () => syncPipelineFieldRow(globalSsimu2Target));
  }

  const pipelineSettingsModal = document.getElementById("pipelineSettingsModal");
  const btnPipelineSettings = document.getElementById("btnPipelineSettings");
  const btnClosePipelineSettings = document.getElementById("btnClosePipelineSettings");
  const btnCancelPipelineSettings = document.getElementById("btnCancelPipelineSettings");
  const btnSavePipelineSettings = document.getElementById("btnSavePipelineSettings");
  const pipelineTabGeneral = document.getElementById("pipelineTabGeneral");
  const pipelineTabApp = document.getElementById("pipelineTabApp");
  const pipelineTabAudio = document.getElementById("pipelineTabAudio");
  const pipelineTabSubtitles = document.getElementById("pipelineTabSubtitles");
  const pipelineTabNaming = document.getElementById("pipelineTabNaming");
  const pipelineTabWatch = document.getElementById("pipelineTabWatch");
  const pipelineGeneralSection = document.getElementById("pipelineGeneralSection");
  const pipelineAppSection = document.getElementById("pipelineAppSection");
  const pipelineAudioSection = document.getElementById("pipelineAudioSection");
  const pipelineSubtitlesSection = document.getElementById("pipelineSubtitlesSection");
  const pipelineNamingSection = document.getElementById("pipelineNamingSection");
  const pipelineWatchSection = document.getElementById("pipelineWatchSection");

  let appSettings = {
    autoname_output: true,
    name_template_movie: DEFAULT_NAME_TEMPLATE_MOVIE,
    name_template_episode: DEFAULT_NAME_TEMPLATE_EPISODE,
    svt_lp: 0,
    svt_low_memory: false,
    ssimu2_target: 80,
    tmdb_lookup: true,
    hdr_strict: true,
    preserve_dovi_rpu: true,
    tmdb_api_key: "",
    subtitle_search: false,
    opensubtitles_api_key: "",
    opensubtitles_username: "",
    opensubtitles_password: "",
    watch_folder_enabled: false,
    watch_folder_path: "",
    watch_folder_output_path: "",
    watch_folder_default_preset: "",
    hour_format: "24",
    allow_builtin_preset_edits: false
  };
  let pipelineSettingsSnapshot = null;

  async function fetchAppSettings() {
    try {
      const res = await fetch("/api/settings");
      const data = await res.json();
      if (data.settings) appSettings = { ...appSettings, ...data.settings };
      applyAppSettingsToUi();
    } catch (e) {
      console.warn("Could not load settings", e);
    }
  }

  function getHourFormat() {
    const el = document.getElementById("globalHourFormat");
    const raw = (el && el.value) || appSettings.hour_format || "24";
    return raw === "12" ? "12" : "24";
  }

  function formatDateTime(epochSec) {
    if (!epochSec) return "";
    const d = new Date(Number(epochSec) * 1000);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleString(undefined, { hour12: getHourFormat() === "12" });
  }

  function applyAppSettingsToUi() {
    const tmdbLookup = document.getElementById("globalTmdbLookup");
    const subSearch = document.getElementById("globalSubtitleSearch");
    if (tmdbLookup) {
      setPipelineToggle(tmdbLookup, appSettings.tmdb_lookup !== false);
      syncPipelineFieldRow(tmdbLookup);
    }
    if (subSearch) {
      setPipelineToggle(subSearch, !!appSettings.subtitle_search);
      syncPipelineFieldRow(subSearch);
    }
    const hdrStrict = document.getElementById("globalHdrStrict");
    if (hdrStrict) {
      setPipelineToggle(hdrStrict, appSettings.hdr_strict !== false);
      syncPipelineFieldRow(hdrStrict);
    }
    const preserveDoviRpu = document.getElementById("globalPreserveDoviRpu");
    if (preserveDoviRpu) {
      setPipelineToggle(preserveDoviRpu, appSettings.preserve_dovi_rpu !== false);
      syncPipelineFieldRow(preserveDoviRpu);
    }
    const hourFormat = document.getElementById("globalHourFormat");
    if (hourFormat) {
      hourFormat.value = appSettings.hour_format === "12" ? "12" : "24";
      syncPipelineFieldRow(hourFormat);
    }
    const allowBuiltinEdits = document.getElementById("globalAllowBuiltinPresetEdits");
    if (allowBuiltinEdits) {
      setPipelineToggle(allowBuiltinEdits, !!appSettings.allow_builtin_preset_edits);
      syncPipelineFieldRow(allowBuiltinEdits);
    }
    const lp = document.getElementById("globalSvtLp");
    if (lp) {
      const n = Number(appSettings.svt_lp);
      lp.value = String(Number.isFinite(n) && n >= 0 && n <= 6 ? n : 0);
      localStorage.setItem(SVT_LP_KEY, lp.value);
      syncPipelineFieldRow(lp);
    }
    const lowMem = document.getElementById("globalSvtLowMemory");
    if (lowMem) {
      setPipelineToggle(lowMem, !!appSettings.svt_low_memory);
      localStorage.setItem(SVT_LOW_MEMORY_KEY, appSettings.svt_low_memory ? "1" : "0");
      syncPipelineFieldRow(lowMem);
    }
    const ssimu2Target = document.getElementById("globalSsimu2Target");
    if (ssimu2Target) {
      const n = Number(appSettings.ssimu2_target);
      ssimu2Target.value = String(Number.isFinite(n) && n >= 0 && n <= 100 ? n : 80);
      syncPipelineFieldRow(ssimu2Target);
    }
    const movieTpl = document.getElementById("globalNameTemplateMovie");
    if (movieTpl) {
      movieTpl.value = stripExtToken(appSettings.name_template_movie || DEFAULT_NAME_TEMPLATE_MOVIE);
      syncPipelineFieldRow(movieTpl);
    }
    const epTpl = document.getElementById("globalNameTemplateEpisode");
    if (epTpl) {
      epTpl.value = stripExtToken(appSettings.name_template_episode || DEFAULT_NAME_TEMPLATE_EPISODE);
      syncPipelineFieldRow(epTpl);
    }
    const tmdbKey = document.getElementById("settingsTmdbApiKey");
    const osKey = document.getElementById("settingsOsApiKey");
    const osUser = document.getElementById("settingsOsUser");
    const osPass = document.getElementById("settingsOsPass");
    if (tmdbKey) tmdbKey.value = appSettings.tmdb_api_key || "";
    if (osKey) osKey.value = appSettings.opensubtitles_api_key || "";
    if (osUser) osUser.value = appSettings.opensubtitles_username || "";
    if (osPass) osPass.value = appSettings.opensubtitles_password || "";

    const watchEnabled = document.getElementById("globalWatchFolderEnabled");
    if (watchEnabled) {
      setPipelineToggle(watchEnabled, !!appSettings.watch_folder_enabled);
      syncPipelineFieldRow(watchEnabled);
    }
    const watchPath = document.getElementById("settingsWatchFolderPath");
    if (watchPath) watchPath.value = appSettings.watch_folder_path || "";
    const watchOutputPath = document.getElementById("settingsWatchFolderOutputPath");
    if (watchOutputPath) watchOutputPath.value = appSettings.watch_folder_output_path || "";
    const watchPreset = document.getElementById("settingsWatchFolderPreset");
    if (watchPreset) {
      watchPreset.innerHTML = `<option value="">(No default — use built-in defaults)</option>` +
        allPresets.map(p => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`).join("");
      watchPreset.value = appSettings.watch_folder_default_preset || "";
    }
  }

  async function persistAppSettings(partial) {
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(partial)
      });
      const data = await res.json();
      if (data.settings) appSettings = { ...appSettings, ...data.settings };
      return true;
    } catch (e) {
      console.warn("Could not save settings", e);
      return false;
    }
  }

  function takePipelineSettingsSnapshot() {
    return {
      appSettings: { ...appSettings },
      ssimu2: getSsimu2Mode(),
      container: getContainerFormat(),
      svtLp: document.getElementById("globalSvtLp")?.value || "0",
      svtLowMemory: document.getElementById("globalSvtLowMemory")?.getAttribute("aria-checked") === "true",
      autocrop: getAutocropEnabled(),
      ssimu2Post: getSsimu2PostEnabled(),
      extractSubs: getExtractSubtitlesEnabled(),
      stripSubCredits: getStripSubCreditsEnabled(),
      audioBestOnly: getAudioBestOnly(),
      audioFormat: getAudioFormat(),
      audioLangs: [...pipelineAudioLanguages],
      subLangs: [...pipelineSubLanguages],
      subKinds: [...pipelineSubKinds]
    };
  }

  function restorePipelineSettingsSnapshot(snap) {
    if (!snap) return;
    appSettings = { ...snap.appSettings };
    applyAppSettingsToUi();

    const ssimu2 = document.getElementById("globalSsimu2");
    if (ssimu2) {
      ssimu2.value = snap.ssimu2 || "auto";
      localStorage.setItem(SSIMU2_KEY, ssimu2.value);
      syncPipelineFieldRow(ssimu2);
    }
    const container = document.getElementById("globalContainer");
    if (container) {
      container.value = snap.container === "webm" ? "webm" : "mp4";
      localStorage.setItem(CONTAINER_KEY, container.value);
      syncPipelineFieldRow(container);
      syncContainerUi();
    }
    const lp = document.getElementById("globalSvtLp");
    if (lp) {
      lp.value = String(snap.svtLp ?? "0");
      localStorage.setItem(SVT_LP_KEY, lp.value);
      syncPipelineFieldRow(lp);
    }
    setPipelineToggle(document.getElementById("globalSvtLowMemory"), !!snap.svtLowMemory);
    localStorage.setItem(SVT_LOW_MEMORY_KEY, snap.svtLowMemory ? "1" : "0");
    syncPipelineFieldRow(document.getElementById("globalSvtLowMemory"));

    setPipelineToggle(document.getElementById("globalAutocrop"), snap.autocrop !== false);
    localStorage.setItem(AUTOCROP_KEY, snap.autocrop !== false ? "1" : "0");
    syncPipelineFieldRow(document.getElementById("globalAutocrop"));

    setPipelineToggle(document.getElementById("globalSsimu2Post"), !!snap.ssimu2Post);
    localStorage.setItem(SSIMU2_POST_KEY, snap.ssimu2Post ? "1" : "0");
    syncPipelineFieldRow(document.getElementById("globalSsimu2Post"));

    setPipelineToggle(document.getElementById("globalExtractSubtitles"), snap.extractSubs !== false);
    localStorage.setItem(EXTRACT_SUBS_KEY, snap.extractSubs !== false ? "1" : "0");
    syncPipelineFieldRow(document.getElementById("globalExtractSubtitles"));
    syncExtractSubsDependentRows();

    setPipelineToggle(document.getElementById("globalStripSubCredits"), snap.stripSubCredits !== false);
    localStorage.setItem(STRIP_SUB_CREDITS_KEY, snap.stripSubCredits !== false ? "1" : "0");
    syncPipelineFieldRow(document.getElementById("globalStripSubCredits"));

    setPipelineToggle(document.getElementById("globalAudioBestOnly"), snap.audioBestOnly !== false);
    localStorage.setItem(AUDIO_BEST_ONLY_KEY, snap.audioBestOnly !== false ? "1" : "0");
    syncPipelineFieldRow(document.getElementById("globalAudioBestOnly"));

    const audioFmt = document.getElementById("globalAudioFormat");
    if (audioFmt) {
      audioFmt.value = snap.audioFormat === "eac3" ? "eac3" : "opus";
      localStorage.setItem(AUDIO_FORMAT_KEY, audioFmt.value);
      syncPipelineFieldRow(audioFmt);
    }
    reconcileAudioFormatAndContainer();

    pipelineAudioLanguages = [...(snap.audioLangs || DEFAULT_AUDIO_LANGUAGES)];
    pipelineSubLanguages = [...(snap.subLangs || DEFAULT_SUB_LANGUAGES)];
    pipelineSubKinds = [...(snap.subKinds || DEFAULT_SUB_KINDS)];
    saveLangList(AUDIO_LANGS_KEY, pipelineAudioLanguages);
    saveLangList(SUB_LANGS_KEY, pipelineSubLanguages);
    localStorage.setItem(SUB_KINDS_KEY, JSON.stringify(pipelineSubKinds));
    syncSubKindToggles();
    renderPipelineAudioLangEditor();
    renderPipelineSubLangEditor();
  }

  function collectPipelineAppSettingsFromUi() {
    const toggleOn = (id) => {
      const el = document.getElementById(id);
      return el ? el.getAttribute("aria-checked") === "true" : false;
    };
    const lpEl = document.getElementById("globalSvtLp");
    let lp = Number(lpEl?.value ?? appSettings.svt_lp ?? 0);
    if (!Number.isFinite(lp) || lp < 0 || lp > 6) lp = 0;
    const ssimu2TargetEl = document.getElementById("globalSsimu2Target");
    let ssimu2Target = Number(ssimu2TargetEl?.value ?? appSettings.ssimu2_target ?? 80);
    if (!Number.isFinite(ssimu2Target) || ssimu2Target < 0 || ssimu2Target > 100) ssimu2Target = 80;
    const movieTpl = stripExtToken(
      (document.getElementById("globalNameTemplateMovie")?.value || "").trim()
      || DEFAULT_NAME_TEMPLATE_MOVIE
    );
    const epTpl = stripExtToken(
      (document.getElementById("globalNameTemplateEpisode")?.value || "").trim()
      || DEFAULT_NAME_TEMPLATE_EPISODE
    );
    return {
      autoname_output: true,
      name_template_movie: movieTpl,
      name_template_episode: epTpl,
      svt_lp: lp,
      svt_low_memory: toggleOn("globalSvtLowMemory"),
      ssimu2_target: ssimu2Target,
      tmdb_lookup: toggleOn("globalTmdbLookup"),
      hdr_strict: toggleOn("globalHdrStrict"),
      preserve_dovi_rpu: toggleOn("globalPreserveDoviRpu"),
      subtitle_search: toggleOn("globalSubtitleSearch"),
      tmdb_api_key: document.getElementById("settingsTmdbApiKey")?.value || "",
      opensubtitles_api_key: document.getElementById("settingsOsApiKey")?.value || "",
      opensubtitles_username: document.getElementById("settingsOsUser")?.value || "",
      opensubtitles_password: document.getElementById("settingsOsPass")?.value || "",
      watch_folder_enabled: toggleOn("globalWatchFolderEnabled"),
      watch_folder_path: (document.getElementById("settingsWatchFolderPath")?.value || "").trim(),
      watch_folder_output_path: (document.getElementById("settingsWatchFolderOutputPath")?.value || "").trim(),
      watch_folder_default_preset: document.getElementById("settingsWatchFolderPreset")?.value || "",
      hour_format: getHourFormat(),
      allow_builtin_preset_edits: toggleOn("globalAllowBuiltinPresetEdits")
    };
  }

  async function savePipelineSettings() {
    // Persist local pipeline prefs (already updated live for most fields)
    localStorage.setItem(SSIMU2_KEY, getSsimu2Mode());
    localStorage.setItem(CONTAINER_KEY, getContainerFormat());
    localStorage.setItem(SVT_LP_KEY, document.getElementById("globalSvtLp")?.value || "0");
    localStorage.setItem(
      SVT_LOW_MEMORY_KEY,
      document.getElementById("globalSvtLowMemory")?.getAttribute("aria-checked") === "true" ? "1" : "0"
    );
    localStorage.setItem(AUTOCROP_KEY, getAutocropEnabled() ? "1" : "0");
    localStorage.setItem(SSIMU2_POST_KEY, getSsimu2PostEnabled() ? "1" : "0");
    localStorage.setItem(EXTRACT_SUBS_KEY, getExtractSubtitlesEnabled() ? "1" : "0");
    localStorage.setItem(STRIP_SUB_CREDITS_KEY, getStripSubCreditsEnabled() ? "1" : "0");
    localStorage.setItem(AUDIO_BEST_ONLY_KEY, getAudioBestOnly() ? "1" : "0");
    localStorage.setItem(AUDIO_FORMAT_KEY, getAudioFormat());
    saveLangList(AUDIO_LANGS_KEY, pipelineAudioLanguages);
    saveLangList(SUB_LANGS_KEY, pipelineSubLanguages);
    persistSubKindsFromUi();

    const naming = collectPipelineAppSettingsFromUi();
    const ok = await persistAppSettings(naming);
    if (!ok) {
      await customAlert("Could not save settings.");
      return;
    }
    pipelineSettingsSnapshot = takePipelineSettingsSnapshot();
    if (typeof renderHistory === "function") renderHistory();
    if (typeof renderPresets === "function") renderPresets();
    if (typeof renderQueue === "function") renderQueue();
    closePipelineSettings();
  }

  function showPipelineSection(which) {
    const sections = {
      general: pipelineGeneralSection,
      app: pipelineAppSection,
      audio: pipelineAudioSection,
      subtitles: pipelineSubtitlesSection,
      naming: pipelineNamingSection,
      watch: pipelineWatchSection
    };
    const tabs = {
      general: pipelineTabGeneral,
      app: pipelineTabApp,
      audio: pipelineTabAudio,
      subtitles: pipelineTabSubtitles,
      naming: pipelineTabNaming,
      watch: pipelineTabWatch
    };
    Object.entries(sections).forEach(([key, el]) => {
      if (el) el.classList.toggle("hidden", key !== which);
    });
    Object.entries(tabs).forEach(([key, el]) => {
      if (el) el.classList.toggle("active", key === which);
    });
  }

  function closePipelineSettings() {
    if (pipelineSettingsModal) pipelineSettingsModal.classList.add("hidden");
  }

  async function cancelPipelineSettings() {
    if (pipelineSettingsSnapshot) {
      restorePipelineSettingsSnapshot(pipelineSettingsSnapshot);
      await persistAppSettings(pipelineSettingsSnapshot.appSettings);
    }
    if (typeof renderHistory === "function") renderHistory();
    closePipelineSettings();
  }

  if (btnPipelineSettings) {
    btnPipelineSettings.addEventListener("click", async () => {
      await fetchAppSettings();
      pipelineSettingsSnapshot = takePipelineSettingsSnapshot();
      showPipelineSection("general");
      if (pipelineSettingsModal) pipelineSettingsModal.classList.remove("hidden");
    });
  }
  if (btnClosePipelineSettings) btnClosePipelineSettings.addEventListener("click", cancelPipelineSettings);
  if (btnCancelPipelineSettings) btnCancelPipelineSettings.addEventListener("click", cancelPipelineSettings);
  if (btnSavePipelineSettings) btnSavePipelineSettings.addEventListener("click", savePipelineSettings);
  if (pipelineTabGeneral) pipelineTabGeneral.addEventListener("click", () => showPipelineSection("general"));
  if (pipelineTabApp) pipelineTabApp.addEventListener("click", () => showPipelineSection("app"));
  if (pipelineTabAudio) pipelineTabAudio.addEventListener("click", () => showPipelineSection("audio"));
  if (pipelineTabSubtitles) pipelineTabSubtitles.addEventListener("click", () => showPipelineSection("subtitles"));
  if (pipelineTabNaming) pipelineTabNaming.addEventListener("click", () => showPipelineSection("naming"));
  if (pipelineTabWatch) pipelineTabWatch.addEventListener("click", () => showPipelineSection("watch"));

  function wireSettingsToggle(el) {
    if (!el) return;
    el.addEventListener("click", () => {
      const next = el.getAttribute("aria-checked") !== "true";
      setPipelineToggle(el, next);
      syncPipelineFieldRow(el);
    });
  }
  wireSettingsToggle(document.getElementById("globalTmdbLookup"));
  wireSettingsToggle(document.getElementById("globalHdrStrict"));
  wireSettingsToggle(document.getElementById("globalPreserveDoviRpu"));
  wireSettingsToggle(document.getElementById("globalSubtitleSearch"));
  wireSettingsToggle(document.getElementById("globalSvtLowMemory"));
  wireSettingsToggle(document.getElementById("globalWatchFolderEnabled"));
  wireSettingsToggle(document.getElementById("globalAllowBuiltinPresetEdits"));

  const globalSvtLp = document.getElementById("globalSvtLp");
  if (globalSvtLp) {
    const savedLp = localStorage.getItem(SVT_LP_KEY);
    if (savedLp != null && savedLp !== "") globalSvtLp.value = savedLp;
    syncPipelineFieldRow(globalSvtLp);
    globalSvtLp.addEventListener("change", () => {
      localStorage.setItem(SVT_LP_KEY, globalSvtLp.value);
      syncPipelineFieldRow(globalSvtLp);
    });
  }
  const globalHourFormat = document.getElementById("globalHourFormat");
  if (globalHourFormat) {
    syncPipelineFieldRow(globalHourFormat);
    globalHourFormat.addEventListener("change", () => {
      syncPipelineFieldRow(globalHourFormat);
    });
  }
  const globalSvtLowMemory = document.getElementById("globalSvtLowMemory");
  if (globalSvtLowMemory) {
    const savedLm = localStorage.getItem(SVT_LOW_MEMORY_KEY);
    if (savedLm != null) {
      setPipelineToggle(globalSvtLowMemory, savedLm === "1");
      syncPipelineFieldRow(globalSvtLowMemory);
    }
    globalSvtLowMemory.addEventListener("click", () => {
      const on = globalSvtLowMemory.getAttribute("aria-checked") === "true";
      localStorage.setItem(SVT_LOW_MEMORY_KEY, on ? "1" : "0");
    });
  }

  fetchAppSettings();

  function buildConfigFromPreset(preset, extra = {}) {
    const p = preset || {};
    const svt = (p.svt_params && typeof p.svt_params === "object") ? { ...p.svt_params } : {};
    // Pipeline owns lp / low-memory (strip legacy preset copies)
    delete svt.lp;
    delete svt["low-memory"];
    const lpEl = document.getElementById("globalSvtLp");
    let lp = Number(lpEl?.value ?? appSettings.svt_lp ?? localStorage.getItem(SVT_LP_KEY) ?? 0);
    if (!Number.isFinite(lp) || lp < 0 || lp > 6) lp = 0;
    if (lp > 0) svt.lp = lp;
    const lowMemEl = document.getElementById("globalSvtLowMemory");
    const lowMem = lowMemEl
      ? lowMemEl.getAttribute("aria-checked") === "true"
      : !!appSettings.svt_low_memory;
    if (lowMem) svt["low-memory"] = 1;
    return {
      preset_id: p.id || selectedPreset,
      preset_name: p.name || selectedPreset,
      crf: Number.isFinite(Number(p.crf)) ? Number(p.crf) : 35,
      preset: Number.isFinite(Number(p.preset)) ? Number(p.preset) : 3,
      resolution_target: p.resolution_target || "source",
      ssimu2: getSsimu2Mode(),
      audio_bitrate_51: p.audio_bitrate_51 || "320k",
      audio_bitrate_stereo: p.audio_bitrate_stereo || "160k",
      audio_format: getAudioFormat(),
      audio_languages: getPipelineAudioLanguages(),
      audio_best_only: getAudioBestOnly(),
      autocrop: getAutocropEnabled(),
      ssimu2_post: getSsimu2PostEnabled(),
      extract_subtitles: getExtractSubtitlesEnabled(),
      subtitle_strip_credits: getStripSubCreditsEnabled(),
      subtitle_languages: getPipelineSubLanguages(),
      subtitle_kinds: getPipelineSubKinds(),
      autoname_output: true,
      name_template_movie: appSettings.name_template_movie || DEFAULT_NAME_TEMPLATE_MOVIE,
      name_template_episode: appSettings.name_template_episode || DEFAULT_NAME_TEMPLATE_EPISODE,
      subtitle_search: !!appSettings.subtitle_search,
      container: getContainerFormat(),
      svt_params: svt,
      ...extra
    };
  }

  // Fields owned by the preset (ignore job-only: test_mode, trim, audio_tracks_order)
  const PRESET_SYNC_KEYS = [
    "preset_id", "preset_name", "crf", "preset", "resolution_target",
    "audio_bitrate_51", "audio_bitrate_stereo", "svt_params"
  ];

  function stableJson(val) {
    if (val === null || typeof val !== "object") return JSON.stringify(val);
    if (Array.isArray(val)) return `[${val.map(stableJson).join(",")}]`;
    const keys = Object.keys(val).sort();
    return `{${keys.map(k => `${JSON.stringify(k)}:${stableJson(val[k])}`).join(",")}}`;
  }

  function getPresetForJob(job) {
    const pid = job?.config?.preset_id;
    if (!pid) return null;
    return (allPresets || []).find(p => p.id === pid) || null;
  }

  function normalizePresetCompareVal(key, val) {
    if (key === "crf") return Number.isFinite(Number(val)) ? Number(val) : null;
    if (key === "preset") return Number.isFinite(Number(val)) ? Number(val) : null;
    if (key === "svt_params") {
      const src = (val && typeof val === "object" && !Array.isArray(val)) ? val : {};
      const out = {};
      Object.keys(src).sort().forEach(k => {
        const v = src[k];
        if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) out[k] = Number(v);
        else out[k] = v;
      });
      return out;
    }
    return val === undefined ? null : val;
  }

  function isJobPresetOutdated(job) {
    if (!job || job.status !== "QUEUED") return false;
    const cur = job.config || {};
    // Server sets this when the preset is saved while the job still has a snapshot
    if (cur.preset_stale) return true;
    const preset = getPresetForJob(job);
    if (!preset) return false;
    const fresh = buildConfigFromPreset(preset);
    return PRESET_SYNC_KEYS.some(key =>
      stableJson(normalizePresetCompareVal(key, cur[key])) !==
      stableJson(normalizePresetCompareVal(key, fresh[key]))
    );
  }

  function configFromPresetKeepingJobExtras(job, preset) {
    const fresh = buildConfigFromPreset(preset, getTestModeJobFields());
    const cur = job.config || {};
    // Keep manual audio selection if present
    if (Array.isArray(cur.audio_tracks_order)) {
      fresh.audio_tracks_order = [...cur.audio_tracks_order];
    }
    return fresh;
  }

  function audioLangLabel(code) {
    const fam = langFamily(code);
    const hit = AUDIO_LANG_OPTIONS.find(o => o.code === fam);
    return hit ? hit.label : String(code || "und").toUpperCase();
  }

  function renderPipelineLangList(opts) {
    const {
      listEl,
      addSelectEl,
      languages,
      emptyHtml,
      onChange,
      reorderable = true
    } = opts;
    if (!listEl) return;

    if (!languages.length) {
      listEl.innerHTML = emptyHtml;
    } else {
      listEl.innerHTML = languages.map((code, idx) => {
        const actions = reorderable
          ? `<div class="audio-lang-actions">
            <button type="button" class="btn-icon audio-lang-up" data-idx="${idx}" title="Move up" ${idx === 0 ? "disabled" : ""}>↑</button>
            <button type="button" class="btn-icon audio-lang-down" data-idx="${idx}" title="Move down" ${idx === languages.length - 1 ? "disabled" : ""}>↓</button>
            <button type="button" class="btn-icon audio-lang-remove" data-idx="${idx}" title="Remove">✕</button>
          </div>`
          : `<div class="audio-lang-actions">
            <button type="button" class="btn-icon audio-lang-remove" data-idx="${idx}" title="Remove">✕</button>
          </div>`;
        return `
        <div class="audio-lang-row${reorderable ? "" : " audio-lang-row--flat"}" data-code="${escapeHtml(code)}">
          ${reorderable ? `<span class="audio-lang-priority">${idx + 1}</span>` : ""}
          <span class="audio-lang-name">${escapeHtml(audioLangLabel(code))}</span>
          <span class="audio-lang-code">${escapeHtml(code)}</span>
          ${actions}
        </div>`;
      }).join("");
    }

    if (addSelectEl) {
      const selected = new Set(languages);
      addSelectEl.innerHTML = `<option value="">Add language…</option>` +
        AUDIO_LANG_OPTIONS
          .filter(o => !selected.has(o.code))
          .map(o => `<option value="${o.code}">${escapeHtml(o.label)} (${o.code})</option>`)
          .join("");
    }

    if (reorderable) {
      listEl.querySelectorAll(".audio-lang-up").forEach(btn => {
        btn.addEventListener("click", () => {
          const i = Number(btn.getAttribute("data-idx"));
          if (i <= 0) return;
          const tmp = languages[i - 1];
          languages[i - 1] = languages[i];
          languages[i] = tmp;
          onChange();
        });
      });
      listEl.querySelectorAll(".audio-lang-down").forEach(btn => {
        btn.addEventListener("click", () => {
          const i = Number(btn.getAttribute("data-idx"));
          if (i >= languages.length - 1) return;
          const tmp = languages[i + 1];
          languages[i + 1] = languages[i];
          languages[i] = tmp;
          onChange();
        });
      });
    }
    listEl.querySelectorAll(".audio-lang-remove").forEach(btn => {
      btn.addEventListener("click", () => {
        const i = Number(btn.getAttribute("data-idx"));
        languages.splice(i, 1);
        onChange();
      });
    });
  }

  function renderPipelineAudioLangEditor() {
    renderPipelineLangList({
      listEl: document.getElementById("pipelineAudioLangList"),
      addSelectEl: document.getElementById("pipelineAudioLangAdd"),
      languages: pipelineAudioLanguages,
      reorderable: true,
      emptyHtml: `<div class="audio-lang-empty">No languages — fallback picks one best track overall.</div>`,
      onChange: () => {
        saveLangList(AUDIO_LANGS_KEY, pipelineAudioLanguages);
        renderPipelineAudioLangEditor();
        if (typeof renderQueue === "function") renderQueue();
      }
    });
  }

  function renderPipelineSubLangEditor() {
    renderPipelineLangList({
      listEl: document.getElementById("pipelineSubLangList"),
      addSelectEl: document.getElementById("pipelineSubLangAdd"),
      languages: pipelineSubLanguages,
      reorderable: false,
      emptyHtml: `<div class="audio-lang-empty">No languages — extract all text subtitle languages.</div>`,
      onChange: () => {
        saveLangList(SUB_LANGS_KEY, pipelineSubLanguages);
        renderPipelineSubLangEditor();
      }
    });
  }

  function syncSubKindToggles() {
    const selected = new Set(pipelineSubKinds);
    Object.entries(SUB_KIND_TOGGLE_IDS).forEach(([kind, id]) => {
      const el = document.getElementById(id);
      if (!el) return;
      setPipelineToggle(el, selected.has(kind));
      syncPipelineFieldRow(el);
    });
  }

  function persistSubKindsFromUi() {
    pipelineSubKinds = Object.entries(SUB_KIND_TOGGLE_IDS)
      .filter(([, id]) => {
        const el = document.getElementById(id);
        return el && el.getAttribute("aria-checked") === "true";
      })
      .map(([kind]) => kind);
    localStorage.setItem(SUB_KINDS_KEY, JSON.stringify(pipelineSubKinds));
  }

  const btnAddPipelineAudioLang = document.getElementById("btnAddPipelineAudioLang");
  if (btnAddPipelineAudioLang) {
    btnAddPipelineAudioLang.addEventListener("click", () => {
      const sel = document.getElementById("pipelineAudioLangAdd");
      const code = sel?.value;
      if (!code || pipelineAudioLanguages.includes(code)) return;
      pipelineAudioLanguages.push(code);
      saveLangList(AUDIO_LANGS_KEY, pipelineAudioLanguages);
      renderPipelineAudioLangEditor();
      if (typeof renderQueue === "function") renderQueue();
    });
  }

  const btnAddPipelineSubLang = document.getElementById("btnAddPipelineSubLang");
  if (btnAddPipelineSubLang) {
    btnAddPipelineSubLang.addEventListener("click", () => {
      const sel = document.getElementById("pipelineSubLangAdd");
      const code = sel?.value;
      if (!code || pipelineSubLanguages.includes(code)) return;
      pipelineSubLanguages.push(code);
      saveLangList(SUB_LANGS_KEY, pipelineSubLanguages);
      renderPipelineSubLangEditor();
    });
  }

  Object.values(SUB_KIND_TOGGLE_IDS).forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("click", () => {
      const next = el.getAttribute("aria-checked") !== "true";
      setPipelineToggle(el, next);
      syncPipelineFieldRow(el);
      persistSubKindsFromUi();
    });
  });

  syncSubKindToggles();
  renderPipelineAudioLangEditor();
  renderPipelineSubLangEditor();

  // Throws on a non-2xx so callers can't assign an error body's missing fields
  // (e.g. allPresets = undefined) and wedge the UI.
  async function fetchJson(url, options) {
    const res = await fetch(url, options);
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = null;
    }
    if (!res.ok) {
      throw new Error((data && data.detail) || `${res.status} ${res.statusText}`);
    }
    return data;
  }

  async function fetchPresets() {
    try {
      const data = await fetchJson("/api/presets");
      if (Array.isArray(data)) allPresets = data;
      renderPresets();
      renderQueue();
    } catch (e) {
      console.error("Error fetching presets:", e);
    }
  }

  function syncInspPresetSelect() {
    const inspPresetSelect = document.getElementById("inspPresetSelect");
    if (!inspPresetSelect) return;

    if (!allPresets || allPresets.length === 0) {
      inspPresetSelect.innerHTML = `<option value="">No presets available</option>`;
      inspPresetSelect.disabled = true;
      return;
    }

    inspPresetSelect.disabled = false;
    inspPresetSelect.innerHTML = allPresets.map(p =>
      `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`
    ).join("");
    inspPresetSelect.value = selectedPreset;
  }

  function allowBuiltinPresetEdits() {
    return !!appSettings.allow_builtin_preset_edits;
  }

  function renderPresetCardHtml(p, resLabel) {
    const isBuiltin = !!p.builtin;
    const canEdit = !isBuiltin || allowBuiltinPresetEdits();
    const actions = canEdit
      ? `<div class="preset-actions">
          <button class="btn-icon edit-preset-btn" data-id="${escapeHtml(p.id)}" title="Edit Preset">✎</button>
          <button class="btn-icon delete-btn delete-preset-btn" data-id="${escapeHtml(p.id)}" title="Delete Preset">✕</button>
        </div>`
      : `<div class="preset-actions"></div>`;
    return `
      <div class="preset-card-wrapper ${p.id === selectedPreset ? "is-default" : ""}" data-id="${escapeHtml(p.id)}" data-builtin="${isBuiltin ? "1" : "0"}">
        <div class="preset-btn">
          <div class="preset-name">
            ${escapeHtml(p.name)}
            ${p.id === selectedPreset ? '<span class="preset-default-badge">Default</span>' : ""}
          </div>
          <div class="preset-meta">${escapeHtml(resLabel[p.resolution_target] || "Source")} · CRF ${Number.isFinite(Number(p.crf)) ? Number(p.crf) : 35} · P${Number.isFinite(Number(p.preset)) ? Number(p.preset) : 3}</div>
        </div>
        ${actions}
      </div>
    `;
  }

  function renderPresets() {
    if (!allPresets || allPresets.length === 0) {
      presetListContainer.innerHTML = '<div class="text-dim">No presets configured.</div>';
      selectedPreset = "";
      localStorage.removeItem(DEFAULT_PRESET_KEY);
      syncInspPresetSelect();
      return;
    }

    if (!selectedPreset || !allPresets.some(p => p.id === selectedPreset)) {
      selectedPreset = allPresets[0].id;
      localStorage.setItem(DEFAULT_PRESET_KEY, selectedPreset);
    }

    const resLabel = {
      source: "Source",
      "1080p": "1080p",
      "1440p": "1440p",
      "2160p": "2160p"
    };

    const builtins = allPresets.filter(p => !!p.builtin);
    const locals = allPresets.filter(p => !p.builtin);
    const parts = [];
    builtins.forEach(p => parts.push(renderPresetCardHtml(p, resLabel)));
    if (builtins.length && locals.length) {
      parts.push('<div class="preset-list-separator" role="separator" aria-hidden="true"></div>');
    }
    locals.forEach(p => parts.push(renderPresetCardHtml(p, resLabel)));
    presetListContainer.innerHTML = parts.join("");

    syncInspPresetSelect();

    document.querySelectorAll(".edit-preset-btn").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const pid = btn.getAttribute("data-id");
        const target = allPresets.find(p => p.id === pid);
        if (target) openPresetModal(target);
      });
    });

    document.querySelectorAll(".delete-preset-btn").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const pid = btn.getAttribute("data-id");
        const name = allPresets.find(p => p.id === pid)?.name || pid;
        if (await customConfirm("Are you sure you want to delete:", {
          danger: true,
          subject: name,
          okLabel: "Delete"
        })) {
          await deletePreset(pid);
        }
      });
    });
  }

  btnNewPreset.addEventListener("click", () => {
    openPresetModal(null);
  });

  const presetTabBasic = document.getElementById("presetTabBasic");
  const presetTabAudio = document.getElementById("presetTabAudio");
  const presetTabAdvanced = document.getElementById("presetTabAdvanced");
  const presetTabPreview = document.getElementById("presetTabPreview");
  const presetBasicSection = document.getElementById("presetBasicSection");
  const presetAudioSection = document.getElementById("presetAudioSection");
  const presetAdvancedSection = document.getElementById("presetAdvancedSection");
  const presetPreviewSection = document.getElementById("presetPreviewSection");
  const presetAdvancedFields = document.getElementById("presetAdvancedFields");

  function showPresetSection(which) {
    const sections = {
      basic: presetBasicSection,
      audio: presetAudioSection,
      advanced: presetAdvancedSection,
      preview: presetPreviewSection
    };
    const tabs = {
      basic: presetTabBasic,
      audio: presetTabAudio,
      advanced: presetTabAdvanced,
      preview: presetTabPreview
    };
    Object.entries(sections).forEach(([key, el]) => {
      if (el) el.classList.toggle("hidden", key !== which);
    });
    Object.entries(tabs).forEach(([key, el]) => {
      if (el) el.classList.toggle("active", key === which);
    });
    if (which === "preview") updatePresetCommandPreview();
  }

  if (presetTabBasic) presetTabBasic.addEventListener("click", () => showPresetSection("basic"));
  if (presetTabAudio) presetTabAudio.addEventListener("click", () => showPresetSection("audio"));
  if (presetTabAdvanced) presetTabAdvanced.addEventListener("click", () => showPresetSection("advanced"));
  if (presetTabPreview) presetTabPreview.addEventListener("click", () => showPresetSection("preview"));

  function normalizeSvtValue(def, raw) {
    const n = Number(raw);
    return Number.isFinite(n) ? n : def.default;
  }

  function isSvtDefault(def, value) {
    const v = normalizeSvtValue(def, value);
    // Float compare for ac-bias etc.
    return Math.abs(Number(v) - Number(def.default)) < 1e-9;
  }

  function getSvtInputValue(def) {
    const el = document.getElementById(`svt_${def.key}`);
    if (!el) return def.default;
    if (def.control === "toggle") {
      return el.getAttribute("aria-checked") === "true" ? 1 : 0;
    }
    return el.value;
  }

  function setSvtInputValue(def, value) {
    const el = document.getElementById(`svt_${def.key}`);
    if (!el) return;
    const v = normalizeSvtValue(def, value);
    if (def.control === "toggle") {
      const on = Number(v) === 1;
      el.setAttribute("aria-checked", on ? "true" : "false");
      el.classList.toggle("is-on", on);
      el.querySelector(".svt-toggle-label")?.replaceChildren(document.createTextNode(on ? "On" : "Off"));
      return;
    }
    el.value = String(v);
    if (def.control === "slider") {
      const valEl = document.getElementById(`svt_${def.key}_val`);
      if (valEl) valEl.textContent = String(v);
    }
    if (def.control === "number") {
      syncSvtNumberSpinners(el, def);
    }
  }

  function syncSvtNumberSpinners(input, def) {
    const wrap = input?.closest(".svt-number-wrap");
    if (!wrap || !def) return;
    const cur = normalizeSvtValue(def, input.value);
    const min = Number(def.min);
    const max = Number(def.max);
    const up = wrap.querySelector('.svt-number-btn[data-dir="1"]');
    const down = wrap.querySelector('.svt-number-btn[data-dir="-1"]');
    if (up) up.classList.toggle("is-hidden", cur >= max);
    if (down) down.classList.toggle("is-hidden", cur <= min);
  }

  function buildSvtControlHtml(def, cur) {
    const id = `svt_${def.key}`;
    const key = escapeHtml(def.key);
    if (def.control === "toggle") {
      const on = Number(cur) === 1;
      return `
        <button type="button" class="svt-toggle ${on ? "is-on" : ""} svt-param-input"
          id="${escapeHtml(id)}" data-key="${key}" role="switch"
          aria-checked="${on ? "true" : "false"}" aria-label="${key}">
          <span class="svt-toggle-track" aria-hidden="true"><span class="svt-toggle-thumb"></span></span>
          <span class="svt-toggle-label">${on ? "On" : "Off"}</span>
        </button>`;
    }
    if (def.control === "select") {
      const opts = [];
      for (let v = def.min; v <= def.max + 1e-9; v = Math.round((v + def.step) * 1000) / 1000) {
        const selected = Number(cur) === Number(v) ? "selected" : "";
        opts.push(`<option value="${v}" ${selected}>${v}</option>`);
      }
      return `<select class="form-input svt-param-input" id="${escapeHtml(id)}" data-key="${key}">${opts.join("")}</select>`;
    }
    if (def.control === "slider") {
      return `
        <div class="svt-slider-wrap">
          <input type="range" class="svt-param-input svt-slider" id="${escapeHtml(id)}" data-key="${key}"
            min="${def.min}" max="${def.max}" step="${def.step}" value="${escapeHtml(String(cur))}">
          <span class="svt-slider-val" id="${escapeHtml(id)}_val">${escapeHtml(String(cur))}</span>
        </div>`;
    }
    return `
      <div class="svt-number-wrap">
        <input type="number" class="form-input svt-param-input" id="${escapeHtml(id)}"
          data-key="${key}" value="${escapeHtml(String(cur))}"
          min="${def.min}" max="${def.max}" step="${def.step}">
        <div class="svt-number-spinners" aria-hidden="true">
          <button type="button" class="svt-number-btn" data-dir="1" tabindex="-1" aria-label="Increase">▴</button>
          <button type="button" class="svt-number-btn" data-dir="-1" tabindex="-1" aria-label="Decrease">▾</button>
        </div>
      </div>`;
  }

  function syncSvtRowDefault(def) {
    const row = document.querySelector(`.setting-row[data-svt-key="${def.key}"]`);
    if (!row) return;
    row.classList.toggle("is-default", isSvtDefault(def, getSvtInputValue(def)));
    updatePresetCommandPreview();
  }

  const RES_HEIGHT_MAP = { source: 0, "1080p": 1080, "1440p": 1440, "2160p": 2160 };
  const presetCommandPreviewBody = document.getElementById("presetCommandPreviewBody");

  function updatePresetCommandPreview() {
    if (!presetCommandPreviewBody) return;
    const lines = [];

    const crf = Number(pEditCrf?.value ?? 35);
    if (Number.isFinite(crf) && crf !== 35) lines.push(`--crf ${crf}`);

    const presetVal = Number(pEditPreset?.value ?? 3);
    if (Number.isFinite(presetVal) && presetVal !== 3) lines.push(`--preset ${presetVal}`);

    const resolution = pEditResolution?.value || "source";
    if (resolution !== "source") {
      const h = RES_HEIGHT_MAP[resolution] ?? 0;
      lines.push(`--target-height ${h}`);
    }

    const audio51 = pEditAudio51?.value || "320k";
    if (audio51 !== "320k") lines.push(`audio 5.1 bitrate: ${audio51}`);

    const audioStereo = pEditAudioStereo?.value || "160k";
    if (audioStereo !== "160k") lines.push(`audio stereo bitrate: ${audioStereo}`);

    const svt = collectSvtOverrides();
    const svtFlags = Object.keys(svt)
      .sort()
      .map(k => `--${k} ${svt[k]}`);
    if (svtFlags.length) {
      lines.push(`--svt-params:`);
      svtFlags.forEach(f => lines.push(`  ${f}`));
    }

    if (!lines.length) {
      presetCommandPreviewBody.textContent = "All settings at defaults.";
      presetCommandPreviewBody.classList.add("is-empty");
    } else {
      presetCommandPreviewBody.textContent = lines.join("\n");
      presetCommandPreviewBody.classList.remove("is-empty");
    }
  }

  function renderAdvancedSvtFields(overrides) {
    if (!presetAdvancedFields) return;
    const ov = overrides && typeof overrides === "object" ? overrides : {};

    const byCategory = new Map();
    ESSENTIAL_SVT_SETTINGS.forEach(def => {
      const cat = def.category || "Other";
      if (!byCategory.has(cat)) byCategory.set(cat, []);
      byCategory.get(cat).push(def);
    });
    const orderedCategories = [
      ...SVT_CATEGORY_ORDER.filter(c => byCategory.has(c)),
      ...[...byCategory.keys()].filter(c => !SVT_CATEGORY_ORDER.includes(c)),
    ];

    presetAdvancedFields.innerHTML = orderedCategories.map(cat => {
      const rowsHtml = byCategory.get(cat).map(def => {
        const cur = Object.prototype.hasOwnProperty.call(ov, def.key) ? ov[def.key] : def.default;
        const atDefault = isSvtDefault(def, cur);
        const defaultHint = def.control === "toggle"
          ? (Number(def.default) === 1 ? "On" : "Off")
          : String(def.default);
        return `
        <div class="setting-row ${atDefault ? "is-default" : ""}" data-svt-key="${escapeHtml(def.key)}">
          <label class="setting-label" for="svt_${escapeHtml(def.key)}">${labelWithHelp(def.key, `${def.help}\nDefault: ${defaultHint}`)}</label>
          <button type="button" class="btn-reset-default" data-key="${escapeHtml(def.key)}"
            title="Reset to default (${defaultHint})" aria-label="Reset to default">↺</button>
          <div class="setting-control">
            ${buildSvtControlHtml(def, cur)}
          </div>
        </div>`;
      }).join("");
      return `
        <div class="svt-category">
          <div class="svt-category-title">${escapeHtml(cat)}</div>
          ${rowsHtml}
        </div>`;
    }).join("");

    presetAdvancedFields.querySelectorAll(".svt-param-input").forEach(input => {
      const key = input.getAttribute("data-key");
      const def = ESSENTIAL_SVT_SETTINGS.find(d => d.key === key);
      if (!def) return;

      if (def.control === "toggle") {
        input.addEventListener("click", () => {
          const next = input.getAttribute("aria-checked") === "true" ? 0 : 1;
          setSvtInputValue(def, next);
          syncSvtRowDefault(def);
        });
        return;
      }

      const onChange = () => {
        if (def.control === "slider") {
          const valEl = document.getElementById(`svt_${def.key}_val`);
          if (valEl) valEl.textContent = input.value;
        }
        if (def.control === "number") {
          syncSvtNumberSpinners(input, def);
        }
        syncSvtRowDefault(def);
      };
      input.addEventListener("input", onChange);
      input.addEventListener("change", onChange);
      if (def.control === "number") {
        syncSvtNumberSpinners(input, def);
      }
    });

    presetAdvancedFields.querySelectorAll(".svt-number-btn").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        if (btn.classList.contains("is-hidden")) return;
        const wrap = btn.closest(".svt-number-wrap");
        const input = wrap && wrap.querySelector(".svt-param-input");
        if (!input) return;
        const key = input.getAttribute("data-key");
        const def = ESSENTIAL_SVT_SETTINGS.find(d => d.key === key);
        if (!def) return;
        const step = Number(def.step) || 1;
        const min = Number(def.min);
        const max = Number(def.max);
        const dir = Number(btn.getAttribute("data-dir")) || 0;
        const cur = normalizeSvtValue(def, input.value);
        const next = Math.min(max, Math.max(min, Math.round((cur + dir * step) * 1000) / 1000));
        setSvtInputValue(def, next);
        syncSvtRowDefault(def);
      });
    });

    presetAdvancedFields.querySelectorAll(".btn-reset-default").forEach(btn => {
      btn.addEventListener("click", () => {
        const key = btn.getAttribute("data-key");
        const def = ESSENTIAL_SVT_SETTINGS.find(d => d.key === key);
        if (!def) return;
        setSvtInputValue(def, def.default);
        syncSvtRowDefault(def);
      });
    });
    wireSettingHelpTips(presetAdvancedFields);
  }

  function collectSvtOverrides() {
    const out = {};
    ESSENTIAL_SVT_SETTINGS.forEach(def => {
      const val = normalizeSvtValue(def, getSvtInputValue(def));
      if (!isSvtDefault(def, val)) out[def.key] = val;
    });

    // Basic-tab tune (Tritium default: tune=1/PSNR)
    const tune = Number(pEditTune?.value ?? 1);
    if (Number.isFinite(tune) && tune !== 1) out.tune = tune;
    else delete out.tune;

    // Owned by Settings
    delete out.lp;
    delete out["low-memory"];

    return out;
  }

  function getBasicToggle(el) {
    return !!(el && el.getAttribute("aria-checked") === "true");
  }

  function syncBasicFieldRow(el) {
    syncSettingFieldRow(el, ".basic-reset", "basic-toggle");
    if (el && el.type === "range") {
      const valEl = document.getElementById(`${el.id}_val`);
      if (valEl) valEl.textContent = el.value;
    }
    updatePresetCommandPreview();
  }

  function syncAllBasicFieldRows() {
    document.querySelectorAll(".basic-field, .basic-toggle").forEach(syncBasicFieldRow);
  }

  function wireBasicPresetControls() {
    document.querySelectorAll(".basic-toggle").forEach(btn => {
      btn.addEventListener("click", () => {
        setToggle(btn, !getBasicToggle(btn));
        syncBasicFieldRow(btn);
      });
    });
    document.querySelectorAll(".basic-field").forEach(el => {
      el.addEventListener("change", () => syncBasicFieldRow(el));
      el.addEventListener("input", () => syncBasicFieldRow(el));
    });
    document.querySelectorAll(".basic-reset").forEach(btn => {
      btn.addEventListener("click", () => {
        const target = document.getElementById(btn.getAttribute("data-target"));
        if (!target) return;
        if (target.classList.contains("basic-toggle")) {
          setToggle(target, target.getAttribute("data-default") === "true");
        } else {
          target.value = target.getAttribute("data-default") || "";
        }
        syncBasicFieldRow(target);
      });
    });
  }
  wireBasicPresetControls();
  wireSettingHelpTips(document);

  function applyBasicSvtFields(svtParams) {
    const sp = svtParams && typeof svtParams === "object" ? svtParams : {};
    if (pEditTune) {
      const t = sp.tune !== undefined ? Number(sp.tune) : 1;
      pEditTune.value = String([0, 1, 2, 3, 4, 5].includes(t) ? t : 1);
    }
  }

  function openPresetModal(preset) {
    if (preset && preset.builtin && !allowBuiltinPresetEdits()) {
      void customAlert("Built-in presets are read-only. Enable built-in preset edits in Settings → App to change them.");
      return;
    }
    presetModal.classList.remove("hidden");
    showPresetSection("basic");
    if (preset) {
      presetModalTitle.textContent = "Edit Preset";
      pEditId.value = preset.id;
      pEditName.value = preset.name;
      pEditCrf.value = Number.isFinite(Number(preset.crf)) ? Math.round(Number(preset.crf)) : 35;
      pEditPreset.value = Number.isFinite(Number(preset.preset)) ? Number(preset.preset) : 3;
      pEditResolution.value = preset.resolution_target || "source";
      pEditAudio51.value = preset.audio_bitrate_51 || "320k";
      pEditAudioStereo.value = preset.audio_bitrate_stereo || "160k";
      setToggle(pEditIsDefault, preset.id === selectedPreset);
      applyBasicSvtFields(preset.svt_params || {});
      renderAdvancedSvtFields(preset.svt_params || {});
    } else {
      presetModalTitle.textContent = "New Preset";
      const newId = "preset_" + Date.now();
      pEditId.value = newId;
      pEditName.value = "";
      pEditCrf.value = 35;
      pEditPreset.value = 3;
      pEditResolution.value = "source";
      pEditAudio51.value = "320k";
      pEditAudioStereo.value = "160k";
      setToggle(pEditIsDefault, allPresets.length === 0);
      applyBasicSvtFields({});
      renderAdvancedSvtFields({});
    }
    syncAllBasicFieldRows();
    updatePresetCommandPreview();
  }

  btnClosePresetModal.addEventListener("click", () => presetModal.classList.add("hidden"));
  btnCancelPresetModal.addEventListener("click", () => presetModal.classList.add("hidden"));

  btnSavePreset.addEventListener("click", async () => {
    const name = pEditName.value.trim();
    if (!name) {
      await customAlert("Please enter a preset name.");
      return;
    }

    const existing = (allPresets || []).find(p => p.id === (pEditId.value || ""));
    const payload = {
      id: pEditId.value || ("preset_" + Date.now()),
      name: name,
      description: (existing && existing.description) ? existing.description : "",
      crf: Math.round(Math.min(70, Math.max(1, Number(pEditCrf.value) || 35))),
      preset: Math.round(Math.min(13, Math.max(-3, Number(pEditPreset.value) || 4))),
      resolution_target: pEditResolution.value || "source",
      audio_bitrate_51: pEditAudio51.value,
      audio_bitrate_stereo: pEditAudioStereo.value,
      svt_params: collectSvtOverrides()
    };

    try {
      const data = await fetchJson("/api/presets/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (Array.isArray(data && data.presets)) allPresets = data.presets;

      if (getBasicToggle(pEditIsDefault)) {
        selectedPreset = payload.id;
        localStorage.setItem(DEFAULT_PRESET_KEY, selectedPreset);
      } else if (selectedPreset === payload.id) {
        // Unchecking default on the current default → fall back to first other preset
        const fallback = allPresets.find(p => p.id !== payload.id) || allPresets[0];
        selectedPreset = fallback ? fallback.id : "";
        if (selectedPreset) localStorage.setItem(DEFAULT_PRESET_KEY, selectedPreset);
        else localStorage.removeItem(DEFAULT_PRESET_KEY);
      }

      renderPresets();
      renderQueue();
      presetModal.classList.add("hidden");
    } catch (e) {
      await customAlert("Error saving preset: " + e);
    }
  });

  async function deletePreset(id) {
    try {
      const data = await fetchJson("/api/presets/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "delete", job_id: id })
      });
      if (Array.isArray(data && data.presets)) allPresets = data.presets;
      if (selectedPreset === id) {
        selectedPreset = allPresets.length > 0 ? allPresets[0].id : "";
        if (selectedPreset) localStorage.setItem(DEFAULT_PRESET_KEY, selectedPreset);
        else localStorage.removeItem(DEFAULT_PRESET_KEY);
      }
      renderPresets();
      renderQueue();
    } catch (e) {
      await customAlert("Error deleting preset: " + e);
    }
  }

  fetchPresets();

  // Terminal Drawer Toggle
  btnToggleTerminal.addEventListener("click", () => {
    terminalBox.classList.toggle("collapsed");
  });

  // --- WebSocket Connection ---
  function connectWebSocket() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${protocol}//${window.location.host}/ws/live`);

    ws.onopen = () => {
      console.log("[WS] Connected to live queue feed");
    };

    ws.onmessage = (event) => {
      try {
        const msg = jsonParseSafe(event.data);
        if (!msg) return;
        handleWsMessage(msg.event, msg.data);
      } catch (err) {
        console.error("[WS] Message error:", err);
      }
    };

    ws.onclose = () => {
      console.log("[WS] Disconnected, retrying in 2s...");
      setTimeout(connectWebSocket, 2000);
    };
  }

  function handleWsMessage(event, data) {
    if (event === "initial_state") {
      queueState.isRunning = data.is_running;
      queueState.isPaused = data.is_paused;
      queueState.currentJobId = data.current_job_id;
      queueState.jobs = data.jobs || [];
      renderQueue();
      // Do not auto-stamp Test Mode on reconnect — only sync when the user toggles it.
    } else if (event === "job_added") {
      queueState.jobs.push(data);
      renderQueue();
    } else if (event === "job_removed") {
      const before = queueState.jobs.length;
      queueState.jobs = queueState.jobs.filter(j => j.id !== data.id);
      // Skip re-render when history_updated already dropped this job
      if (queueState.jobs.length !== before) renderQueue();
    } else if (event === "history_updated") {
      // Completed encode moved to Finished — drop from pending immediately
      if (data && data.id) {
        queueState.jobs = queueState.jobs.filter(j => j.id !== data.id);
      }
      renderQueue();
      fetchHistory();
    } else if (event === "subtitle_extract") {
      const name = data?.filename || data?.id || "file";
      const n = data?.extracted ?? 0;
      const st = data?.status || "";
      if (st === "ok") console.info(`[subs] ${name}: extracted ${n} file(s)`);
      else if (st === "skip") console.info(`[subs] ${name}: ${data?.message || "skipped"}`);
      else console.warn(`[subs] ${name}: ${data?.message || "error"}`);
    } else if (event === "subtitle_search") {
      const name = data?.filename || data?.id || "file";
      const n = data?.downloaded ?? 0;
      const st = data?.status || "";
      if (st === "ok") console.info(`[subs-search] ${name}: downloaded ${n} · ${data?.message || ""}`);
      else if (st === "skip") console.info(`[subs-search] ${name}: ${data?.message || "skipped"}`);
      else console.warn(`[subs-search] ${name}: ${data?.message || "error"}`);
    } else if (event === "job_update") {
      const idx = queueState.jobs.findIndex(j => j.id === data.id);
      const activeStatuses = new Set(ACTIVE_JOB_STATUSES);
      if (activeStatuses.has(data.status)) {
        queueState.currentJobId = data.id;
        queueState.isRunning = true;
      } else if (
        queueState.currentJobId === data.id &&
        (data.status === "COMPLETED" || data.status === "FAILED" || data.status === "CANCELLED")
      ) {
        queueState.currentJobId = null;
      }
      if (data.status === "COMPLETED" || data.status === "FAILED" || data.status === "CANCELLED" || data.status === "SKIPPED") {
        // Safety: archived outcomes live under Finished, not the pending list
        if (idx !== -1) queueState.jobs.splice(idx, 1);
        renderQueue();
        fetchHistory();
      } else if (idx !== -1) {
        queueState.jobs[idx] = data;
        renderQueue();
      } else if (data.status && data.status !== "COMPLETED") {
        queueState.jobs.push(data);
        renderQueue();
      }
    } else if (event === "job_progress") {
      if (data.id) queueState.currentJobId = data.id;
      updateActiveJobProgress(data);
    } else if (event === "queue_started") {
      queueState.isRunning = true;
      queueState.isPaused = false;
      renderQueueControls();
    } else if (event === "queue_paused") {
      queueState.isPaused = true;
      renderQueueControls();
    } else if (event === "queue_resumed") {
      queueState.isPaused = false;
      renderQueueControls();
    } else if (event === "queue_reordered") {
      if (Array.isArray(data?.job_ids) && data.job_ids.length) {
        applyLocalQueueOrder(data.job_ids);
        renderQueue();
      }
    } else if (event === "queue_stopped" || event === "queue_completed") {
      queueState.isRunning = false;
      queueState.currentJobId = null;
      renderQueueControls();
      renderQueue();
    }
  }

  function renderQueueControls() {
    // Pending list only — completed jobs live in history, not the queue.
    const pending = (queueState.jobs || []).filter((j) => j.status !== "COMPLETED");
    const hasJobs = pending.length > 0;
    if (btnStartQueue) btnStartQueue.disabled = !hasJobs || !!queueState.isRunning;
    if (btnPauseQueue) btnPauseQueue.disabled = !hasJobs || !queueState.isRunning;

    if (queueState.isRunning) {
      btnStartQueue.classList.add("hidden");
      btnPauseQueue.classList.remove("hidden");
      btnStopQueue.classList.remove("hidden");
      btnPauseQueue.textContent = queueState.isPaused ? "▶ Resume" : "⏸ Pause";
    } else {
      btnStartQueue.classList.remove("hidden");
      btnPauseQueue.classList.add("hidden");
      btnStopQueue.classList.add("hidden");
    }
  }

  function renderQueue() {
    renderQueueControls();
    queueCountBadge.textContent = `${queueState.jobs.length} Jobs`;
    if (tabQueueBadge) tabQueueBadge.textContent = String(queueState.jobs.length);

    // Active Job (panel always visible; idle when nothing is encoding)
    const activeStatuses = new Set(ACTIVE_JOB_STATUSES);
    let activeJob = null;
    if (queueState.currentJobId) {
      activeJob = queueState.jobs.find(j => j.id === queueState.currentJobId && activeStatuses.has(j.status));
    }
    if (!activeJob && queueState.isRunning) {
      activeJob = queueState.jobs.find(j => activeStatuses.has(j.status));
      if (activeJob) queueState.currentJobId = activeJob.id;
    }
    if (activeJob) {
      activeJobSection.classList.remove("is-idle");
      const tag = activeJob.media_tag || {};
      const outName = (activeJob.output_path || "").split(/[\\/]/).pop() || tag.library_name || "";
      const displayName = outName || activeJob.filename || "Encoding…";
      activeFileName.textContent = displayName;
      activeFileName.title = displayName;

      updateStepper(stageNumToStep(activeJob.stage_num || 1), activeJob.stage_percent || 0);
      activeStageText.textContent = activeJob.stage || "Encoding...";
      activePct.textContent = `${Math.round(activeJob.progress || 0)}%`;
      activeProgressFill.style.width = `${activeJob.progress || 0}%`;
      activeFps.textContent = (activeJob.fps || 0).toFixed(1);
      activeElapsed.textContent = formatSeconds(activeJob.elapsed_seconds || 0);
      updateRemainingEta(activeJob);

      // Render logs
      if (activeJob.logs && activeJob.logs.length > 0) {
        terminalLogs.innerHTML = activeJob.logs.map(l => `<div>${escapeHtml(l)}</div>`).join("");
        terminalBox.scrollTop = terminalBox.scrollHeight;
      }
    } else {
      activeJobSection.classList.add("is-idle");
      activeFileName.textContent = "No active encode";
      activeFileName.removeAttribute("title");
      updateStepper(0, 0);
      activeStageText.textContent = "Waiting for queue…";
      activePct.textContent = "0%";
      activeProgressFill.style.width = "0%";
      activeFps.textContent = "—";
      activeElapsed.textContent = "00:00:00";
      if (activeRemaining) activeRemaining.textContent = "—";
      resetFinalEta();
      syncContainerUi();
    }

    // Queue list — keep active jobs visible; completed live under Finished
    const listJobs = queueState.jobs.filter(j => j.status !== "COMPLETED");
    if (listJobs.length === 0) {
      queueList.innerHTML = "";
    } else {
      queueList.innerHTML = listJobs.map(job => renderQueueCard(job)).join("");
      applyQueueFilenameTooltips();
      
      // Attach remove handlers
      document.querySelectorAll("#queueList .btn-remove-job").forEach(btn => {
        btn.addEventListener("click", (e) => {
          const jid = e.currentTarget.getAttribute("data-id");
          removeJob(jid);
        });
      });
      document.querySelectorAll("#queueList .btn-edit-job").forEach(btn => {
        btn.addEventListener("click", (e) => {
          const jid = e.currentTarget.getAttribute("data-id");
          openEditJobModal(jid);
        });
      });
      document.querySelectorAll("#queueList .btn-requeue-job").forEach(btn => {
        btn.addEventListener("click", (e) => {
          const jid = e.currentTarget.getAttribute("data-id");
          requeueJob(jid);
        });
      });
      document.querySelectorAll("#queueList .btn-view-log-queue").forEach(btn => {
        btn.addEventListener("click", (e) => {
          const jid = e.currentTarget.getAttribute("data-id");
          const job = queueState.jobs.find(j => j.id === jid);
          if (job) openLogReportModal(job);
        });
      });
      document.querySelectorAll("#queueList .btn-sync-preset").forEach(btn => {
        btn.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          const jid = e.currentTarget.getAttribute("data-id");
          syncJobPresetFromCurrent(jid);
        });
      });
      wireQueueDragReorder();
    }
  }

  let _queueDragPersistTimer = null;
  let _queueDragOrderBefore = null;

  function getQueueCardDragAfterElement(container, y) {
    const cards = [...container.querySelectorAll(".queue-card:not(.is-dragging)")];
    return cards.reduce((closest, child) => {
      const box = child.getBoundingClientRect();
      const offset = y - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset) {
        return { offset, element: child };
      }
      return closest;
    }, { offset: Number.NEGATIVE_INFINITY, element: null }).element;
  }

  function readQueueDomOrder() {
    return [...queueList.querySelectorAll(".queue-card[data-id]")]
      .map(el => el.getAttribute("data-id"))
      .filter(Boolean);
  }

  function applyLocalQueueOrder(orderedIds) {
    const byId = new Map((queueState.jobs || []).map(j => [j.id, j]));
    const next = [];
    const seen = new Set();
    orderedIds.forEach(id => {
      const job = byId.get(id);
      if (job && !seen.has(id)) {
        next.push(job);
        seen.add(id);
      }
    });
    (queueState.jobs || []).forEach(j => {
      if (!seen.has(j.id)) next.push(j);
    });
    queueState.jobs = next;
  }

  async function persistQueueOrder(orderedIds) {
    try {
      const res = await fetch("/api/queue/reorder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_ids: orderedIds })
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || res.statusText || "Reorder failed");
      }
      const data = await res.json();
      if (Array.isArray(data.jobs)) queueState.jobs = data.jobs;
    } catch (e) {
      console.warn("Queue reorder failed", e);
      // Snap back to last known server order on next WS/initial refresh
      if (_queueDragOrderBefore) applyLocalQueueOrder(_queueDragOrderBefore);
      renderQueue();
    }
  }

  function wireQueueDragReorder() {
    if (!queueList) return;
    queueList.querySelectorAll(".queue-card.is-draggable").forEach(card => {
      const handle = card.querySelector(".queue-card-drag-handle");
      if (!handle) return;
      handle.addEventListener("dragstart", (e) => {
        _queueDragOrderBefore = readQueueDomOrder();
        card.classList.add("is-dragging");
        try {
          e.dataTransfer.effectAllowed = "move";
          e.dataTransfer.setData("text/plain", card.getAttribute("data-id") || "");
          // Drag the whole card visually, not just the handle glyph
          const rect = card.getBoundingClientRect();
          e.dataTransfer.setDragImage(card, Math.min(24, rect.width / 4), Math.min(20, rect.height / 2));
        } catch (_) { /* IE / restricted */ }
      });
      handle.addEventListener("dragend", async () => {
        card.classList.remove("is-dragging");
        const ordered = readQueueDomOrder();
        applyLocalQueueOrder(ordered);
        const before = (_queueDragOrderBefore || []).join(",");
        _queueDragOrderBefore = null;
        if (ordered.join(",") === before) return;
        if (_queueDragPersistTimer) clearTimeout(_queueDragPersistTimer);
        _queueDragPersistTimer = setTimeout(() => persistQueueOrder(ordered), 40);
      });
    });
  }

  if (queueList && !queueList.dataset.dragBound) {
    queueList.dataset.dragBound = "1";
    queueList.addEventListener("dragover", (e) => {
      const dragging = queueList.querySelector(".queue-card.is-dragging");
      if (!dragging) return;
      e.preventDefault();
      try { e.dataTransfer.dropEffect = "move"; } catch (_) { /* noop */ }
      const after = getQueueCardDragAfterElement(queueList, e.clientY);
      if (after == null) queueList.appendChild(dragging);
      else queueList.insertBefore(dragging, after);
    });
    queueList.addEventListener("drop", (e) => {
      e.preventDefault();
    });
  }

  async function syncJobPresetFromCurrent(jobId) {
    const job = queueState.jobs.find(j => j.id === jobId);
    if (!job || job.status !== "QUEUED") return;
    const preset = getPresetForJob(job);
    if (!preset) {
      await customAlert("Preset for this job was deleted. Pick a preset in Edit.");
      return;
    }
    const config = configFromPresetKeepingJobExtras(job, preset);
    try {
      const res = await fetch("/api/queue/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobId, config })
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || res.statusText || "Update failed");
      }
      const data = await res.json();
      if (data.job) {
        const idx = queueState.jobs.findIndex(j => j.id === jobId);
        if (idx !== -1) queueState.jobs[idx] = data.job;
        renderQueue();
      }
    } catch (e) {
      await customAlert("Could not update job from preset: " + e.message);
    }
  }

  function applyQueueFilenameTooltips() {
    document.querySelectorAll("#queueList .q-filename").forEach(el => {
      const original = el.getAttribute("data-original-name") || "";
      const display = el.getAttribute("data-full-name") || el.textContent || "";
      // Hover shows original source name; if truncated and no separate original, show full target
      if (original && original !== display) {
        el.title = `Original: ${original}`;
      } else if (el.scrollWidth > el.clientWidth + 1) {
        el.title = display;
      } else {
        el.removeAttribute("title");
      }
    });
  }

  function shortLangBadge(code) {
    const fam = langFamily(code);
    return fam ? fam.toLowerCase() : "";
  }

  /** Language families that will actually be encoded for this job. */
  function selectedEncodeLangFamilies(job) {
    const tracks = job?.media_info?.audio_tracks || [];
    if (!tracks.length) return new Set();

    const order = job?.config?.audio_tracks_order;
    let selectedTracks;
    if (Array.isArray(order)) {
      if (order.length === 0) return new Set();
      const byIdx = new Map(tracks.map((t) => [t.stream_index, t]));
      selectedTracks = order.map((i) => byIdx.get(i)).filter(Boolean);
    } else {
      const cfg = job?.config || {};
      const prefs = {
        languages: Array.isArray(cfg.audio_languages) && cfg.audio_languages.length
          ? cfg.audio_languages.map(langFamily)
          : getPipelineAudioLanguages(),
        bestOnly: cfg.audio_best_only !== false
      };
      const auto = getAutoSelectedTrackIds(tracks, prefs);
      selectedTracks = tracks.filter((t) => auto.has(t.stream_index));
    }
    return new Set(selectedTracks.map((t) => langFamily(t.language)).filter(Boolean));
  }

  /** Pipeline language badges in settings order; on = included in encode. */
  function pipelineAudioLangBadges(job) {
    const pipelineLangs = getPipelineAudioLanguages();
    const selected = selectedEncodeLangFamilies(job);
    return pipelineLangs.map((code) => {
      const on = selected.has(code);
      const state = on ? "is-on" : "is-off";
      const label = audioLangLabel(code);
      const title = on
        ? `${label} — included in encode`
        : `${label} — not included in encode`;
      return `<span class="q-status-badge q-lang-badge ${state}" title="${escapeHtml(title)}">${escapeHtml(shortLangBadge(code))}</span>`;
    }).join("");
  }

  function renderQueueCard(job) {
    const isFailed = job.status === "FAILED";
    const isCancelled = job.status === "CANCELLED";
    const isActive = ACTIVE_JOB_STATUSES.includes(job.status);
    const statusClass = isFailed || isCancelled
      ? "q-status-failed"
      : (isActive ? "q-status-active" : "q-status-queued");
    const isTest = !!job.config?.test_mode;
    const testLabel = isTest
      ? `${job.config.trim_start || "?"} → ${job.config.trim_end || "?"}`
      : "";
    const canEdit = job.status === "QUEUED";
    const canRequeue = isCancelled || isFailed;
    const canDrag = !isActive;
    const tag = job.media_tag || {};
    const outName = (job.output_path || "").split(/[\\/]/).pop() || tag.library_name || "";
    const displayName = outName || job.filename || "";
    const imdb = tag.imdb_id || "";
    const quality = tag.quality || "";
    const year = tag.year ? String(tag.year) : "";
    const tagSource = tag.source || "";
    const audioLangBadges = pipelineAudioLangBadges(job);
    const presetName = job.config?.preset_name || "—";
    const outdated = canEdit && isJobPresetOutdated(job);
    const presetBadge = outdated
      ? `<span class="q-status-badge q-preset-badge q-outdated-badge" title="Preset settings changed since this job was queued">${escapeHtml(presetName)} - Outdated <button type="button" class="btn-sync-preset" data-id="${job.id}" title="Update job from current preset" aria-label="Update from preset">↺</button></span>`
      : `<span class="q-status-badge q-preset-badge">${escapeHtml(presetName)}</span>`;

    return `
      <div class="queue-card${isCancelled ? " is-cancelled" : ""}${canDrag ? " is-draggable" : ""}" data-id="${job.id}">
        <span class="queue-card-drag-handle" title="Drag to reorder"${canDrag ? ' draggable="true"' : ""} aria-hidden="true">⋮⋮</span>
        <div class="queue-card-left">
          <div class="q-filename" data-full-name="${escapeHtml(displayName)}" data-original-name="${escapeHtml(job.filename || "")}">${escapeHtml(displayName)}</div>
          <div class="q-meta">
            <span class="q-status-badge ${statusClass}">${job.status}</span>
            ${presetBadge}
            ${year ? `<span class="q-status-badge q-tag-badge">${escapeHtml(year)}</span>` : ""}
            ${quality ? `<span class="q-status-badge q-tag-badge">${escapeHtml(quality)}</span>` : ""}
            ${imdb
              ? `<span class="q-status-badge q-imdb-badge" title="IMDb">${escapeHtml(imdb)}</span>`
              : `<span class="q-status-badge q-tag-warn" title="${escapeHtml(tag.lookup_note || "No IMDb ID")}">no IMDb</span>`}
            ${(!imdb && tagSource === "tmdb") ? `<span class="q-status-badge q-tag-badge">TMDB</span>` : ""}
            ${audioLangBadges}
            ${isTest ? `<span class="q-status-badge badge-test">TEST ${escapeHtml(testLabel)}</span>` : ""}
          </div>
        </div>
        <div class="queue-card-right">
          ${canRequeue ? `<button class="btn btn-outline btn-sm btn-view-log-queue" data-id="${job.id}" title="Failure report" aria-label="Failure report">
            <svg class="btn-glyph" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M3.5 1.5h6.2L13 4.8V14a.5.5 0 0 1-.5.5h-9A.5.5 0 0 1 3 14V2a.5.5 0 0 1 .5-.5zm6 .9V5h2.6L9.5 2.4zM5 7h6v1H5V7zm0 2.5h6v1H5v-1zm0 2.5h4v1H5v-1z"/></svg>
          </button>` : ""}
          ${canRequeue ? `<button class="btn btn-outline btn-sm btn-requeue-job" data-id="${job.id}" title="Reset to queue">↺ Reset</button>` : ""}
          ${canEdit ? `<button class="btn btn-outline btn-sm btn-edit-job" data-id="${job.id}" title="Edit">✎</button>` : ""}
          ${isActive ? "" : `<button class="btn btn-outline btn-sm btn-remove-job" data-id="${job.id}">✕</button>`}
        </div>
      </div>
    `;
  }

    function updateActiveJobProgress(data) {
    if (data.id && queueState.currentJobId && data.id !== queueState.currentJobId) return;
    if (data.id && !queueState.currentJobId) queueState.currentJobId = data.id;
    if (data.stage) activeStageText.textContent = data.stage;
    const hasStagePct = data.stage_percent !== undefined && data.stage_percent !== null;
    const activeJob = queueState.jobs.find(j => j.id === (data.id || queueState.currentJobId));
    if (data.stage_num) {
      updateStepper(stageNumToStep(data.stage_num), hasStagePct ? data.stage_percent : undefined);
    } else if (hasStagePct) {
      updateStepper(
        Number(document.querySelector(".step-node.active")?.id?.replace("step", "") || 0) || 0,
        data.stage_percent
      );
    }
    if (data.progress !== undefined && data.progress !== null) {
      activePct.textContent = `${Math.round(data.progress)}%`;
      activeProgressFill.style.width = `${data.progress}%`;
    }
    if (data.fps !== undefined && data.fps !== null) {
      activeFps.textContent = Number(data.fps).toFixed(1);
    }
    if (data.elapsed !== undefined) {
      activeElapsed.textContent = formatSeconds(data.elapsed);
    }
    updateRemainingEta(activeJob, data);
    if (data.log) {
      const div = document.createElement("div");
      div.textContent = data.log;
      terminalLogs.appendChild(div);
      if (terminalLogs.children.length > 200) terminalLogs.removeChild(terminalLogs.firstChild);
      terminalBox.scrollTop = terminalBox.scrollHeight;
    }
  }

  function updateStepper(activeNum, stagePct) {
    const hasPct = stagePct !== undefined && stagePct !== null && !Number.isNaN(Number(stagePct));
    const pct = hasPct ? Math.max(0, Math.min(100, Number(stagePct))) : null;
    for (let i = 1; i <= 3; i++) {
      const node = document.getElementById(`step${i}`);
      const line = document.getElementById(`line${i}`);
      if (node) {
        node.classList.remove("active", "completed");
        const circle = node.querySelector(".step-circle");
        let ring = null;
        if (i < activeNum) {
          node.classList.add("completed");
          ring = 100;
        } else if (i === activeNum && activeNum > 0) {
          node.classList.add("active");
          ring = pct !== null ? pct : Number(circle?.style.getPropertyValue("--step-progress") || 0);
        } else if (circle) {
          ring = 0;
        }
        if (circle && ring !== null) circle.style.setProperty("--step-progress", String(ring));
      }
      if (line) {
        line.classList.remove("filled");
        if (i < activeNum) line.classList.add("filled");
      }
    }
  }

  // --- API Actions ---
  btnStartQueue.addEventListener("click", () => postAction("start"));
  btnPauseQueue.addEventListener("click", () => {
    if (queueState.isPaused) postAction("resume");
    else postAction("pause");
  });
  btnStopQueue.addEventListener("click", () => postAction("stop"));


  async function postAction(action) {
    try {
      const body = { action };
      if (action === "start") {
        const tm = getTestModeJobFields();
        const queued = (queueState.jobs || []).filter((j) => j.status === "QUEUED");
        // The Test Mode toggle already stamps every queued job the moment it's
        // flipped (saveTestModeSettings -> syncTestModeToQueuedJobs), so in the
        // normal single-session flow queued jobs are always in sync here. Only
        // warn when they're genuinely NOT — e.g. a job was queued from another
        // tab/session after the last sync, or a job's Test Mode was edited
        // individually and now disagrees with the global toggle.
        const outOfSync = queued.some((j) => {
          const jobTestMode = !!j.config?.test_mode;
          if (jobTestMode !== tm.test_mode) return true;
          if (tm.test_mode) {
            return j.config?.trim_start !== tm.trim_start || j.config?.trim_end !== tm.trim_end;
          }
          return false;
        });
        if (outOfSync && queued.length >= 1) {
          const ok = await customConfirm(
            `Test Mode is ${tm.test_mode ? "ON" : "OFF"}, but ${queued.length} queued job(s) don't match it ` +
              `(added or edited since the toggle was last set).\n\n` +
              `Start will use each job's OWN Test Mode setting, not force the current toggle onto them.\n\n` +
              `Use the Test Mode toggle (or Save in Test Mode settings) if you want to stamp all queued jobs first.\n\n` +
              `Continue starting the queue?`
          );
          if (!ok) return;
        }
      }
      await fetch("/api/queue/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
    } catch (e) {
      console.error(e);
    }
  }

  async function syncTestModeToQueuedJobs() {
    try {
      await fetch("/api/queue/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "apply-test-mode",
          ...getTestModeJobFields()
        })
      });
    } catch (e) {
      console.error("Failed to sync Test Mode to queue:", e);
    }
  }

  async function removeJob(jobId) {
    try {
      await fetch("/api/queue/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "remove", job_id: jobId })
      });
    } catch (e) {
      console.error(e);
    }
  }

  async function requeueJob(jobId) {
    try {
      const res = await fetch("/api/queue/requeue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "requeue", job_id: jobId })
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || res.statusText || "Reset failed");
      }
    } catch (e) {
      await customAlert("Error resetting job: " + e);
    }
  }

  // --- System Stats Poller ---
  const pipelineCpuCores = document.getElementById("pipelineCpuCores");

  async function pollSystemStats() {
    try {
      const res = await fetch("/api/system");
      const data = await res.json();
      cpuVal.textContent = `${Math.round(data.cpu_percent)}%`;
      cpuBar.style.width = `${data.cpu_percent}%`;

      if (data.gpu) {
        gpuVal.textContent = `${Math.round(data.gpu.utilization)}%`;
        gpuBar.style.width = `${data.gpu.utilization}%`;
      }

      ramVal.textContent = `${data.ram_used_gb} GB`;
      ramBar.style.width = `${data.ram_percent}%`;

      if (pipelineCpuCores && data.cpu_logical) {
        const logical = data.cpu_logical;
        const physical = data.cpu_physical;
        pipelineCpuCores.textContent = physical && physical !== logical
          ? `${logical} threads (${physical} cores)`
          : `${logical} cores`;
      }
    } catch (e) {}
  }
  // Self-scheduling rather than setInterval: /api/system can block (nvidia-smi)
  // during a heavy encode, and a fixed interval would stack requests that
  // later land out of order and make the gauges jump through stale samples.
  (async function pollSystemStatsLoop() {
    for (;;) {
      await pollSystemStats();
      await new Promise(r => setTimeout(r, 2000));
    }
  })();

  // --- Native Windows File Dialog & In-App Browser ---
  // Prevent browser default window drag/drop behavior (opening media in browser)
  window.addEventListener("dragover", (e) => e.preventDefault(), false);
  window.addEventListener("drop", (e) => e.preventDefault(), false);

  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("dragover");
  });
  dropZone.addEventListener("dragleave", (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
  });
  dropZone.addEventListener("drop", async (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
    openNativeFilePicker();
  });
  dropZone.addEventListener("click", () => openNativeFilePicker());

  const btnOpenNativeFromModal = document.getElementById("btnOpenNativeFromModal");
  if (btnOpenNativeFromModal) btnOpenNativeFromModal.addEventListener("click", () => openNativeFilePicker());
  btnCloseModal.addEventListener("click", () => browseModal.classList.add("hidden"));
  btnBrowserUp.addEventListener("click", () => {
    if (currentBrowserPath) {
      const parts = currentBrowserPath.split(/[\\/]/).filter(Boolean);
      parts.pop();
      const parent = parts.join("\\");
      openBrowser(parent || "drives");
    } else {
      openBrowser("drives");
    }
  });

  const loadingModal = document.getElementById("loadingModal");
  const loadingMessage = document.getElementById("loadingMessage");
  let loadingDepth = 0;

  function showLoading(message) {
    loadingDepth += 1;
    if (loadingMessage) loadingMessage.textContent = message || "Loading…";
    if (loadingModal) loadingModal.classList.remove("hidden");
  }

  function hideLoading() {
    loadingDepth = Math.max(0, loadingDepth - 1);
    if (loadingDepth === 0 && loadingModal) loadingModal.classList.add("hidden");
  }

  function setLoadingMessage(message) {
    if (loadingMessage && message) loadingMessage.textContent = message;
  }

  async function openNativeFilePicker() {
    dropZone.style.pointerEvents = "none";
    // Modal up before the OS dialog so when the dialog closes the UI is already
    // busy — pick_files can still take a moment (tk teardown / path resolve).
    showLoading("Select video files…");
    let data = null;
    try {
      const res = await fetch("/api/dialog/pick_files", { method: "POST" });
      data = await res.json();
    } catch (e) {
      console.error("Picker error, falling back to browser modal:", e);
      hideLoading();
      dropZone.style.pointerEvents = "";
      openBrowser("drives");
      return;
    }
    dropZone.style.pointerEvents = "";

    if (!(data && data.status === "ok" && data.paths && data.paths.length > 0)) {
      hideLoading();
      return;
    }

    if (data.paths.length === 1) {
      try {
        await inspectFile(data.paths[0], { alreadyLoading: true });
        browseModal.classList.remove("hidden");
      } finally {
        hideLoading();
      }
      return;
    }

    const activeP = allPresets.find(p => p.id === selectedPreset) || {};
    const cfg = buildConfigFromPreset(activeP, getTestModeJobFields());
    const n = data.paths.length;
    setLoadingMessage(`Adding ${n} file${n === 1 ? "" : "s"}…`);
    let result = null;
    let addErr = null;
    try {
      result = await fetchJson("/api/queue/batch_add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: data.paths, config: cfg })
      });
    } catch (e) {
      addErr = e;
    } finally {
      hideLoading();
    }
    if (addErr) {
      await customAlert("Error adding files: " + (addErr.message || addErr));
      return;
    }
    // batch_add reports per-file failures in its body, not the status code —
    // without this a partially failed multi-select looks like it fully worked.
    const failed = (result && result.errors) || [];
    if (failed.length) {
      const lines = failed
        .slice(0, 8)
        .map(f => `• ${(f.path || "").split(/[\\/]/).pop()}: ${f.error}`)
        .join("\n");
      const more = failed.length > 8 ? `\n…and ${failed.length - 8} more.` : "";
      await customAlert(
        `Added ${(result && result.added) || 0} file(s). ${failed.length} could not be added:\n\n${lines}${more}`
      );
    }
  }

  async function openBrowser(path) {
    browseModal.classList.remove("hidden");
    inspectorPanel.classList.add("hidden");
    if (browserChrome) browserChrome.classList.remove("hidden");
    try {
      const res = await fetch(`/api/browse?path=${encodeURIComponent(path || "drives")}`);
      const data = await res.json();
      currentBrowserPath = data.current_path;
      browserCurrentPath.textContent = data.current_path;

      browserItemsList.innerHTML = data.items.map(item => `
        <div class="b-item" data-path="${escapeHtml(item.path)}" data-isdir="${item.is_dir}">
          <span>${item.is_dir ? "📁" : "🎬"}</span>
          <span>${escapeHtml(item.name)}</span>
        </div>
      `).join("");

      document.querySelectorAll(".b-item").forEach(el => {
        el.addEventListener("click", async () => {
          const isDir = el.getAttribute("data-isdir") === "true";
          const p = el.getAttribute("data-path");
          if (isDir) {
            openBrowser(p);
          } else {
            document.querySelectorAll(".b-item").forEach(b => b.classList.remove("active"));
            el.classList.add("active");
            inspectFile(p);
          }
        });
      });
    } catch (e) {
      console.error(e);
    }
    }

  // Mirrors core/pipeline.py select_and_prioritize_audio's ordering: preferred
  // languages first (in priority order), everything else after, in original order.
  function orderTracksByLanguage(tracks, languages) {
    const priority = languages && languages.length ? languages : [...DEFAULT_AUDIO_LANGUAGES];
    const buckets = {};
    priority.forEach(key => { buckets[key] = []; });
    const other = [];
    tracks.forEach(t => {
      const fam = langFamily(t.language);
      if (buckets[fam]) buckets[fam].push(t);
      else other.push(t);
    });
    return [...priority.flatMap(key => buckets[key]), ...other];
  }

  function pickBestTrack(tracks) {
    if (!tracks || tracks.length === 0) return null;
    return tracks.reduce((best, t) => {
      const score = (x) => {
        const ch = Number(x.channels || 0);
        const br = Number(x.bitrate || 0);
        const surround = ch >= 5 ? 1 : 0;
        return [surround, ch, br];
      };
      const a = score(t);
      const b = score(best);
      for (let i = 0; i < a.length; i++) {
        if (a[i] !== b[i]) return a[i] > b[i] ? t : best;
      }
      return best;
    });
  }

  function getPipelineAudioPrefs() {
    return {
      languages: getPipelineAudioLanguages(),
      bestOnly: getAudioBestOnly()
    };
  }

  function getAutoSelectedTrackIds(tracks, prefs = null) {
    const { languages, bestOnly } = prefs || getPipelineAudioPrefs();
    const byLang = {};
    tracks.forEach(t => {
      const key = langFamily(t.language);
      if (!byLang[key]) byLang[key] = [];
      byLang[key].push(t);
    });

    const selected = new Set();
    const priority = languages.length ? languages : [...DEFAULT_AUDIO_LANGUAGES];
    priority.forEach(key => {
      const group = byLang[key];
      if (!group || !group.length) return;
      if (bestOnly) {
        const best = pickBestTrack(group);
        if (best) selected.add(best.stream_index);
      } else {
        group.forEach(t => selected.add(t.stream_index));
      }
    });

    // If no preferred-language tracks exist, auto-select one overall best track
    if (selected.size === 0 && tracks.length > 0) {
      const best = pickBestTrack(tracks);
      if (best) selected.add(best.stream_index);
    }
    return selected;
  }

  function applyAutoAudioSelection(tracks) {
    const prefs = getPipelineAudioPrefs();
    const auto = getAutoSelectedTrackIds(tracks, prefs);
    document.querySelectorAll(".track-checkbox").forEach(cb => {
      const sidx = parseInt(cb.getAttribute("data-sidx"), 10);
      cb.checked = auto.has(sidx);
    });
    const hint = document.getElementById("inspAudioHint");
    if (hint) {
      hint.textContent = "Auto-select: Best tracks selected.";
    }
  }

  let probeSeq = 0;

  async function inspectFile(filePath, opts = {}) {
    const mySeq = ++probeSeq;
    const shortName = filePath.split(/[\\/]/).pop() || "file";
    const alreadyLoading = !!(opts && opts.alreadyLoading);
    if (alreadyLoading) {
      setLoadingMessage(`Reading ${shortName}…`);
    } else {
      showLoading(`Reading ${shortName}…`);
    }
    try {
      if (browserChrome) browserChrome.classList.add("hidden");
      inspectorPanel.classList.remove("hidden");
      inspFileName.textContent = shortName;
      inspVideoMeta.innerHTML = `<span class="badge badge-av1">Probing streams...</span>`;
      inspAudioTracks.innerHTML = `<div class="track-empty">Loading audio streams...</div>`;

      const res = await fetch(`/api/probe?path=${encodeURIComponent(filePath)}&audio_format=${encodeURIComponent(getAudioFormat())}`);
      const data = await res.json();
      // A slower earlier probe must not repaint the panel (or set
      // currentProbedMedia) over the file the user has since clicked.
      if (mySeq !== probeSeq) return;
      currentProbedMedia = { path: filePath, data };

      const v = data.media?.video || {};
      const hdr = data.hdr || {};
      
      const hdrTag = hdrSourceLabel(hdr);

      inspVideoMeta.innerHTML = `
        <span class="badge badge-av1">${v.width}x${v.height}</span>
        <span class="badge badge-hdr">${hdrTag}</span>
      `;

      syncInspPresetSelect();
      const tracks = data.prioritized_audio || [];
      if (tracks.length === 0) {
        inspAudioTracks.innerHTML = `<div class="track-empty">No audio tracks found in this file.</div>`;
      } else {
        inspAudioTracks.innerHTML = tracks.map((t) => {
          const layout = audioLayoutDesc(t);
          const rawTitle = (t.title || "").trim();
          // Prefer a real track label; never fall back to filename-looking junk
          let trackTitle = rawTitle;
          if (!trackTitle) {
            trackTitle = layout || `${(t.language || "und").toUpperCase()} Audio`;
          }
          const badge = audioTrackEncodeBadge(t, {
            formatLabel: audioFormatLabel(getAudioFormat()),
            bitrate: t.target_bitrate || ""
          });
          return `
          <label class="track-row" data-sidx="${t.stream_index}">
            <input type="checkbox" class="track-checkbox" data-sidx="${t.stream_index}">
            <div class="track-info">
              <div class="track-primary">
                <span class="track-lang ${langToneClass(t.language)}">${(t.language || "und").toUpperCase()}</span>
                <span class="track-title" title="${escapeHtml(trackTitle)}">${escapeHtml(trackTitle)}</span>
              </div>
              <span class="track-desc">${escapeHtml(t.codec || "")}</span>
            </div>
            <span class="track-badge">${escapeHtml(badge)}</span>
          </label>`;
        }).join("");
        applyAutoAudioSelection(tracks);
      }

      const inspPresetSelect = document.getElementById("inspPresetSelect");
      if (inspPresetSelect && !inspPresetSelect.dataset.audioBound) {
        inspPresetSelect.dataset.audioBound = "1";
        inspPresetSelect.addEventListener("change", () => {
          const t = currentProbedMedia?.data?.prioritized_audio || [];
          if (t.length) applyAutoAudioSelection(t);
        });
      }

    } catch (e) {
      console.error("Probe error:", e);
    } finally {
      if (!alreadyLoading) hideLoading();
    }
  }

  btnConfirmAddJob.addEventListener("click", async () => {
    if (!currentProbedMedia) return;
    const selectedSidx = [];
    document.querySelectorAll(".track-checkbox:checked").forEach(cb => {
      selectedSidx.push(parseInt(cb.getAttribute("data-sidx"), 10));
    });

    const inspPresetSelect = document.getElementById("inspPresetSelect");
    const chosenPresetId = inspPresetSelect ? inspPresetSelect.value : selectedPreset;
    const activeP = allPresets.find(p => p.id === chosenPresetId) || {};
    
    const payload = {
      input_path: currentProbedMedia.path,
      config: buildConfigFromPreset(activeP, getTestModeJobFields())
    };
    // Always persist selection; [] means video-only (no auto fallback)
    payload.config.audio_tracks_order = selectedSidx;

    try {
      await fetchJson("/api/queue/add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      browseModal.classList.add("hidden");
    } catch (e) {
      // Keep the modal open — closing it on a rejected add made the failure
      // invisible and the job silently never appeared in the queue.
      await customAlert("Error adding job: " + (e.message || e));
    }
  });

  // --- Navigation Tabs & History Screen ---
  const tabQueue = document.getElementById("tabQueue");
  const tabHistory = document.getElementById("tabHistory");
  const viewQueue = document.getElementById("viewQueue");
  const viewHistory = document.getElementById("viewHistory");
  const historyList = document.getElementById("historyList");
  const historyCountBadge = document.getElementById("historyCountBadge");
  const btnClearHistory = document.getElementById("btnClearHistory");
  const testModeEnabled = document.getElementById("testModeEnabled");
  const testModeFields = document.getElementById("testModeFields");
  const testModeNote = document.getElementById("testModeNote");
  const btnTestModeToggle = document.getElementById("btnTestModeToggle");
  const btnTestModeSettings = document.getElementById("btnTestModeSettings");
  const testModeControl = document.getElementById("testModeControl");
  const testModeModal = document.getElementById("testModeModal");
  const btnCloseTestModeModal = document.getElementById("btnCloseTestModeModal");
  const btnCloseTestModeModalFooter = document.getElementById("btnCloseTestModeModalFooter");
  const btnSaveTestMode = document.getElementById("btnSaveTestMode");
  const TEST_MODE_KEY = "av1queue_test_mode";

  const editJobModal = document.getElementById("editJobModal");
  const editJobTabDetails = document.getElementById("editJobTabDetails");
  const editJobTabAudio = document.getElementById("editJobTabAudio");
  const editJobTabParams = document.getElementById("editJobTabParams");
  const editJobDetailsSection = document.getElementById("editJobDetailsSection");
  const editJobAudioSection = document.getElementById("editJobAudioSection");
  const editJobParamsSection = document.getElementById("editJobParamsSection");

  function showEditJobSection(which) {
    const sections = {
      details: editJobDetailsSection,
      audio: editJobAudioSection,
      params: editJobParamsSection
    };
    const tabs = {
      details: editJobTabDetails,
      audio: editJobTabAudio,
      params: editJobTabParams
    };
    Object.entries(sections).forEach(([key, el]) => {
      if (el) el.classList.toggle("hidden", key !== which);
    });
    Object.entries(tabs).forEach(([key, el]) => {
      if (el) el.classList.toggle("active", key === which);
    });
  }

  if (editJobTabDetails) editJobTabDetails.addEventListener("click", () => showEditJobSection("details"));
  if (editJobTabAudio) editJobTabAudio.addEventListener("click", () => showEditJobSection("audio"));
  if (editJobTabParams) editJobTabParams.addEventListener("click", () => showEditJobSection("params"));
  const editJobId = document.getElementById("editJobId");
  const editJobFileName = document.getElementById("editJobFileName");
  const editJobPreset = document.getElementById("editJobPreset");
  const editJobAudioTracks = document.getElementById("editJobAudioTracks");
  const btnSaveEditJob = document.getElementById("btnSaveEditJob");
  const btnCancelEditJob = document.getElementById("btnCancelEditJob");
  const btnCloseEditJob = document.getElementById("btnCloseEditJob");

  let editJobPreviewJob = null;

  /** Live library basename from edit-job fields (mirrors refresh_media_tag_quality). */
  function buildEditJobLibraryName(job) {
    const tag = (job && job.media_tag) || {};
    const titleEl = document.getElementById("editJobTitle");
    const yearEl = document.getElementById("editJobYear");
    const imdbEl = document.getElementById("editJobImdb");
    const title = (titleEl?.value || "").trim() || tag.title || "Unknown";
    const yearRaw = (yearEl?.value || "").trim();
    const yearNum = yearRaw ? Number(yearRaw) : null;
    const year = Number.isFinite(yearNum) ? yearNum : null;
    let imdb = ((imdbEl?.value || "").trim() || tag.imdb_id || "").toLowerCase();
    if (imdb && !imdb.startsWith("tt") && /^\d+$/.test(imdb)) imdb = `tt${imdb}`;

    const presetId = editJobPreset ? editJobPreset.value : (job.config?.preset_id || "");
    const preset = (allPresets || []).find((p) => p.id === presetId) || {};
    const container = preset.container || job.config?.container || "mp4";
    const ext = String(container).toLowerCase() === "webm" ? "webm" : "mp4";
    const resolutionTarget = preset.resolution_target
      || job.config?.resolution_target
      || tag.resolution_target
      || "source";
    const video = job.media_info?.video || {};
    const hints = tag.hints || {};
    const hdr = job.media_info?.hdr;
    const res = resolutionFromTarget(resolutionTarget, video, hints);
    let hdrTag = hdrFilenameTag(hdr);
    if (!hdrTag && hints.hdr) {
      hdrTag = hints.hdr === "DoVi" ? "HDR10" : String(hints.hdr);
    }
    const quality = [res, hdrTag].filter(Boolean).join(" ") || tag.quality || "AV1";
    const q = String(quality).trim();
    const qParts = q.split(/\s+/);
    const name = sanitizeFilenamePart(title) || "Unknown";
    const season = tag.season != null ? pad2(tag.season) : "";
    const episode = tag.episode != null ? pad2(tag.episode) : "";
    const values = {
      name,
      title: name,
      year: year != null ? String(year) : "",
      imdbid: imdb,
      imdb,
      quality: sanitizeFilenamePart(q),
      resolution: qParts[0] || "",
      hdr: qParts.slice(1).join(" ") || "",
      width: video.width ? String(video.width) : "",
      height: video.height ? String(video.height) : "",
      original: sanitizeFilenamePart(tag.original || title) || name,
      show: sanitizeFilenamePart(tag.show || title) || name,
      epname: sanitizeFilenamePart(tag.epname || ""),
      season,
      episode,
      sxxexx: season && episode ? `S${season}E${episode}` : "",
      ext
    };
    const kind = tag.media_kind || "movie";
    const rawTpl = kind === "episode"
      ? (appSettings.name_template_episode || DEFAULT_NAME_TEMPLATE_EPISODE)
      : (appSettings.name_template_movie || DEFAULT_NAME_TEMPLATE_MOVIE);
    let libraryName = applyNameTemplate(stripExtToken(rawTpl) || rawTpl, values);

    const testFields = getTestModeJobFields();
    const testOn = !!(testFields.test_mode || job.config?.test_mode);
    if (testOn) {
      const startTag = String(testFields.trim_start || job.config?.trim_start || "start").replace(/:/g, "");
      const endTag = String(testFields.trim_end || job.config?.trim_end || "end").replace(/:/g, "");
      const stem = libraryName.replace(/\.[^.]+$/, "");
      libraryName = `${stem}.test_${startTag}-${endTag}.${ext}`;
    }
    return libraryName;
  }

  function refreshEditJobNamePreview() {
    if (!editJobPreviewJob) return;
    const name = buildEditJobLibraryName(editJobPreviewJob);
    if (editJobFileName) {
      editJobFileName.textContent = name;
      editJobFileName.title = editJobPreviewJob.input_path || editJobPreviewJob.filename || "";
    }
  }

  const logReportModal = document.getElementById("logReportModal");
  const btnCloseLogModal = document.getElementById("btnCloseLogModal");
  const logModalTitle = document.getElementById("logModalTitle");
  const logReportSummary = document.getElementById("logReportSummary");
  const logModalContent = document.getElementById("logModalContent");

  let historyItems = [];

  function pad2(n) {
    return String(Math.max(0, Math.floor(Number(n) || 0))).padStart(2, "0");
  }

  function readTimeInputs(prefix) {
    const h = document.getElementById(`test${prefix}H`)?.value ?? 0;
    const m = document.getElementById(`test${prefix}M`)?.value ?? 0;
    const s = document.getElementById(`test${prefix}S`)?.value ?? 0;
    return `${pad2(h)}:${pad2(m)}:${pad2(s)}`;
  }

  function writeTimeInputs(prefix, hms) {
    const parts = String(hms || "00:00:00").split(":");
    const h = parts[0] ?? "0";
    const m = parts[1] ?? "0";
    const s = parts[2] ?? "0";
    const elH = document.getElementById(`test${prefix}H`);
    const elM = document.getElementById(`test${prefix}M`);
    const elS = document.getElementById(`test${prefix}S`);
    if (elH) elH.value = String(parseInt(h, 10) || 0);
    if (elM) elM.value = String(parseInt(m, 10) || 0);
    if (elS) elS.value = String(parseInt(s, 10) || 0);
  }

  function loadTestModeSettings() {
    let saved = null;
    try {
      saved = JSON.parse(localStorage.getItem(TEST_MODE_KEY) || "null");
    } catch (_) {
      saved = null;
    }
    const enabled = !!(saved && saved.enabled);
    const from = (saved && saved.from) || "01:01:10";
    const to = (saved && saved.to) || "01:01:30";
    if (testModeEnabled) testModeEnabled.checked = enabled;
    writeTimeInputs("From", from);
    writeTimeInputs("To", to);
    syncTestModeUi();
  }

  function saveTestModeSettings() {
    const payload = {
      enabled: !!(testModeEnabled && testModeEnabled.checked),
      from: readTimeInputs("From"),
      to: readTimeInputs("To")
    };
    localStorage.setItem(TEST_MODE_KEY, JSON.stringify(payload));
    syncTestModeUi();
    // Keep already-queued jobs in sync with the global toggle
    syncTestModeToQueuedJobs();
  }

  function syncTestModeUi() {
    const on = !!(testModeEnabled && testModeEnabled.checked);
    const from = readTimeInputs("From");
    const to = readTimeInputs("To");
    if (testModeFields) testModeFields.classList.toggle("is-enabled", on);
    if (btnTestModeToggle) {
      btnTestModeToggle.classList.toggle("is-on", on);
      btnTestModeToggle.setAttribute("aria-checked", on ? "true" : "false");
    }
    if (testModeControl) testModeControl.classList.toggle("is-on", on);
    if (testModeNote) {
      testModeNote.innerHTML = on
        ? `Active sample: <strong>${from} → ${to}</strong>. Queued jobs (and Start) encode only this range.`
        : `Default sample: <strong>1:01:10 → 1:01:30</strong> (20 seconds). Turn on Test Mode to apply this range when you start the queue.`;
    }
  }

  function getTestModeJobFields() {
    if (!(testModeEnabled && testModeEnabled.checked)) {
      return { test_mode: false };
    }
    return {
      test_mode: true,
      trim_start: readTimeInputs("From"),
      trim_end: readTimeInputs("To")
    };
  }

  function showMainView(which) {
    const views = { queue: viewQueue, history: viewHistory };
    const tabs = { queue: tabQueue, history: tabHistory };
    Object.entries(views).forEach(([key, el]) => {
      if (!el) return;
      el.classList.toggle("hidden", key !== which);
    });
    Object.entries(tabs).forEach(([key, el]) => {
      if (!el) return;
      el.classList.toggle("active", key === which);
    });
  }

  tabQueue.addEventListener("click", () => {
    showMainView("queue");
  });

  tabHistory.addEventListener("click", () => {
    showMainView("history");
    fetchHistory();
  });

  if (btnTestModeToggle) {
    btnTestModeToggle.addEventListener("click", () => {
      if (testModeEnabled) testModeEnabled.checked = !testModeEnabled.checked;
      saveTestModeSettings();
    });
  }

  function openTestModeModal() {
    if (testModeModal) testModeModal.classList.remove("hidden");
  }
  function closeTestModeModal() {
    if (testModeModal) testModeModal.classList.add("hidden");
  }
  if (btnTestModeSettings) btnTestModeSettings.addEventListener("click", openTestModeModal);
  if (btnCloseTestModeModal) btnCloseTestModeModal.addEventListener("click", closeTestModeModal);
  if (btnCloseTestModeModalFooter) btnCloseTestModeModalFooter.addEventListener("click", closeTestModeModal);
  if (btnSaveTestMode) {
    btnSaveTestMode.addEventListener("click", () => {
      saveTestModeSettings();
      closeTestModeModal();
    });
  }

  ["From", "To"].forEach(prefix => {
    ["H", "M", "S"].forEach(unit => {
      const el = document.getElementById(`test${prefix}${unit}`);
      if (el) el.addEventListener("change", saveTestModeSettings);
      if (el) el.addEventListener("input", syncTestModeUi);
    });
  });
  loadTestModeSettings();

  function collectNonDefaultEncodeParams(cfg) {
    const c = cfg && typeof cfg === "object" ? cfg : {};
    const rows = [];

    const push = (key, value) => {
      if (value === undefined || value === null || value === "") return;
      rows.push({ key, value: String(value) });
    };

    const crf = Number(c.crf);
    if (Number.isFinite(crf) && crf !== 35) push("crf", crf);

    const presetSpeed = Number(c.preset);
    if (Number.isFinite(presetSpeed) && presetSpeed !== 3) push("preset", presetSpeed);

    const resolution = c.resolution_target || "source";
    if (resolution !== "source") push("resolution_target", resolution);

    const audio51 = c.audio_bitrate_51 || "320k";
    if (audio51 !== "320k") push("audio_bitrate_51", audio51);

    const audioStereo = c.audio_bitrate_stereo || "160k";
    if (audioStereo !== "160k") push("audio_bitrate_stereo", audioStereo);

    const audioFmt = c.audio_format || "opus";
    if (audioFmt !== "opus") push("audio_format", audioFmt);

    const container = c.container || "mp4";
    if (container !== "mp4") push("container", container);

    if (c.autocrop === false) push("autocrop", "off");
    if (c.ssimu2_post === true) push("ssimu2_post", "on");
    if (c.extract_subtitles === false) push("extract_subtitles", "off");
    if (c.audio_best_only === false) push("audio_best_only", "off");

    if (Array.isArray(c.audio_languages) && c.audio_languages.length) {
      const def = DEFAULT_AUDIO_LANGUAGES.join(",");
      if (c.audio_languages.map(langFamily).join(",") !== def) {
        push("audio_languages", c.audio_languages.join(", "));
      }
    }

    if (c.test_mode) {
      push("test_mode", `${c.trim_start || "?"} → ${c.trim_end || "?"}`);
    }

    const svt = (c.svt_params && typeof c.svt_params === "object") ? c.svt_params : {};
    Object.keys(svt).sort().forEach(key => {
      const val = svt[key];
      const def = ESSENTIAL_SVT_SETTINGS.find(d => d.key === key);
      if (def && isSvtDefault(def, val)) return;
      // lp default is 0 (omit); low-memory default off
      if (key === "lp" && Number(val) === 0) return;
      if (key === "low-memory" && Number(val) === 0) return;
      push(key, val);
    });

    return rows;
  }

  function renderEditJobParams(cfg, opts = {}) {
    const el = document.getElementById("editJobParams");
    if (!el) return;
    const rows = collectNonDefaultEncodeParams(cfg);
    const note = opts.outdated
      ? `<p class="edit-job-params-note">Outdated from preset — showing this job’s snapshotted settings.</p>`
      : "";
    if (!rows.length) {
      el.innerHTML = `${note}<div class="edit-job-params-empty">All settings at defaults.</div>`;
      return;
    }
    el.innerHTML = note + rows.map(r => `
      <div class="edit-job-params-row">
        <span class="edit-job-params-key">${escapeHtml(r.key)}</span>
        <span class="edit-job-params-val">${escapeHtml(r.value)}</span>
      </div>`).join("");
  }

  function closeEditJobModal() {
    editJobPreviewJob = null;
    if (editJobModal) editJobModal.classList.add("hidden");
  }

  async function openEditJobModal(jobId) {
    const job = queueState.jobs.find(j => j.id === jobId);
    if (!job || job.status !== "QUEUED") {
      await customAlert("Only queued jobs can be edited.");
      return;
    }
    if (!editJobModal) return;
    editJobPreviewJob = job;
    editJobId.value = job.id;

    const tag = job.media_tag || {};
    const editTitle = document.getElementById("editJobTitle");
    const editYear = document.getElementById("editJobYear");
    const editImdb = document.getElementById("editJobImdb");
    if (editTitle) editTitle.value = tag.title || "";
    if (editYear) editYear.value = tag.year != null ? String(tag.year) : "";
    if (editImdb) editImdb.value = tag.imdb_id || "";

    if (editJobPreset) {
      editJobPreset.innerHTML = (allPresets || []).map(p =>
        `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`
      ).join("");
      const pid = job.config?.preset_id || selectedPreset;
      if (pid && allPresets.some(p => p.id === pid)) editJobPreset.value = pid;
      editJobPreset.onchange = () => {
        const chosen = allPresets.find(p => p.id === editJobPreset.value);
        if (!chosen) return;
        // Preview what Save would apply from the newly selected preset
        renderEditJobParams(buildConfigFromPreset(chosen, {
          audio_tracks_order: job.config?.audio_tracks_order,
          test_mode: job.config?.test_mode,
          trim_start: job.config?.trim_start,
          trim_end: job.config?.trim_end
        }), { outdated: false });
        refreshEditJobNamePreview();
      };
    }

    refreshEditJobNamePreview();

    renderEditJobParams(job.config || {}, { outdated: isJobPresetOutdated(job) });
    showEditJobSection("details");

    const rawTracks = job.media_info?.audio_tracks || [];
    const order = job.config?.audio_tracks_order;
    // Explicit [] = video-only; missing/null = fall back to auto defaults
    const hasExplicitOrder = Array.isArray(order);
    const selected = new Set(hasExplicitOrder ? order : []);
    if (editJobAudioTracks) {
    if (rawTracks.length === 0) {
      editJobAudioTracks.innerHTML = `<div class="track-empty">No audio tracks found.</div>`;
    } else {
      const cfg = job.config || {};
      const autoPrefs = {
        languages: Array.isArray(cfg.audio_languages) && cfg.audio_languages.length
          ? cfg.audio_languages.map(langFamily)
          : [...DEFAULT_AUDIO_LANGUAGES],
        bestOnly: cfg.audio_best_only !== false
      };
      // media_info.audio_tracks is raw probe (source stream) order — sort by
      // language priority so checkbox DOM order (and the audio_tracks_order
      // saved from it) puts the preferred language first, matching Add Job.
      const tracks = orderTracksByLanguage(rawTracks, autoPrefs.languages);
      const autoSelected = getAutoSelectedTrackIds(tracks, autoPrefs);
      editJobAudioTracks.innerHTML = tracks.map((t) => {
        const layout = audioLayoutDesc(t);
        const rawTitle = (t.title || "").trim();
        let trackTitle = rawTitle || layout || `${(t.language || "und").toUpperCase()} Audio`;
        const checked = !hasExplicitOrder
          ? (autoSelected.has(t.stream_index) ? "checked" : "")
          : (selected.has(t.stream_index) ? "checked" : "");
        const badge = audioTrackEncodeBadge(t);
        return `
          <label class="track-row" data-sidx="${t.stream_index}">
            <input type="checkbox" ${checked} class="track-checkbox edit-track-checkbox" data-sidx="${t.stream_index}">
            <div class="track-info">
              <div class="track-primary">
                <span class="track-lang ${langToneClass(t.language)}">${(t.language || "und").toUpperCase()}</span>
                <span class="track-title" title="${escapeHtml(trackTitle)}">${escapeHtml(trackTitle)}</span>
              </div>
              <span class="track-desc">${escapeHtml(t.codec || "")}</span>
            </div>
            <span class="track-badge">${escapeHtml(badge)}</span>
          </label>`;
      }).join("");
    }
    }

    editJobModal.classList.remove("hidden");
  }

  if (btnCancelEditJob) btnCancelEditJob.addEventListener("click", closeEditJobModal);
  if (btnCloseEditJob) btnCloseEditJob.addEventListener("click", closeEditJobModal);

  ["editJobTitle", "editJobYear", "editJobImdb"].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("input", refreshEditJobNamePreview);
    el.addEventListener("change", refreshEditJobNamePreview);
  });

  if (btnSaveEditJob) {
    btnSaveEditJob.addEventListener("click", async () => {
      const jid = editJobId?.value;
      if (!jid) return;
      const chosenPresetId = editJobPreset ? editJobPreset.value : selectedPreset;
      const activeP = allPresets.find(p => p.id === chosenPresetId) || {};
      const selectedSidx = [];
      document.querySelectorAll(".edit-track-checkbox:checked").forEach(cb => {
        selectedSidx.push(parseInt(cb.getAttribute("data-sidx"), 10));
      });
      const config = buildConfigFromPreset(activeP, {
        ...getTestModeJobFields(),
        audio_tracks_order: selectedSidx
      });
      const yearRaw = document.getElementById("editJobYear")?.value?.trim();
      const imdbRaw = (document.getElementById("editJobImdb")?.value || "").trim();
      let imdb = imdbRaw.toLowerCase();
      if (imdb && !imdb.startsWith("tt") && /^\d+$/.test(imdb)) imdb = `tt${imdb}`;
      const media_tag = {
        title: (document.getElementById("editJobTitle")?.value || "").trim() || undefined,
        year: yearRaw ? Number(yearRaw) : null,
        imdb_id: imdb || null
      };
      try {
        const res = await fetch("/api/queue/update", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ job_id: jid, config, media_tag })
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || res.statusText || "Update failed");
        }
        const data = await res.json();
        if (data.job) {
          const idx = queueState.jobs.findIndex(j => j.id === jid);
          if (idx !== -1) queueState.jobs[idx] = data.job;
          renderQueue();
        }
        closeEditJobModal();
      } catch (e) {
        await customAlert("Error updating job: " + e);
      }
    });
  }

  async function fetchHistory() {
    try {
      const res = await fetch("/api/history");
      historyItems = await res.json();
      renderHistory();
    } catch (e) {
      console.error("Error fetching history:", e);
    }
  }

  function formatBytesShort(bytes) {
    const n = Number(bytes) || 0;
    if (n >= 1024 ** 4) return `${(n / (1024 ** 4)).toFixed(2)} TB`;
    if (n >= 1024 ** 3) return `${(n / (1024 ** 3)).toFixed(1)} GB`;
    if (n >= 1024 ** 2) return `${Math.round(n / (1024 ** 2))} MB`;
    if (n >= 1024) return `${Math.round(n / 1024)} KB`;
    return `${Math.round(n)} B`;
  }

  // SSIMU2 quality-vs-target coloring: green within ±5 of target, fading to
  // amber then rose the further off-target in EITHER direction — well above
  // target isn't "free" either, it means bits were spent past what the target
  // asked for.
  function getSsimu2Target() {
    const n = Number(appSettings?.ssimu2_target);
    return Number.isFinite(n) ? n : 80;
  }

  function hexToRgb(hex) {
    const h = hex.replace("#", "");
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }

  function lerpRgb(hexA, hexB, t) {
    const a = hexToRgb(hexA), b = hexToRgb(hexB);
    return a.map((v, i) => Math.round(v + (b[i] - v) * t));
  }

  function ssimu2Rgb(value, target) {
    const t = Number.isFinite(target) ? target : getSsimu2Target();
    const dev = Math.abs(value - t);
    const GREEN = "#4ade80", AMBER = "#f59e0b", ROSE = "#f43f5e";
    if (dev <= 5) return hexToRgb(GREEN);
    const span = Math.min(1, (dev - 5) / 20); // 0 at dev=5, 1 at dev>=25
    return span <= 0.5 ? lerpRgb(GREEN, AMBER, span * 2) : lerpRgb(AMBER, ROSE, (span - 0.5) * 2);
  }

  function ssimu2Color(value, target) {
    if (!Number.isFinite(value)) return "";
    const [r, g, b] = ssimu2Rgb(value, target);
    return `rgb(${r}, ${g}, ${b})`;
  }

  function ssimu2Bg(value, target) {
    if (!Number.isFinite(value)) return "";
    const [r, g, b] = ssimu2Rgb(value, target);
    return `rgba(${r}, ${g}, ${b}, 0.16)`;
  }

  function renderHistory() {
    tabHistoryBadge.textContent = historyItems.length;
    historyCountBadge.textContent = `${historyItems.length} Finished`;

    if (historyItems.length === 0) {
      historyList.innerHTML = `
        <div class="card" style="text-align: center; padding: 40px 20px; color: var(--text-dim);">
          <div style="font-size: 32px; margin-bottom: 8px;">📁</div>
          <h3>No Finished Encodes Yet</h3>
          <p>Completed, skipped, and failed jobs are saved here with reports and logs.</p>
        </div>
      `;
      return;
    }

    historyList.innerHTML = historyItems.map(item => {
      const origMB = Math.round((item.stats?.original_bytes || 0) / (1024 * 1024));
      const finalMB = Math.round((item.stats?.final_bytes || 0) / (1024 * 1024));
      const reduction = item.stats?.reduction_percent !== undefined ? `-${item.stats.reduction_percent}%` : "--";
      const durStr = formatSeconds(item.stats?.duration_seconds || item.elapsed_seconds || 0);
      const outPath = item.output_path || "";
      const outName = outPath.split(/[\\/]/).pop() || item.filename || "Finished encode";
      const ssimu2Avg = item.stats?.ssimu2_avg;
      const ssimu2P15 = item.stats?.ssimu2_p15;
      const ssimu2Min = item.stats?.ssimu2_min;
      const hasSsimu2 = ssimu2Avg !== undefined && ssimu2Avg !== null;
      // Colored by p15 (15th-percentile frame score) — robust against a single
      // pathological outlier frame (like Math.min would be) while still
      // catching a real stretch of below-target quality (unlike the average).
      const ssimu2PillColor = ssimu2Color(Number(ssimu2P15));
      const ssimu2PillBg = ssimu2Bg(Number(ssimu2P15));
      const ssimu2Html = hasSsimu2
        ? `<span class="h-stats-pill h-ssimu2-pill" style="color: ${ssimu2PillColor}; background: ${ssimu2PillBg};">SSIMU2 avg: ${Math.round(Number(ssimu2Avg))} — min: ${Math.round(Number(ssimu2Min ?? 0))} — p15: ${Math.round(Number(ssimu2P15 ?? 0))}</span>`
        : "";
      const estFullBytesBadge = item.stats?.estimated_full_bytes;
      const testHtml = item.config?.test_mode
        ? `<span class="h-stats-pill badge-test">TEST${estFullBytesBadge ? ` — Est size: ${formatBytesShort(estFullBytesBadge)}` : ""}</span>`
        : "";

      const hdr = item.media_info?.hdr;
      // Prefer the tag actually written into the filename, so the badge and the
      // file on disk can never disagree; fall back for pre-media_tag records.
      const qualityParts = String(item.media_tag?.quality || "").trim().split(/\s+/);
      const hdrLabel = (qualityParts.length > 1
        ? qualityParts.slice(1).join(" ")
        : hdrFilenameTag(hdr)) || "SDR";

      const audioLayouts = encodedAudioChannelLabels(item);
      const audioBadge = audioLayouts.length
        ? `<span class="badge badge-opus">Audio: ${escapeHtml(audioLayouts.join(", "))}</span>`
        : "";

      const isSkipped = item.status === "SKIPPED" || item.stats?.skipped;
      const isFailed = item.status === "FAILED" || !!item.stats?.failed;
      const isCancelled = item.status === "CANCELLED" || !!item.stats?.cancelled;
      const sizePill = isFailed
        ? (item.error || item.stats?.error || "encode failed")
        : (isCancelled
          ? (item.error || "cancelled")
          : (isSkipped
            ? "original kept (not encoded)"
            : (item.config?.test_mode
              ? `${finalMB} MB`
              : `${reduction} (${origMB} MB ➔ ${finalMB} MB)`)));
      const statusBadge = isFailed
        ? `<span class="h-stats-pill h-failed-pill" title="${escapeHtml(item.error || item.stats?.error || "Failed")}">FAILED</span>`
        : (isCancelled
          ? `<span class="h-stats-pill h-cancelled-pill" title="${escapeHtml(item.error || "Cancelled")}">CANCELLED</span>`
          : (isSkipped
            ? `<span class="h-stats-pill badge-test" title="${escapeHtml(item.skip_reason || item.stats?.reason || "Skipped")}">SKIPPED</span>`
            : ""));
      const folderPath = (isFailed || isCancelled || isSkipped)
        ? (item.input_path || outPath)
        : outPath;
      const folderTitle = (isFailed || isCancelled || isSkipped)
        ? "Open source folder"
        : "Open output folder";

      return `
        <div class="history-card" data-id="${item.id}">
          <div class="history-card-left">
            <div class="h-title" title="${escapeHtml(outPath || outName)}">${escapeHtml(outName)}</div>
            <div class="h-meta-row">
              ${statusBadge}
              ${(!isFailed && !isCancelled) ? ssimu2Html : ""}
              <span class="h-stats-pill${isFailed || isCancelled ? " h-error-pill" : ""}" title="${escapeHtml(sizePill)}">${escapeHtml(sizePill.length > 80 ? sizePill.slice(0, 77) + "…" : sizePill)}</span>
              ${(!isFailed && !isCancelled) ? testHtml : ""}
              <span class="badge q-preset-badge">${escapeHtml(item.config?.preset_name || "—")}</span>
              <span>•</span>
              <span class="badge badge-hdr">${escapeHtml(hdrLabel)}</span>
              ${(!isFailed && !isCancelled) ? audioBadge : ""}
              <span>•</span>
              <span>Run: ${durStr}</span>
            </div>
          </div>
          <div class="history-card-right">
            <button class="btn btn-outline btn-sm btn-open-folder" data-path="${escapeHtml(folderPath)}" title="${folderTitle}" aria-label="${folderTitle}">
              <svg class="btn-glyph" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M1.5 3.5A1.5 1.5 0 0 1 3 2h3.2c.3 0 .6.1.8.3L8.3 3.5H13A1.5 1.5 0 0 1 14.5 5v7A1.5 1.5 0 0 1 13 13.5H3A1.5 1.5 0 0 1 1.5 12V3.5zm1.5 0V12h10V5H7.8L6.5 3.5H3z"/></svg>
            </button>
            <button class="btn btn-outline btn-sm btn-view-log" data-id="${item.id}" title="Report" aria-label="Report">
              <svg class="btn-glyph" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M3.5 1.5h6.2L13 4.8V14a.5.5 0 0 1-.5.5h-9A.5.5 0 0 1 3 14V2a.5.5 0 0 1 .5-.5zm6 .9V5h2.6L9.5 2.4zM5 7h6v1H5V7zm0 2.5h6v1H5v-1zm0 2.5h4v1H5v-1z"/></svg>
            </button>
            <button class="btn btn-outline btn-sm btn-requeue-history" data-id="${item.id}" title="Reset to queue (re-encode with the same settings)" aria-label="Reset to queue">↺</button>
            <button class="btn btn-outline btn-sm btn-delete-history" data-id="${item.id}" title="Delete Record" aria-label="Delete record">✕</button>
          </div>
        </div>
      `;
    }).join("");

    // Attach Action Listeners
    document.querySelectorAll(".btn-open-folder").forEach(btn => {
      btn.addEventListener("click", (e) => {
        const p = e.currentTarget.getAttribute("data-path");
        if (p) openInExplorer(p);
      });
    });

    document.querySelectorAll(".btn-view-log").forEach(btn => {
      btn.addEventListener("click", (e) => {
        const id = e.currentTarget.getAttribute("data-id");
        const job = historyItems.find(h => h.id === id);
        if (job) openLogReportModal(job);
      });
    });

    document.querySelectorAll(".btn-delete-history").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        const id = e.currentTarget.getAttribute("data-id");
        if (await customConfirm("Are you sure you want to delete:", {
          danger: true,
          subject: "this completed encode record",
          okLabel: "Delete"
        })) {
          await deleteHistoryItem(id);
        }
      });
    });

    document.querySelectorAll(".btn-requeue-history").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        const id = e.currentTarget.getAttribute("data-id");
        if (await customConfirm("Reset this encode back to the queue? It will re-encode with the same settings.")) {
          await requeueHistoryItem(id);
        }
      });
    });
  }

  btnClearHistory.addEventListener("click", async () => {
    if (await customConfirm("Are you sure you want to delete:", {
      danger: true,
      subject: "all completed encode records",
      okLabel: "Delete"
    })) {
      try {
        await fetch("/api/history/clear", { method: "POST" });
        fetchHistory();
      } catch (e) {
        await customAlert("Error clearing history: " + e);
      }
    }
  });

  async function openInExplorer(filePath) {
    try {
      await fetch("/api/open_folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: filePath })
      });
    } catch (e) {
      console.error(e);
    }
  }

  async function deleteHistoryItem(id) {
    try {
      await fetch("/api/history/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "delete", job_id: id })
      });
      fetchHistory();
    } catch (e) {
      await customAlert("Error deleting history record: " + e);
    }
  }

  async function requeueHistoryItem(id) {
    try {
      const res = await fetch("/api/history/requeue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "requeue", job_id: id })
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || res.statusText || "Reset failed");
      }
      fetchHistory();
    } catch (e) {
      await customAlert("Error resetting encode to queue: " + e);
    }
  }

  function openLogReportModal(job) {
    logReportModal.classList.remove("hidden");
    const displayName = (job.output_path || "").split(/[\\/]/).pop()
      || job.filename
      || "Encode";
    logModalTitle.textContent = `Report: ${displayName}`;

    const status = String(job.status || "").toUpperCase();
    const isFailed = status === "FAILED" || !!job.stats?.failed;
    const isCancelled = status === "CANCELLED" || !!job.stats?.cancelled;
    const isSkipped = status === "SKIPPED" || !!job.stats?.skipped;
    const isProblem = isFailed || isCancelled;

    const durStr = formatSeconds(job.stats?.duration_seconds || job.elapsed_seconds || 0);
    const dateStr = formatDateTime(job.completed_at);
    const presetName = escapeHtml(
      job.config?.preset_name
        || (Number.isFinite(Number(job.config?.crf)) ? `CRF ${Number(job.config.crf)}` : "—")
    );

    if (isProblem) {
      const errText = job.error || job.stats?.error || job.stage || "Unknown error";
      const statusLabel = isCancelled ? "Cancelled" : "Failed";
      const statusColor = isCancelled ? "var(--text-muted)" : "var(--accent-rose)";
      const whenLbl = isCancelled ? "Cancelled" : "Failed";
      const whenBox = dateStr ? `
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">${whenLbl}</span>
          <span class="rep-stat-val" style="font-size: 0.95em;">${escapeHtml(dateStr)}</span>
        </div>` : "";
      logReportSummary.innerHTML = `
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">Status</span>
          <span class="rep-stat-val" style="color: ${statusColor};">${statusLabel}</span>
        </div>
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">Elapsed</span>
          <span class="rep-stat-val">${durStr}</span>
        </div>
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">Preset</span>
          <span class="rep-stat-val">${presetName}</span>
        </div>
        ${whenBox}
        <div class="rep-error-box">
          <span class="rep-stat-lbl">Error</span>
          <pre class="rep-error-text">${escapeHtml(errText)}</pre>
        </div>
      `;
    } else if (isSkipped) {
      const reason = job.skip_reason || job.stats?.reason || "Skipped — original file kept";
      const whenBox = dateStr ? `
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">Skipped</span>
          <span class="rep-stat-val" style="font-size: 0.95em;">${escapeHtml(dateStr)}</span>
        </div>` : "";
      logReportSummary.innerHTML = `
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">Status</span>
          <span class="rep-stat-val" style="color: var(--text-muted);">Skipped</span>
        </div>
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">Elapsed</span>
          <span class="rep-stat-val">${durStr}</span>
        </div>
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">Preset</span>
          <span class="rep-stat-val">${presetName}</span>
        </div>
        ${whenBox}
        <div class="rep-error-box">
          <span class="rep-stat-lbl">Reason</span>
          <pre class="rep-error-text">${escapeHtml(reason)}</pre>
        </div>
      `;
    } else {
      const origMB = Math.round((job.stats?.original_bytes || 0) / (1024 * 1024));
      const finalMB = Math.round((job.stats?.final_bytes || 0) / (1024 * 1024));
      const reduction = job.stats?.reduction_percent !== undefined ? `-${job.stats.reduction_percent}%` : "--";
      const ssimu2Avg = job.stats?.ssimu2_avg;
      const ssimu2P15Val = Number(job.stats?.ssimu2_p15 ?? 0);
      const ssimu2MinVal = Number(job.stats?.ssimu2_min ?? 0);
      const ssimu2Box = (ssimu2Avg !== undefined && ssimu2Avg !== null) ? `
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">SSIMU2 Avg</span>
          <span class="rep-stat-val" style="color: ${ssimu2Color(Number(ssimu2Avg))};">${Number(ssimu2Avg).toFixed(2)}</span>
        </div>
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">SSIMU2 p15</span>
          <span class="rep-stat-val" style="color: ${ssimu2Color(ssimu2P15Val)};">${ssimu2P15Val.toFixed(2)}</span>
        </div>
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">SSIMU2 min</span>
          <span class="rep-stat-val" style="color: ${ssimu2Color(ssimu2MinVal)};">${ssimu2MinVal.toFixed(2)}</span>
        </div>` : "";

      const estFullBytes = job.stats?.estimated_full_bytes;
      const estFullBox = (job.stats?.test_mode && estFullBytes) ? `
        <div class="rep-stat-box">
          <span class="rep-stat-lbl" title="Extrapolated from this segment's size/duration ratio — video is CRF-mode, so this assumes the tested segment's complexity is representative of the whole title.">Est. Full-Length Size</span>
          <span class="rep-stat-val">${formatBytesShort(estFullBytes)}</span>
        </div>` : "";
      const completedBox = dateStr ? `
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">Completed</span>
          <span class="rep-stat-val" style="font-size: 0.95em;">${escapeHtml(dateStr)}</span>
        </div>` : "";

      logReportSummary.innerHTML = `
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">Space Saved</span>
          <span class="rep-stat-val" style="color: var(--accent-emerald);">${reduction}</span>
        </div>
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">Original ➔ Final</span>
          <span class="rep-stat-val">${origMB} MB ➔ ${finalMB} MB</span>
        </div>
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">Encode Time</span>
          <span class="rep-stat-val">${durStr}</span>
        </div>
        <div class="rep-stat-box">
          <span class="rep-stat-lbl">Preset</span>
          <span class="rep-stat-val">${presetName}</span>
        </div>
        ${completedBox}
        ${estFullBox}
        ${ssimu2Box}
      `;
    }

    const logs = (job.logs && job.logs.length)
      ? job.logs
      : ["No log entries recorded."];
    logModalContent.innerHTML = logs.map(l => `<div>${escapeHtml(l)}</div>`).join("");
    logModalContent.scrollTop = logModalContent.scrollHeight;
  }

  btnCloseLogModal.addEventListener("click", () => logReportModal.classList.add("hidden"));

  // Initial Fetch
  fetchHistory();

  // --- Utilities ---
  function formatSeconds(sec) {
    const h = Math.floor(sec / 3600).toString().padStart(2, '0');
    const m = Math.floor((sec % 3600) / 60).toString().padStart(2, '0');
    const s = Math.floor(sec % 60).toString().padStart(2, '0');
    return `${h}:${m}:${s}`;
  }

  function parseHmsToSeconds(val) {
    if (val == null || val === "") return null;
    const parts = String(val).trim().split(":").map(Number);
    if (parts.some(n => Number.isNaN(n))) return null;
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    if (parts.length === 1) return parts[0];
    return null;
  }

  function parseFrameRate(rate) {
    if (rate == null || rate === "") return 0;
    if (typeof rate === "number") return rate > 0 ? rate : 0;
    const s = String(rate);
    if (s.includes("/")) {
      const [n, d] = s.split("/").map(Number);
      if (d) return n / d;
    }
    const n = Number(s);
    return n > 0 ? n : 0;
  }

  function estimateEncodeFrames(job) {
    if (!job) return 0;
    let dur = 0;
    if (job.config?.test_mode) {
      const a = parseHmsToSeconds(job.config.trim_start);
      const b = parseHmsToSeconds(job.config.trim_end);
      if (a != null && b != null && b > a) dur = b - a;
    }
    if (!dur) dur = Number(job.media_info?.duration || 0);
    const fps = parseFrameRate(job.media_info?.video?.r_frame_rate);
    if (!(dur > 0) || !(fps > 0)) return 0;
    return dur * fps;
  }

  function resetFinalEta() {
    finalEtaState.jobId = null;
    finalEtaState.fpsSum = 0;
    finalEtaState.fpsN = 0;
  }

  function updateRemainingEta(job, data = {}) {
    if (!activeRemaining) return;
    const status = data.status || job?.status || "";
    const stageNum = data.stage_num != null ? Number(data.stage_num) : Number(job?.stage_num || 0);
    const isFinal = status === "FINAL_ENCODE" || stageNum === 2;
    if (!job || !isFinal) {
      if (!isFinal) resetFinalEta();
      activeRemaining.textContent = "—";
      return;
    }

    const jobId = job.id || data.id;
    if (finalEtaState.jobId !== jobId) {
      finalEtaState.jobId = jobId;
      finalEtaState.fpsSum = 0;
      finalEtaState.fpsN = 0;
    }

    const fpsNow = Number(data.fps != null ? data.fps : job.fps);
    if (fpsNow > 0) {
      finalEtaState.fpsSum += fpsNow;
      finalEtaState.fpsN += 1;
    }
    const avgFps = finalEtaState.fpsN > 0 ? finalEtaState.fpsSum / finalEtaState.fpsN : 0;
    const pct = Number(
      data.stage_percent != null ? data.stage_percent : (job.stage_percent != null ? job.stage_percent : NaN)
    );
    const frames = estimateEncodeFrames(job);

    if (avgFps > 0 && frames > 0 && !Number.isNaN(pct) && pct >= 0) {
      const remainingFrames = frames * (1 - Math.min(100, pct) / 100);
      activeRemaining.textContent = formatSeconds(Math.max(0, remainingFrames / avgFps));
      return;
    }
    activeRemaining.textContent = "—";
  }

  function escapeHtml(str) {
    if (!str) return "";
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function jsonParseSafe(str) {
    try { return JSON.parse(str); } catch (e) { return null; }
  }

  // Themed replacements for window.alert()/confirm() — same dark modal chrome
  // as the rest of the app instead of the browser's native dialog.
  const dialogModal = document.getElementById("dialogModal");
  const dialogMessage = document.getElementById("dialogMessage");
  const dialogSubject = document.getElementById("dialogSubject");
  const btnDialogOk = document.getElementById("btnDialogOk");
  const btnDialogCancel = document.getElementById("btnDialogCancel");
  let dialogResolve = null;

  function closeDialog(result) {
    if (dialogModal) dialogModal.classList.add("hidden");
    const resolve = dialogResolve;
    dialogResolve = null;
    if (resolve) resolve(result);
  }

  function openDialog({ message, subject, okLabel, cancelLabel, danger, showCancel }) {
    // Only one dialog at a time — resolve any still-open one as cancelled first.
    if (dialogResolve) closeDialog(false);
    return new Promise((resolve) => {
      dialogResolve = resolve;
      if (dialogMessage) dialogMessage.textContent = message || "";
      if (dialogSubject) {
        const hasSubject = !!(subject && String(subject).trim());
        dialogSubject.textContent = hasSubject ? String(subject).trim() : "";
        dialogSubject.classList.toggle("hidden", !hasSubject);
      }
      if (btnDialogOk) {
        btnDialogOk.textContent = okLabel || "OK";
        btnDialogOk.classList.toggle("btn-danger", !!danger);
        btnDialogOk.classList.toggle("btn-primary", !danger);
      }
      if (btnDialogCancel) {
        btnDialogCancel.classList.toggle("hidden", !showCancel);
        btnDialogCancel.textContent = cancelLabel || "Cancel";
      }
      if (dialogModal) dialogModal.classList.remove("hidden");
      btnDialogOk?.focus();
    });
  }

  function customAlert(message, opts) {
    return openDialog({ message, showCancel: false, ...opts }).then(() => undefined);
  }

  function customConfirm(message, opts) {
    return openDialog({ message, showCancel: true, ...opts });
  }

  if (btnDialogOk) btnDialogOk.addEventListener("click", () => closeDialog(true));
  if (btnDialogCancel) btnDialogCancel.addEventListener("click", () => closeDialog(false));
  if (dialogModal) {
    dialogModal.addEventListener("click", (e) => {
      if (e.target === dialogModal) closeDialog(false);
    });
  }
  document.addEventListener("keydown", (e) => {
    if (!dialogResolve || !dialogModal || dialogModal.classList.contains("hidden")) return;
    if (e.key === "Escape") { e.preventDefault(); closeDialog(false); }
    else if (e.key === "Enter") { e.preventDefault(); closeDialog(true); }
  });

  // Start WebSocket
  renderQueueControls();
  connectWebSocket();
});
