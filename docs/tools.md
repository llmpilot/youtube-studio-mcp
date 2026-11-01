# Tools

The server exposes these MCP tools.

| Tool | Purpose |
| --- | --- |
| `youtube_auth_status` | Check whether local OAuth files exist. |
| `youtube_start_auth` | Return the auth helper command and OAuth details. |
| `youtube_channel_overview` | Fetch channel profile, branding, uploads playlist, and public statistics. |
| `youtube_list_videos` | List recent uploads with metadata, status, statistics, and pagination. |
| `youtube_get_video` | Fetch metadata for a single video. |
| `youtube_update_video` | Update title, description, tags, category, default language, or privacy status. `publish_at` (ISO 8601 with UTC offset) schedules publication; `""` removes a schedule. |
| `youtube_upload_video` | Upload a local video file (resumable, 8 MiB chunks, retries). Default visibility `private`; optional `publish_at` schedules it. Costs 1,600 quota units. |
| `youtube_upload_thumbnail` | Upload a local image as a custom thumbnail. |
| `youtube_channel_analytics` | Read channel-level analytics for a date range. |
| `youtube_video_analytics` | Read daily analytics for one video across a date range. |
| `youtube_post_comment` | Post a top-level comment on a video. |
| `youtube_list_comments` | List top-level comment threads for a video. |

## Example prompts

```text
Show my channel overview and the last 10 uploaded videos.
```

```text
Summarize my YouTube analytics for the last 28 days and suggest title or thumbnail improvements.
```

```text
Update video VIDEO_ID with this title, description, and tags.
```

```text
Upload /absolute/path/to/thumbnail.png as the thumbnail for VIDEO_ID.
```

## Tool annotations

Every tool carries MCP annotations so clients can auto-allow reads and confirm writes:

| Hint | Tools |
| --- | --- |
| `readOnlyHint: true` | `youtube_auth_status`, `youtube_start_auth`, `youtube_channel_overview`, `youtube_list_videos`, `youtube_get_video`, `youtube_channel_analytics`, `youtube_video_analytics`, `youtube_list_comments` |
| `destructiveHint: true` (overwrites existing data) | `youtube_update_video`, `youtube_upload_thumbnail` |
| additive writes (`destructiveHint: false`, not idempotent) | `youtube_upload_video`, `youtube_post_comment` |

New tools must be added to `_TOOL_HINTS` in `scripts/server.py`; a test fails otherwise.
