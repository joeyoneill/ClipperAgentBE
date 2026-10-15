# tests/test_subtitle_math_and_sf_utils.py
# Unit tests for subtitle word-timing redistribution math (`realign_transcript_words`),
# manual word boundary validation (`validate_manual_words`), ASS karaoke subtitle
# generation (`generate_karaoke_ass_file`), and async FFmpeg rendering in `utils/sf_videos.py`.

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import HTTPException

from models.misc import WordTimestamp
from models.sf_videos import AspectRatioType
from tests.conftest import make_doc_snap, mock_db, mock_gcs
from utils.sf_videos import (
    _escape_ass_word,
    _escape_ffmpeg_filter_path,
    _format_ass_time,
    gcs_blob_exists,
    generate_karaoke_ass_file,
    generate_signed_download_url,
    get_user_sf_video_doc,
    realign_transcript_words,
    render_sf_video_variant,
    sanitize_download_filename,
    validate_manual_words,
)


################################################################
# 1. Subtitle Word-Timing Redistribution Math (`realign_transcript_words`)
################################################################


def test_realign_transcript_words_empty_existing_distributes_by_char_length() -> None:
    """When no prior word timestamps exist, distributes across [start, end] weighted by length."""
    realigned = realign_transcript_words(
        existing_words=[],
        new_transcript_text="Hi extraordinary world",
        clip_start_sec=10.0,
        clip_end_sec=20.0,
    )
    assert [w.word for w in realigned] == ["Hi", "extraordinary", "world"]
    assert realigned[0].start_sec >= 10.0
    assert realigned[-1].end_sec <= 20.0
    # Longer word ("extraordinary") should receive a longer duration than "Hi"
    dur_hi = realigned[0].end_sec - realigned[0].start_sec
    dur_long = realigned[1].end_sec - realigned[1].start_sec
    assert dur_long > dur_hi
    # Strictly monotonic non-overlapping
    for i in range(1, len(realigned)):
        assert realigned[i].start_sec >= realigned[i - 1].end_sec


def test_realign_transcript_words_equal_preserves_timestamps() -> None:
    """Punctuation/casing changes with identical cleaned tokens keep original timestamps."""
    existing = [
        WordTimestamp(word="hello", start_sec=30.0, end_sec=30.8),
        WordTimestamp(word="streamers", start_sec=31.0, end_sec=32.2),
        WordTimestamp(word="welcome", start_sec=32.5, end_sec=33.5),
    ]
    realigned = realign_transcript_words(
        existing_words=existing,
        new_transcript_text="Hello, Streamers! Welcome.",
        clip_start_sec=30.0,
        clip_end_sec=35.0,
    )
    assert [w.word for w in realigned] == ["Hello,", "Streamers!", "Welcome."]
    assert realigned[0].start_sec == 30.0
    assert realigned[0].end_sec == 30.8
    assert realigned[1].start_sec == 31.0
    assert realigned[1].end_sec == 32.2
    assert realigned[2].start_sec == 32.5
    assert realigned[2].end_sec == 33.5


def test_realign_transcript_words_replace_insert_delete_opcodes() -> None:
    """Tests SequenceMatcher replace, insert (start, middle, end), and delete branches."""
    existing = [
        WordTimestamp(word="we", start_sec=12.0, end_sec=12.4),
        WordTimestamp(word="won", start_sec=12.5, end_sec=13.0),
        WordTimestamp(word="the", start_sec=13.1, end_sec=13.4),
        WordTimestamp(word="match", start_sec=13.5, end_sec=14.2),
        WordTimestamp(word="today", start_sec=14.3, end_sec=14.8),
    ]
    new_text = "Yo we crushed all the big match easily!"
    realigned = realign_transcript_words(
        existing_words=existing,
        new_transcript_text=new_text,
        clip_start_sec=10.0,
        clip_end_sec=16.0,
    )
    assert [w.word for w in realigned] == new_text.split()
    for idx, w in enumerate(realigned):
        assert 10.0 <= w.start_sec < w.end_sec <= 16.0
        if idx > 0:
            assert w.start_sec >= realigned[idx - 1].end_sec


def test_realign_transcript_words_insert_at_end_branch() -> None:
    """Specifically exercises the `else` insert branch when tokens are appended at the end."""
    existing = [
        WordTimestamp(word="let", start_sec=20.0, end_sec=20.5),
        WordTimestamp(word="us", start_sec=20.6, end_sec=21.0),
    ]
    realigned = realign_transcript_words(
        existing_words=existing,
        new_transcript_text="let us celebrate right now",
        clip_start_sec=20.0,
        clip_end_sec=25.0,
    )
    assert len(realigned) == 5
    assert realigned[-1].end_sec <= 25.0
    for idx in range(1, len(realigned)):
        assert realigned[idx].start_sec >= realigned[idx - 1].end_sec


def test_realign_transcript_words_relative_timestamps_normalized() -> None:
    """When existing words are 0-based relative to a clip starting at 60s, shifts them into [60, 70]."""
    existing_relative = [
        WordTimestamp(word="first", start_sec=0.2, end_sec=0.8),
        WordTimestamp(word="second", start_sec=1.0, end_sec=1.8),
    ]
    realigned = realign_transcript_words(
        existing_words=existing_relative,
        new_transcript_text="first second",
        clip_start_sec=60.0,
        clip_end_sec=70.0,
    )
    assert realigned[0].start_sec == pytest.approx(60.2, abs=0.05)
    assert realigned[1].end_sec == pytest.approx(61.8, abs=0.05)


def test_realign_transcript_words_full_foreign_translation_clamping() -> None:
    """Full translation with zero token overlap redistributes cleanly within tight clip bounds."""
    existing = [
        WordTimestamp(word="Hello", start_sec=5.0, end_sec=5.5),
        WordTimestamp(word="friends", start_sec=5.6, end_sec=6.2),
    ]
    spanish = "Hola a todos mis amigos del canal bienvenidos al directo de hoy"
    realigned = realign_transcript_words(
        existing_words=existing,
        new_transcript_text=spanish,
        clip_start_sec=5.0,
        clip_end_sec=7.0,
    )
    assert len(realigned) == len(spanish.split())
    assert realigned[0].start_sec >= 5.0
    assert realigned[-1].end_sec <= 7.0
    for idx in range(len(realigned)):
        assert realigned[idx].start_sec < realigned[idx].end_sec
        if idx > 0:
            assert realigned[idx].start_sec >= realigned[idx - 1].end_sec


def test_realign_transcript_words_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError, match="Invalid clip boundaries"):
        realign_transcript_words([], "hello", clip_start_sec=-1.0, clip_end_sec=5.0)

    with pytest.raises(ValueError, match="Invalid clip boundaries"):
        realign_transcript_words([], "hello", clip_start_sec=10.0, clip_end_sec=10.0)

    with pytest.raises(ValueError, match="at least one word"):
        realign_transcript_words([], "   ", clip_start_sec=0.0, clip_end_sec=5.0)


################################################################
# 2. Manual Word Timing Validation (`validate_manual_words`)
################################################################


def test_validate_manual_words_valid_and_tolerance() -> None:
    words = [
        WordTimestamp(word="  Clean ", start_sec=9.99, end_sec=10.5),
        WordTimestamp(word="cut!", start_sec=10.5, end_sec=12.01),
    ]
    validated = validate_manual_words(words, clip_start_sec=10.0, clip_end_sec=12.0)
    assert validated[0].word == "Clean"
    assert validated[0].start_sec == 10.0
    assert validated[1].end_sec == 12.0


def test_validate_manual_words_error_cases() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_manual_words([], 0.0, 10.0)

    with pytest.raises(ValueError, match="Word #1 cannot be empty"):
        validate_manual_words(
            [WordTimestamp(word="   ", start_sec=1.0, end_sec=2.0)], 0.0, 10.0
        )

    with pytest.raises(ValueError, match="outside the clip window"):
        validate_manual_words(
            [WordTimestamp(word="early", start_sec=4.5, end_sec=6.0)], 5.0, 10.0
        )

    with pytest.raises(ValueError, match="must be less than end time"):
        validate_manual_words(
            [WordTimestamp(word="inverted", start_sec=7.0, end_sec=6.5)], 5.0, 10.0
        )

    with pytest.raises(ValueError, match="overlaps with previous word"):
        validate_manual_words(
            [
                WordTimestamp(word="one", start_sec=5.0, end_sec=7.0),
                WordTimestamp(word="two", start_sec=6.5, end_sec=8.0),
            ],
            5.0,
            10.0,
        )

    with pytest.raises(ValueError, match="zero duration"):
        validate_manual_words(
            [
                WordTimestamp(word="one", start_sec=5.0, end_sec=7.0),
                WordTimestamp(word="two", start_sec=6.99, end_sec=7.0),
            ],
            5.0,
            10.0,
        )


################################################################
# 3. ASS Karaoke Subtitle Formatting & Generation
################################################################


def test_ass_formatting_and_escaping_helpers() -> None:
    assert _format_ass_time(0.0) == "0:00:00.00"
    assert _format_ass_time(65.432) == "0:01:05.43"
    assert _format_ass_time(3661.05) == "1:01:01.05"
    assert _escape_ass_word(r"{\b1}hacked\word") == "b1hackedword"
    assert _escape_ffmpeg_filter_path(r"C:\tmp\my'sub:1.ass") == r"'C\:/tmp/my\'sub\:1.ass'"
    assert sanitize_download_filename("My Epic Clip!! #1", suffix="9:16_1080p") == (
        "My_Epic_Clip_1_9:16_1080p.mp4"
    )
    assert sanitize_download_filename("!!!") == "clip.mp4"


def test_generate_karaoke_ass_file_vertical_and_landscape(tmp_path: Path) -> None:
    words = [
        WordTimestamp(word=f"word{i}", start_sec=30.0 + i * 0.5, end_sec=30.4 + i * 0.5)
        for i in range(7)
    ]
    ass_vert = os.path.join(str(tmp_path), "vertical.ass")
    generate_karaoke_ass_file(
        words=words,
        clip_start_sec=30.0,
        clip_duration_sec=5.0,
        output_ass_path=ass_vert,
        aspect_ratio="9:16_crop",
        resolution="1080p",
    )
    content_vert = open(ass_vert, encoding="utf-8").read()
    assert "PlayResX: 1080" in content_vert
    assert "PlayResY: 1920" in content_vert
    assert r"{\c&H0000F0FF&}word0{\c&H00FFFFFF&}" in content_vert

    ass_land = os.path.join(str(tmp_path), "landscape.ass")
    generate_karaoke_ass_file(
        words=words,
        clip_start_sec=30.0,
        clip_duration_sec=5.0,
        output_ass_path=ass_land,
        aspect_ratio="original",
        resolution="720p",
    )
    content_land = open(ass_land, encoding="utf-8").read()
    assert "PlayResX: 1280" in content_land
    assert "PlayResY: 720" in content_land


################################################################
# 4. SFVideo Doc Authorization, GCS Signed URLs & FFmpeg Renderer
################################################################


def test_get_user_sf_video_doc_branches() -> None:
    doc_ref = MagicMock()
    doc_ref.get.return_value = make_doc_snap("sf_1", {"uid": "user_123", "title": "Clip"})
    mock_db.collection.return_value.document.return_value = doc_ref

    ref, data = get_user_sf_video_doc("sf_1", "user_123")
    assert ref is doc_ref
    assert data["title"] == "Clip"

    # 403 Forbidden
    with pytest.raises(HTTPException) as exc:
        get_user_sf_video_doc("sf_1", "other_user")
    assert exc.value.status_code == 403

    # 404 Not Found
    doc_ref.get.return_value = make_doc_snap("sf_1", None, exists=False)
    with pytest.raises(HTTPException) as exc:
        get_user_sf_video_doc("sf_1", "user_123")
    assert exc.value.status_code == 404

    # 500 DB Exception
    doc_ref.get.side_effect = RuntimeError("Firestore down")
    with pytest.raises(HTTPException) as exc:
        get_user_sf_video_doc("sf_1", "user_123")
    assert exc.value.status_code == 500


def test_gcs_blob_exists_and_generate_signed_download_url() -> None:
    assert gcs_blob_exists("gs://wrong-bucket/file.mp4") is False
    mock_blob = MagicMock()
    mock_blob.exists.return_value = True
    mock_blob.generate_signed_url.return_value = "https://signed-download.example.com/clip.mp4"
    mock_gcs.bucket.return_value.blob.return_value = mock_blob

    bucket = os.environ["GCS_ROOT_BUCKET_NAME"]
    uri = f"gs://{bucket}/user_123/sf_videos/sf_1/variant.mp4"
    assert gcs_blob_exists(uri) is True

    with pytest.raises(ValueError, match="Invalid GCS URI"):
        generate_signed_download_url("gs://bad/uri.mp4", "clip.mp4")

    # Path 1: Credentials with sign_bytes
    mock_creds = MagicMock()
    mock_creds.sign_bytes = MagicMock()
    with patch("utils.sf_videos.google_auth_default", return_value=(mock_creds, "proj")):
        url = generate_signed_download_url(uri, "clip.mp4")
        assert url == "https://signed-download.example.com/clip.mp4"

    # Path 2: ADC / Compute credentials without sign_bytes (uses IAM token refresh)
    mock_adc = MagicMock(spec=["refresh", "token", "service_account_email"])
    mock_adc.token = "ya29.tok"
    mock_adc.service_account_email = "default"
    with patch("utils.sf_videos.google_auth_default", return_value=(mock_adc, "proj")):
        url2 = generate_signed_download_url(uri, "clip.mp4")
        assert url2 == "https://signed-download.example.com/clip.mp4"
        mock_adc.refresh.assert_called_once()


@pytest.mark.asyncio
async def test_render_sf_video_variant_all_aspect_ratios_and_failure() -> None:
    words = [WordTimestamp(word="Test", start_sec=10.0, end_sec=11.0)]
    mock_blob = MagicMock()
    mock_gcs.bucket.return_value.blob.return_value = mock_blob

    async def fake_subprocess_exec(*cmd: str, **kwargs: object) -> MagicMock:
        out_path = cmd[-1]
        with open(out_path, "wb") as f:
            f.write(b"fake_mp4_bytes")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    bucket = os.environ["GCS_ROOT_BUCKET_NAME"]
    aspects: tuple[AspectRatioType, ...] = ("original", "9:16_crop", "9:16_pad")
    with (
        patch(
            "utils.sf_videos.generate_video_signed_read_url",
            return_value="https://signed-source.example.com/raw.mp4",
        ),
        patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess_exec) as mock_exec,
    ):
        for aspect in aspects:
            gcs_uri = await render_sf_video_variant(
                uid="user_123",
                sf_id="sf_1",
                source_gcs_uri=f"gs://{bucket}/user_123/lf_videos/v1/original.mp4",
                start_sec=10.0,
                end_sec=20.0,
                words=words,
                aspect_ratio=aspect,
                resolution="720p",
                subtitles=True,
                variant_key=f"{aspect}__720p__subs_on",
            )
            assert gcs_uri.startswith(f"gs://{bucket}/user_123/sf_videos/sf_1/")
        assert mock_exec.call_count == 3

    # Test FFmpeg non-zero exit code error handling
    async def failing_subprocess_exec(*cmd: str, **kwargs: object) -> MagicMock:
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"FFmpeg fatal error"))
        return proc

    with (
        patch(
            "utils.sf_videos.generate_video_signed_read_url",
            return_value="https://signed-source.example.com/raw.mp4",
        ),
        patch("asyncio.create_subprocess_exec", side_effect=failing_subprocess_exec),
    ):
        with pytest.raises(RuntimeError, match="FFmpeg render failed"):
            await render_sf_video_variant(
                uid="user_123",
                sf_id="sf_1",
                source_gcs_uri=f"gs://{bucket}/user_123/lf_videos/v1/original.mp4",
                start_sec=10.0,
                end_sec=20.0,
                words=[],
                aspect_ratio="original",
                resolution="1080p",
                subtitles=False,
                variant_key="original__1080p__subs_off",
            )
