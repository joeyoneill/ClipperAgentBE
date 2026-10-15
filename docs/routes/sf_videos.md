# Short-Form Video (Reel) Routes (`/api/sf_videos`)

**Source Files**:
- `routes/sf_videos/__init__.py`
- `routes/sf_videos/save.py`
- `routes/sf_videos/manage.py`
- Helpers & Models: `utils/sf_videos.py`, `models/sf_videos.py`

These endpoints power the **`REEL`** workflow: saving agent-proposed `ClipCandidate` cards from Chat, previewing clips inline, editing subtitles (in Simple Text or Advanced Word-Timing mode), and rendering/downloading MP4 variants on demand with FFmpeg.

---

## 1. Human-in-the-Loop Save Endpoint (`routes/sf_videos/save.py`)

### `POST /api/sf_videos/save`
Triggered when the user clicks **`[ ★ SAVE TO REEL ]`** on a `ClipCandidate` card in the Chat view. Saves the clip metadata and word-level timestamps in a lightweight `PENDING_CUT` state so zero FFmpeg compute is spent until the user exports a specific format.

- **Auth**: `Bearer <Firebase-JWT>`
- **Response Code**: `201 Created`
- **Idempotency**: Queries `sfvideos` for an existing document matching `(uid, candidate_id)`. If found, returns the existing `SFVideo` record immediately without creating a duplicate.

#### Request Body (`SaveClipRequest`)
```json
{
  "candidate_id": "c7a8b9d0-1234-5678-9abc-def012345678",
  "video_id": "8f9d2a1b3c4e5f6a",
  "session_id": "sess_98765",
  "title": "How to Dodge Waterfowl Dance",
  "start_sec": 31.0,
  "end_sec": 50.0,
  "transcript_text": "Watch out for the Waterfowl Dance flurry right here!",
  "words": [
    {"word": "Watch", "start_sec": 31.0, "end_sec": 31.6},
    {"word": "out", "start_sec": 31.7, "end_sec": 32.1}
  ],
  "rationale": "Opens on the high-energy warning hook and finishes cleanly after the dodge tip."
}
```

#### Error Responses
| Status | Condition |
| :---: | :--- |
| `400` | Parent `LFVideo` status is not `"SUCCESSFUL"`, or `start_sec < 0` / `end_sec <= start_sec`. |
| `403` / `404` | Parent `video_id` does not belong to user or does not exist. |
| `500` | Firestore write failure. |

---

## 2. Reel Directory, Subtitle Editor & Delete Endpoints (`routes/sf_videos/manage.py`)

### `GET /api/sf_videos/list`
Lists all saved short-form clips belonging to the authenticated user (`created_at` descending), batch-fetching all referenced parent `lfvideos` documents in a single Firestore RPC (`db.get_all`) to attach a fresh v4 signed `preview_url` to each clip.

- **Auth**: `Bearer <Firebase-JWT>`
- **Response Code**: `200 OK`
- **Response Body**: `list[SFVideoResponse]` (includes `preview_url` for `#t={start_sec},{end_sec}` inline playback).

---

### `PATCH /api/sf_videos/{sf_id}`
Updates a saved clip's `title` and/or edits its subtitles using one of two subtitle editing modes. Whenever subtitles are modified, any cached subtitle-burned MP4 variants (`*__subs_on`) are automatically deleted from GCS and removed from `rendered_variants` in Firestore so the next download renders the updated subtitles.

- **Auth**: `Bearer <Firebase-JWT>` (must own `sf_id`)
- **Response Code**: `200 OK`
- **Response Body**: Updated `SFVideoResponse`

#### Subtitle Editing Modes (`UpdateSFVideoRequest`)
1. **Mode A — Simple Paragraph Text (`transcript_text` provided, `words` omitted)**:
   - Runs `realign_transcript_words(existing_words, new_transcript_text, clip_start_sec, clip_end_sec)`.
   - Uses `difflib.SequenceMatcher` opcodes (`equal`, `replace`, `insert`, `delete`) and character-length weighting to realign typo fixes, rewordings, or full foreign-language translations onto monotonic timestamps strictly clamped within `[start_sec, end_sec]`.
2. **Mode B — Advanced Word-by-Word Timing (`words` list provided)**:
   - Runs `validate_manual_words(words, clip_start_sec, clip_end_sec)`.
   - Enforces non-empty word text, `clip_start_sec <= start_sec < end_sec <= clip_end_sec` (with `15ms` float tolerance), and non-overlapping chronological order (`word[i].start_sec >= word[i-1].end_sec`).

```json
{
  "title": "Updated Clip Title",
  "transcript_text": "Optional freeform paragraph edit for Mode A",
  "words": [
    {"word": "Optional", "start_sec": 31.0, "end_sec": 31.8},
    {"word": "ModeB", "start_sec": 31.9, "end_sec": 32.5}
  ]
}
```

#### Error Responses
| Status | Condition |
| :---: | :--- |
| `400` | Empty payload, blank `title`/`transcript_text`, or word timestamp boundary/overlap violation. |
| `403` / `404` | Clip not owned by user or not found. |

---

### `DELETE /api/sf_videos/{sf_id}`
Deletes a saved short-form clip and cleans up all cached MP4 variant blobs in GCS under `<uid>/sf_videos/<sf_id>/`.

- **Auth**: `Bearer <Firebase-JWT>` (must own `sf_id`)
- **Response Code**: `204 No Content`

---

## 3. On-Demand FFmpeg Export & Variant Caching (`POST /api/sf_videos/{sf_id}/download`)

Renders (or serves from cache) an MP4 file configured with the user's chosen aspect ratio, resolution, and burned-in karaoke subtitles, returning a GCS v4 signed download URL with `Content-Disposition: attachment`.

- **Auth**: `Bearer <Firebase-JWT>` (must own `sf_id`)
- **Response Code**: `200 OK`

#### Request Body (`DownloadSFVideoRequest`)
```json
{
  "aspect_ratio": "9:16_crop",
  "resolution": "1080p",
  "subtitles": true
}
```

| Parameter | Allowed Values | Description |
| :--- | :--- | :--- |
| `aspect_ratio` | `"original"`, `"9:16_crop"`, `"9:16_pad"` | `original` keeps 16:9 source aspect; `9:16_crop` center-crops to vertical; `9:16_pad` composites crisp foreground over a box-blurred vertical background. |
| `resolution` | `"1080p"`, `"720p"` | `1080p` Full HD (`1080x1920` / `1920x1080`, CRF `21`) vs. `720p` Fast (`720x1280` / `1280x720`, CRF `23`). |
| `subtitles` | `true`, `false` | When `true`, generates an `.ass` karaoke subtitle file (`generate_karaoke_ass_file`) with white text and active-word yellow (`{\c&H0000F0FF&}`) highlight burned into the video stream. |

#### Variant Caching Flow (`rendered_variants`)
1. Computes deterministic key: `variant_key = "{aspect_ratio}__{resolution}__{subs_on|subs_off}"`.
2. **Cache Hit (`cached: true`)**: If `rendered_variants[variant_key]` exists in Firestore and the blob exists in GCS (`gcs_blob_exists`), signs and returns the download URL immediately without running FFmpeg.
3. **Cache Miss (`cached: false`)**: Acquires `_ffmpeg_semaphore` (`asyncio.Semaphore(2)`), seeks the parent video via HTTP Range (`-ss {start_sec} -to {end_sec} -i <signed_source_url>`), encodes with `libx264 -preset ultrafast`, uploads `<uid>/sf_videos/<sf_id>/<variant_key>.mp4` to GCS, records the URI in `rendered_variants`, and returns the signed download URL.

#### Response Body (`DownloadSFVideoResponse`)
```json
{
  "download_url": "https://storage.googleapis.com/<bucket>/<uid>/sf_videos/<sf_id>/9:16_crop__1080p__subs_on.mp4?X-Goog-Algorithm=...",
  "filename": "How_to_Dodge_Waterfowl_Dance_9:16_crop_1080p_cc.mp4",
  "variant_key": "9:16_crop__1080p__subs_on",
  "cached": true
}
```
