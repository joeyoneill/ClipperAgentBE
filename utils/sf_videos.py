# utils/sf_videos.py
# Helper Utils for Short-Form Video (Reel) Routes, ASS Subtitles, & Fast FFmpeg Rendering

# Imports
import asyncio
from datetime import timedelta
import difflib
from dotenv import load_dotenv
from fastapi import HTTPException, status
from google.auth import default as google_auth_default
from google.auth.transport import requests as google_requests
import imageio_ffmpeg
import os
import re
import tempfile
from typing import Any
from uuid import uuid4

# Custom Dependencies
from db import db
from models.misc import WordTimestamp
from models.sf_videos import AspectRatioType, ResolutionType
from storage import gcs_client
from utils.lf_videos import generate_video_signed_read_url

# Load Env Vars
load_dotenv()
SFVIDEO_COLLECTION_NAME = os.environ["SFVIDEO_COLLECTION_NAME"]
GCS_ROOT_BUCKET_NAME = os.environ["GCS_ROOT_BUCKET_NAME"]
SIGNING_SERVICE_ACCOUNT_EMAIL = os.getenv("SIGNING_SERVICE_ACCOUNT_EMAIL")

# Resolve bundled static FFmpeg binary from imageio-ffmpeg once at module load
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

# Limit concurrent FFmpeg processes so the backend container never gets CPU-starved
_ffmpeg_semaphore = asyncio.Semaphore(2)


################################################################
# Helper: Fetch & Authorize SFVideo Doc
################################################################

def get_user_sf_video_doc(sf_id: str, uid: str) -> tuple[Any, dict[str, Any]]:
    """
    Fetches the `sf_videos` Firestore document and verifies it exists and belongs to `uid`.
    """
    try:
        doc_ref = db.collection(SFVIDEO_COLLECTION_NAME).document(sf_id)
        doc_snap = doc_ref.get()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {e}",
        )
    if not doc_snap.exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip record '{sf_id}' not found.",
        )

    sf_data = doc_snap.to_dict()
    if sf_data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip record '{sf_id}' not found.",
        )

    if sf_data.get("uid") != uid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access or modify this clip.",
        )
    return doc_ref, sf_data

################################################################
# Helper: Generate Signed GCS Attachment Download URL
################################################################

def sanitize_download_filename(title: str, suffix: str = "") -> str:
    """Builds a clean, filesystem-safe .mp4 filename for browser downloads."""
    cleaned = re.sub(r"[^a-zA-Z0-9_\-\s]", "", title or "clip").strip()
    cleaned = re.sub(r"\s+", "_", cleaned)[:50] or "clip"
    if suffix:
        return f"{cleaned}_{suffix}.mp4"
    return f"{cleaned}.mp4"


def gcs_blob_exists(gcs_uri: str) -> bool:
    """Checks whether a cached variant blob actually exists in GCS."""
    bucket_prefix = f"gs://{GCS_ROOT_BUCKET_NAME}/"
    if not gcs_uri or not gcs_uri.startswith(bucket_prefix):
        return False
    blob_path = gcs_uri.removeprefix(bucket_prefix)
    bucket = gcs_client.bucket(GCS_ROOT_BUCKET_NAME)
    return bucket.blob(blob_path).exists()


def generate_signed_download_url(
    gcs_uri: str,
    download_filename: str,
    expiration_minutes: int = 60,
) -> str:
    """
    Generates a v4 Signed GET URL with Content-Disposition: attachment
    so the browser immediately downloads the MP4 file instead of opening a tab.
    """
    bucket_prefix = f"gs://{GCS_ROOT_BUCKET_NAME}/"
    if not gcs_uri.startswith(bucket_prefix):
        raise ValueError("Invalid GCS URI")

    blob_path = gcs_uri.removeprefix(bucket_prefix)
    bucket = gcs_client.bucket(GCS_ROOT_BUCKET_NAME)
    blob = bucket.blob(blob_path)
    disposition = f'attachment; filename="{download_filename}"'

    credentials, _ = google_auth_default()
    if hasattr(credentials, "sign_bytes"):
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=expiration_minutes),
            method="GET",
            response_disposition=disposition,
            response_type="video/mp4",
        )

    auth_request = google_requests.Request()
    credentials.refresh(auth_request)
    sa_email = getattr(credentials, "service_account_email", None)
    if not sa_email or sa_email == "default":
        sa_email = SIGNING_SERVICE_ACCOUNT_EMAIL
    if not sa_email:
        raise RuntimeError(
            "SIGNING_SERVICE_ACCOUNT_EMAIL must be set in .env when running with user ADC credentials."
        )

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiration_minutes),
        method="GET",
        service_account_email=sa_email,
        access_token=credentials.token,
        response_disposition=disposition,
        response_type="video/mp4",
    )

################################################################
# Helper: Generate White + Yellow Active-Word .ass Subtitles
################################################################

def _format_ass_time(seconds: float) -> str:
    """Formats seconds into ASS timestamp format H:MM:SS.cc (centiseconds)."""
    total_cs = max(0, int(round(seconds * 100)))
    hrs = total_cs // 360000
    rem = total_cs % 360000
    mins = rem // 6000
    rem %= 6000
    secs = rem // 100
    cs = rem % 100
    return f"{hrs}:{mins:02d}:{secs:02d}.{cs:02d}"


def _escape_ass_word(text: str) -> str:
    """Strips ASS control characters so spoken words cannot break override tags."""
    return text.replace("\\", "").replace("{", "").replace("}", "").strip()


def _escape_ffmpeg_filter_path(path: str) -> str:
    """Escapes a file path for safe use inside an FFmpeg `ass=` filter string across Linux/macOS/Windows."""
    normalized = path.replace("\\", "/")
    normalized = normalized.replace(":", r"\:").replace("'", r"\'")
    return f"'{normalized}'"


def realign_transcript_words(
    existing_words: list[WordTimestamp],
    new_transcript_text: str,
    clip_start_sec: float,
    clip_end_sec: float,
) -> list[WordTimestamp]:
    """
    Realigns edited transcript text (typo fixes, rewording, or full foreign-language
    translation) onto the clip's word-level timestamps while strictly clamping every
    word within `[clip_start_sec, clip_end_sec]`.
    """
    if clip_start_sec < 0 or clip_end_sec <= clip_start_sec:
        raise ValueError(
            f"Invalid clip boundaries: start_sec ({clip_start_sec}) must be >= 0 and < end_sec ({clip_end_sec})."
        )

    new_tokens = [t for t in new_transcript_text.strip().split() if t]
    if not new_tokens:
        raise ValueError("Transcript text must contain at least one word.")

    def _distribute_span(
        tokens: list[str], span_start: float, span_end: float
    ) -> list[WordTimestamp]:
        """Splits `[span_start, span_end]` across `tokens` weighted by character length."""
        if not tokens:
            return []
        s = max(clip_start_sec, min(clip_end_sec, span_start))
        e = max(s + 0.04, min(clip_end_sec, span_end))
        total_dur = max(0.04, e - s)
        weights = [max(1, len(tok)) for tok in tokens]
        total_weight = sum(weights)

        out: list[WordTimestamp] = []
        cursor = s
        for idx, (tok, w) in enumerate(zip(tokens, weights)):
            if idx == len(tokens) - 1:
                w_end = e
            else:
                w_end = cursor + total_dur * (w / total_weight)
            out.append(
                WordTimestamp(
                    word=tok,
                    start_sec=round(cursor, 3),
                    end_sec=round(max(cursor + 0.02, w_end), 3),
                )
            )
            cursor = w_end
        return out

    # If no prior word timestamps exist, distribute evenly across the clip window
    if not existing_words:
        return _distribute_span(new_tokens, clip_start_sec, clip_end_sec)

    # Normalize existing words into clip-absolute [clip_start_sec, clip_end_sec] coordinates
    first_w_start = float(existing_words[0].start_sec)
    is_relative = first_w_start < clip_start_sec - 1.0
    norm_existing: list[WordTimestamp] = []
    for w in existing_words:
        ws = float(w.start_sec) + (clip_start_sec if is_relative else 0.0)
        we = float(w.end_sec) + (clip_start_sec if is_relative else 0.0)
        ws = max(clip_start_sec, min(clip_end_sec, ws))
        we = max(ws + 0.02, min(clip_end_sec, we))
        norm_existing.append(WordTimestamp(word=w.word, start_sec=ws, end_sec=we))

    def _clean(tok: str) -> str:
        return tok.lower().strip(".,!?;:\"'()[]{}")

    old_clean = [_clean(w.word) for w in norm_existing]
    new_clean = [_clean(t) for t in new_tokens]

    matcher = difflib.SequenceMatcher(None, old_clean, new_clean)
    aligned: list[WordTimestamp] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                src_w = norm_existing[i1 + k]
                aligned.append(
                    WordTimestamp(
                        word=new_tokens[j1 + k],
                        start_sec=src_w.start_sec,
                        end_sec=src_w.end_sec,
                    )
                )
        elif tag == "replace":
            span_start = norm_existing[i1].start_sec
            span_end = norm_existing[i2 - 1].end_sec
            aligned.extend(_distribute_span(new_tokens[j1:j2], span_start, span_end))
        elif tag == "insert":
            if i1 > 0 and i1 < len(norm_existing):
                anchor = norm_existing[i1 - 1].end_sec
                next_start = norm_existing[i1].start_sec
                span_start = max(clip_start_sec, anchor - 0.15)
                span_end = min(clip_end_sec, max(next_start, span_start + 0.25))
            elif i1 == 0:
                first_s = norm_existing[0].start_sec
                span_start = clip_start_sec
                span_end = max(clip_start_sec + 0.2, min(clip_end_sec, first_s))
            else:
                last_e = norm_existing[-1].end_sec
                span_start = min(last_e, clip_end_sec - 0.2)
                span_end = clip_end_sec
            aligned.extend(_distribute_span(new_tokens[j1:j2], span_start, span_end))
        elif tag == "delete":
            continue

    # Final safety pass: clamp strictly inside [clip_start_sec, clip_end_sec] & enforce monotonicity
    n = len(aligned)
    min_step = min(0.02, (clip_end_sec - clip_start_sec) / max(1, n * 2))
    prev_end = clip_start_sec

    for idx, w in enumerate(aligned):
        max_allowed_start = clip_end_sec - (n - idx) * min_step
        ws = max(clip_start_sec, prev_end, min(w.start_sec, max_allowed_start))
        max_allowed_end = clip_end_sec - (n - 1 - idx) * min_step
        we = max(ws + min_step, min(w.end_sec, max_allowed_end))
        w.start_sec = round(ws, 3)
        w.end_sec = round(we, 3)
        prev_end = w.end_sec

    return aligned


def validate_manual_words(
    words: list[WordTimestamp],
    clip_start_sec: float,
    clip_end_sec: float,
) -> list[WordTimestamp]:
    """
    Strictly validates manual word-by-word edits from [ ADVANCED ] word timing subtitle editing mode:
    1. Every word has non-empty text.
    2. clip_start_sec <= word.start_sec < word.end_sec <= clip_end_sec.
    3. Chronological non-overlapping order (word[i].start_sec >= word[i-1].end_sec).
    """
    if not words:
        raise ValueError("Subtitle word list cannot be empty.")

    eps = 0.015  # 15ms float rounding tolerance
    validated: list[WordTimestamp] = []
    prev_end = clip_start_sec

    for idx, w in enumerate(words):
        num = idx + 1
        text = w.word.strip()
        if not text:
            raise ValueError(f"Word #{num} cannot be empty.")

        ws = round(float(w.start_sec), 3)
        we = round(float(w.end_sec), 3)

        if ws < clip_start_sec - eps or we > clip_end_sec + eps:
            raise ValueError(
                f'Word #{num} ("{text}") time ({ws:.2f}s - {we:.2f}s) is outside '
                f"the clip window ({clip_start_sec:.2f}s - {clip_end_sec:.2f}s)."
            )

        ws = max(clip_start_sec, min(clip_end_sec, ws))
        we = max(clip_start_sec, min(clip_end_sec, we))

        if ws >= we:
            raise ValueError(
                f'Word #{num} ("{text}") start time ({ws:.2f}s) must be less than end time ({we:.2f}s).'
            )

        if idx > 0 and ws < prev_end - eps:
            raise ValueError(
                f'Word #{num} ("{text}") start time ({ws:.2f}s) overlaps with previous word end time ({prev_end:.2f}s).'
            )

        ws = max(prev_end, ws) if idx > 0 else ws
        if ws >= we:
            raise ValueError(
                f'Word #{num} ("{text}") has zero duration after aligning with previous word ({ws:.2f}s).'
            )

        validated.append(WordTimestamp(word=text, start_sec=round(ws, 3), end_sec=round(we, 3)))
        prev_end = round(we, 3)

    return validated


def generate_karaoke_ass_file(
    words: list[WordTimestamp],
    clip_start_sec: float,
    clip_duration_sec: float,
    output_ass_path: str,
    aspect_ratio: AspectRatioType,
    resolution: ResolutionType,
) -> None:
    """
    Generates an Advanced SubStation Alpha (.ass) subtitle file replicating the
    Chat preview's CC style: bold white text with a thick black outline where the
    currently spoken word highlights in Arcade Yellow (#FFF000 -> ASS &H0000F0FF&).
    """
    is_vertical = aspect_ratio in ("9:16_crop", "9:16_pad")
    if is_vertical:
        play_res_x, play_res_y = (1080, 1920) if resolution == "1080p" else (720, 1280)
        font_size = 62 if resolution == "1080p" else 42
        outline = 5 if resolution == "1080p" else 3
        margin_v = 260 if resolution == "1080p" else 175
        words_per_chunk = 4
    else:
        play_res_x, play_res_y = (1920, 1080) if resolution == "1080p" else (1280, 720)
        font_size = 54 if resolution == "1080p" else 36
        outline = 4 if resolution == "1080p" else 3
        margin_v = 85 if resolution == "1080p" else 55
        words_per_chunk = 6

    header = f"[Script Info]\nScriptType: v4.00+\nPlayResX: {play_res_x}\nPlayResY: {play_res_y}\nScaledBorderAndShadow: yes\nWrapStyle: 0\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: ArcadeCC,Sans,{font_size},&H00FFFFFF,&H0000F0FF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,{outline},2,2,40,40,{margin_v},1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"

    # Determine if stored word timestamps are absolute (in parent video) or already 0-based
    offset = (
        clip_start_sec
        if (words and words[0].start_sec >= (clip_start_sec - 0.5))
        else 0.0
    )

    dialogue_lines: list[str] = []
    for chunk_start in range(0, len(words), words_per_chunk):
        chunk = words[chunk_start : chunk_start + words_per_chunk]
        if not chunk:
            continue

        escaped_tokens = [_escape_ass_word(w.word) for w in chunk]

        for idx, w in enumerate(chunk):
            rel_start = max(0.0, w.start_sec - offset)
            rel_end = max(rel_start + 0.04, w.end_sec - offset)

            # Prevent 1-frame overlaps between consecutive words in the same chunk
            if idx + 1 < len(chunk):
                next_rel_start = max(0.0, chunk[idx + 1].start_sec - offset)
                if next_rel_start > rel_start:
                    rel_end = min(next_rel_start, rel_end + 0.35)

            if rel_start >= clip_duration_sec:
                continue
            rel_end = min(rel_end, clip_duration_sec)

            # Build line where active word `idx` is Yellow (&H0000F0FF&) and others are White (&H00FFFFFF&)
            styled_words: list[str] = []
            for j, token in enumerate(escaped_tokens):
                if not token:
                    continue
                if j == idx:
                    styled_words.append(r"{\c&H0000F0FF&}" + token + r"{\c&H00FFFFFF&}")
                else:
                    styled_words.append(token)

            line_text = " ".join(styled_words)
            start_str = _format_ass_time(rel_start)
            end_str = _format_ass_time(rel_end)
            dialogue_lines.append(
                f"Dialogue: 0,{start_str},{end_str},ArcadeCC,,0,0,0,,{line_text}"
            )

    with open(output_ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n" + "\n".join(dialogue_lines) + "\n")

################################################################
# Helper: Fast Async FFmpeg Renderer + GCS Upload
################################################################

async def render_sf_video_variant(
    uid: str,
    sf_id: str,
    source_gcs_uri: str,
    start_sec: float,
    end_sec: float,
    words: list[WordTimestamp],
    aspect_ratio: AspectRatioType,
    resolution: ResolutionType,
    subtitles: bool,
    variant_key: str,
) -> str:
    """
    Renders the requested (aspect_ratio, resolution, subtitles) clip variant using
    the bundled `imageio-ffmpeg` executable with fast HTTP Range input seeking,
    uploads the resulting MP4 to GCS, and returns its `gs://<bucket>/<blob_path>` URI.
    """
    signed_source_url = generate_video_signed_read_url(
        source_gcs_uri, expiration_minutes=30
    )
    duration_sec = max(0.5, round(end_sec - start_sec, 2))

    run_uid = uuid4().hex[:10]
    tmp_dir = tempfile.gettempdir()
    tmp_mp4_path = os.path.join(tmp_dir, f"sf_{sf_id}_{run_uid}.mp4")
    tmp_ass_path = os.path.join(tmp_dir, f"sf_{sf_id}_{run_uid}.ass")

    try:
        # Generate .ass subtitle file if requested and words exist
        use_subs = bool(subtitles and words)
        if use_subs:
            generate_karaoke_ass_file(
                words=words,
                clip_start_sec=start_sec,
                clip_duration_sec=duration_sec,
                output_ass_path=tmp_ass_path,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
            )

        ass_filter_arg = f"ass={_escape_ffmpeg_filter_path(tmp_ass_path)}"

        # Build FFmpeg filter args based on (aspect_ratio, resolution, subtitles)
        filter_args: list[str] = []

        if aspect_ratio == "original":
            vf_chain: list[str] = []
            if resolution == "720p":
                vf_chain.append("scale=-2:720")
            if use_subs:
                vf_chain.append(ass_filter_arg)
            if vf_chain:
                filter_args = ["-vf", ",".join(vf_chain)]

        elif aspect_ratio == "9:16_crop":
            target_w, target_h = (1080, 1920) if resolution == "1080p" else (720, 1280)
            vf_chain = [
                "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)'",
                f"scale={target_w}:{target_h}",
                "setsar=1",
            ]
            if use_subs:
                vf_chain.append(ass_filter_arg)
            filter_args = ["-vf", ",".join(vf_chain)]

        elif aspect_ratio == "9:16_pad":
            target_w, target_h = (1080, 1920) if resolution == "1080p" else (720, 1280)
            # Downscale background to 180x320 -> fast boxblur -> upscale to target_w:target_h,
            # then overlay crisp full-resolution foreground in the center
            complex_graph = (
                f"[0:v]split=2[bg][fg];"
                f"[bg]crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',"
                f"scale=180:320,boxblur=10:2,scale={target_w}:{target_h},setsar=1[bg_b];"
                f"[fg]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg_s];"
                f"[bg_b][fg_s]overlay=(W-w)/2:(H-h)/2"
            )
            if use_subs:
                complex_graph += f",{ass_filter_arg}[outv]"
            else:
                complex_graph += "[outv]"

            filter_args = [
                "-filter_complex",
                complex_graph,
                "-map",
                "[outv]",
                "-map",
                "0:a?",
            ]

        # Assemble full FFmpeg command using imageio-ffmpeg's bundled binary
        ffmpeg_cmd = [
            FFMPEG_EXE,
            "-y",
            "-ss",
            f"{start_sec:.2f}",
            "-to",
            f"{end_sec:.2f}",
            "-i",
            signed_source_url,
            *filter_args,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "21" if resolution == "1080p" else "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            tmp_mp4_path,
        ]

        # Execute FFmpeg asynchronously under concurrency semaphore
        async with _ffmpeg_semaphore:
            proc = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_bytes = await proc.communicate()

            if proc.returncode != 0 or not os.path.exists(tmp_mp4_path):
                err_tail = (stderr_bytes or b"").decode("utf-8", errors="replace")[-600:]
                raise RuntimeError(f"FFmpeg render failed: {err_tail}")

        # Upload rendered MP4 variant to GCS for permanent caching
        blob_path = f"{uid}/sf_videos/{sf_id}/{variant_key}.mp4"
        gcs_uri = f"gs://{GCS_ROOT_BUCKET_NAME}/{blob_path}"
        bucket = gcs_client.bucket(GCS_ROOT_BUCKET_NAME)
        blob = bucket.blob(blob_path)
        await asyncio.to_thread(
            blob.upload_from_filename,
            tmp_mp4_path,
            content_type="video/mp4",
        )
        return gcs_uri

    finally:
        # Clean up temporary files
        for p in (tmp_mp4_path, tmp_ass_path):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass