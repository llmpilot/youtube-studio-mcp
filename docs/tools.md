# Tools

The server exposes these MCP tools.

| Tool | Purpose |
| --- | --- |
| `youtube_auth_status` | Check whether local OAuth files exist. |
| `youtube_start_auth` | Return the auth helper command and OAuth details. |
| `youtube_channel_overview` | Fetch channel profile, branding, uploads playlist, and public statistics. |
| `youtube_list_videos` | List recent uploads with metadata, status, statistics, and pagination. |
| `youtube_get_video` | Fetch metadata for a single video. |
| `youtube_update_video` | Update title, description, tags, category, default language, or privacy status. `publish_at` (ISO 8601 with UTC offset) schedules publication; `""` removes a schedule. Also `made_for_kids`, `embeddable`, `license`, `public_stats_viewable`, `contains_synthetic_media`. |
| `youtube_upload_video` | Upload a local video file (resumable, 8 MiB chunks, retries). Default visibility `private`; optional `publish_at` schedules it; optional `made_for_kids`, `embeddable`, `license`, `public_stats_viewable`, `contains_synthetic_media`. Costs 1,600 quota units. |
| `youtube_delete_video` | Permanently delete a video (comments, statistics and captions go with it). Needs `confirm_title` equal to the current title; use `dry_run` first to see title, views and visibility (51 quota units). |
| `youtube_upload_thumbnail` | Upload a local image as a custom thumbnail. |
| `youtube_list_playlists` | List your playlists with privacy and item count (1 quota unit). |
| `youtube_list_playlist_items` | List a playlist's videos in order, with item ids and positions (1 unit). |
| `youtube_create_playlist` | Create a playlist, default `private` (50 quota units). |
| `youtube_update_playlist` | Change title, description, language or privacy; other fields are kept (50 units). |
| `youtube_delete_playlist`, `youtube_remove_playlist_item`, `youtube_delete_comment`, `youtube_moderate_comments`, `youtube_delete_video` | Delete a playlist; its videos stay on the channel (50 units). |
| `youtube_add_playlist_item` | Add a video to a playlist, optionally at a position (50 quota units). |
| `youtube_remove_playlist_item` | Remove an entry by playlist item id (not video id); the video is kept (50 units). |
| `youtube_move_playlist_item` | Move an entry to a new position; needs manual ordering (50 units). |
| `youtube_reply_to_comment` | Reply to a top-level comment (50 quota units). |
| `youtube_list_replies` | List the replies to a top-level comment (1 unit). |
| `youtube_delete_comment` | Permanently delete a comment or reply (50 units). |
| `youtube_moderate_comments` | Approve, hold or reject up to 50 comments in one call; `ban_author` with `rejected` (50 units). |
| `youtube_list_captions` | List a video's caption tracks (50 quota units). |
| `youtube_upload_caption` | Add a caption track from a local .srt/.vtt file (language, name, draft; 400 units). |
| `youtube_update_caption` | Replace a track's content and/or draft status (450 units). |
| `youtube_delete_caption` | Delete a caption track (50 units). |
| `youtube_download_caption` | Download a track as srt/vtt/sbv/scc/ttml, optionally saved to a file (200 units). |
| `youtube_analytics_query` | Free YouTube Analytics query: `start_date`, `end_date`, `metrics`, optional `dimensions`, `filters`, `sort`, `max_results`, `start_index`. Returns `columns` and compact `rows`. Own quota, no Data API units. |
| `youtube_channel_analytics` | Read channel-level analytics for a date range. |
| `youtube_video_analytics` | Read daily analytics for one video across a date range. |
| `youtube_post_comment` | Post a top-level comment on a video. |
| `youtube_list_comments` | List comment threads for a video with paging (`page_token`), `order`, `search_terms`, `moderation_status` and optional embedded replies. |
| `youtube_list_channel_comments` | Same for all videos of your channel, newest first; filter `heldForReview` or `likelySpam` to find comments to moderate (2 quota units). |

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
| `readOnlyHint: true` | `youtube_auth_status`, `youtube_start_auth`, `youtube_channel_overview`, `youtube_list_videos`, `youtube_get_video`, `youtube_list_captions`, `youtube_list_replies`, `youtube_list_channel_comments`, `youtube_analytics_query`, `youtube_list_playlists`, `youtube_list_playlist_items`, `youtube_channel_analytics`, `youtube_video_analytics`, `youtube_list_comments` |
| `destructiveHint: true` (overwrites existing data) | `youtube_update_video`, `youtube_upload_thumbnail`, `youtube_update_caption`, `youtube_delete_caption`, `youtube_update_playlist`, `youtube_delete_playlist` |
| additive writes (`destructiveHint: false`, not idempotent) | `youtube_upload_video`, `youtube_post_comment`, `youtube_upload_caption`, `youtube_create_playlist`, `youtube_reply_to_comment`, `youtube_add_playlist_item`, `youtube_move_playlist_item`, `youtube_download_caption` (may write a local file) |

New tools must be added to `_TOOL_HINTS` in `scripts/server.py`; a test fails otherwise.

## Errors

Failures inside a tool (API errors, bad input, missing files, network problems) come back as a normal
tool result with `isError: true` and a message that says what to do next, so the assistant can react
instead of seeing a protocol failure. Examples: exhausted quota (`quotaExceeded`) explains the daily
reset, an expired or revoked login (`invalid_grant`) points to `scripts/auth.py auth`, and 404s remind
that video, playlist, playlist item and caption ids are different. The original HTTP status, reason and
message always follow under `Details:`. Only protocol problems (unknown method, unknown tool) are
JSON-RPC errors.

## Quota

`youtube_quota_status` shows the estimated YouTube Data API units this server spent today, what is
left, when the quota resets (midnight Pacific Time, DST-aware without extra dependencies) and a
breakdown per call. Costs follow Google's table: reads 1, writes 50, caption upload 400, caption
update 450, caption download 200, search 100, video upload 1,600. The Analytics API has its own quota
and is not counted.

The counter is saved in `secrets/quota_usage.json` (next to the token) so restarts keep the total.
It is an estimate: other apps using the same Google Cloud project and failed calls are not included.
Settings: `YOUTUBE_DAILY_QUOTA` (default 10000, for example after a quota increase) and
`YOUTUBE_QUOTA_FILE`.

### Analytics examples

```text
Top 10 videos of the last 28 days:   dimensions [video], metrics [views, estimatedMinutesWatched], sort [-views], max_results 10
Where do viewers come from:          dimensions [insightTrafficSourceType], metrics [views]
Daily trend:                         dimensions [day], metrics [views, subscribersGained]
Countries:                           dimensions [country], metrics [views], sort [-views]
Retention curve of one video:        dimensions [elapsedVideoTimeRatio], metrics [audienceWatchRatio], filters "video==VIDEO_ID"
```

Metric, dimension and filter names are validated locally (letters and digits only); Google rejects
unsupported combinations with an explanation, which is passed through.

## Dry run

Every tool that changes something accepts `dry_run: true`. The server then validates the input, runs
the read-only lookups it needs and returns what it *would* do, without sending any write:

- `planned_requests`: method, endpoint, query and body of each write (uploads show file size and
  metadata, thumbnails and caption files only their size) plus the quota cost,
- `changes`: before/after for each changed field (`youtube_update_video`, `youtube_update_playlist`),
- `targets`: what the ids point to for deletes, moderation and playlist item removal (author and text of
  a comment, playlist title, ...), with a `not found` warning for unknown ids,
- `quota_cost_estimate`: units the real call would cost.

Nothing is sent, deleted or written to disk (`youtube_download_caption` does not create the file).
The lookups themselves cost their normal quota (usually 1 unit). Read-only tools ignore the flag, and
any value except `false`, `null`, `0` or an empty string counts as a dry run, so an unclear value never
executes a write. Without the flag nothing changes.

## Video status fields

`youtube_update_video` and `youtube_upload_video` accept these optional status fields; only the ones you
pass are changed:

| Argument | Meaning |
| --- | --- |
| `made_for_kids` | Self-declared "made for kids" (COPPA). YouTube may override it. |
| `embeddable` | Allow embedding on other sites. |
| `license` | `youtube` (standard) or `creativeCommon`. |
| `public_stats_viewable` | Show like and view counts publicly. |
| `contains_synthetic_media` | Disclose realistic altered or AI-generated content. |

Booleans must be real booleans: `"false"` as text is rejected instead of being read as true.
