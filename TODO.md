# TODO – fehlende Features

Jeder Punkt wird als **eigener Commit** umgesetzt (Commit-Titel in Klammern). Reihenfolge = Priorität.
Stand: 0.1.0 plus Fix für zeilenweises JSON (stdio).

## Grundlagen (zuerst)

- [x] **Tests** – `tests/` mit `unittest`, HTTP und stdio gemockt; Protokolltest (`initialize`, `tools/list`, `tools/call`) in beiden Framings (`test: add protocol and tool tests`)
- [x] **Tool-Annotationen** – `readOnlyHint` / `destructiveHint` je Tool, damit Clients Schreibaktionen gezielt bestätigen lassen (`feat: add tool annotations`)
- [ ] **Fehler als Tool-Ergebnis** – API-Fehler mit `isError: true` statt JSON-RPC-Fehler; verständliche Meldung bei `quotaExceeded`, 401 und 403 (`fix: report API errors as tool results`)
- [ ] **Protokoll-Aushandlung** – `protocolVersion` des Clients zurückgeben statt fester Version; `notifications/cancelled` ignorieren (`fix: negotiate protocol version`)
- [ ] **Dry-Run für Schreibaktionen** – Parameter `dry_run`, zeigt die geplante Änderung als Vorher/Nachher (`feat: add dry_run to write tools`)

## Video-Verwaltung (Data API v3)

- [x] **Video hochladen** – `youtube_upload_video`, Upload mit Fortsetzung (resumable), Titel, Beschreibung, Tags, Sichtbarkeit (`feat: add video upload`)
- [x] **Geplante Veröffentlichung** – `publishAt` in `youtube_update_video` und beim Upload (Zeitzone, nur mit `private`) (`feat: add scheduled publishing`)
- [ ] **Weitere Video-Felder** – `madeForKids`, `embeddable`, `license`, `publicStatsViewable`, `containsSyntheticMedia` (`feat: expose more video status fields`)
- [ ] **Video löschen** – `youtube_delete_video`, nur mit Bestätigungsparameter (`feat: add video delete`)
- [ ] **Mehrere Videos abrufen** – `youtube_get_videos` mit ID-Liste (bis 50, ein API-Aufruf) (`feat: batch video lookup`)
- [ ] **Lokalisierungen** – Titel und Beschreibung in weiteren Sprachen (`localizations`) lesen und setzen (`feat: add video localizations`)
- [ ] **Kategorien** – `youtube_list_categories` je Region (`feat: list video categories`)

## Untertitel

- [x] **Untertitel verwalten** – `youtube_list_captions`, `youtube_upload_caption` (SRT/VTT, Deutsch), `youtube_update_caption`, `youtube_delete_caption`, `youtube_download_caption` (`feat: add caption tools`)

## Playlists

- [ ] **Playlists lesen** – `youtube_list_playlists`, `youtube_list_playlist_items` (`feat: list playlists`)
- [ ] **Playlists schreiben** – anlegen, Titel/Beschreibung/Sichtbarkeit ändern, löschen (`feat: manage playlists`)
- [ ] **Playlist-Einträge** – Video hinzufügen, entfernen, Reihenfolge ändern (`feat: manage playlist items`)

## Kommentare

- [ ] **Antworten und Moderation** – auf Kommentar antworten, Antworten lesen, Kommentar löschen, `setModerationStatus`, als Spam melden (`feat: comment replies and moderation`)
- [ ] **Kanalweite Kommentare und Paginierung** – `allThreadsRelatedToChannelId`, `page_token`, Suchbegriff, Sortierung (`feat: channel-wide comments with pagination`)

## Kanal

- [ ] **Kanal-Branding ändern** – Beschreibung, Schlüsselwörter, Standardsprache, Land, Banner (`feat: update channel branding`)
- [ ] **Suche** – `youtube_search` (eigene Videos, Kanäle, Konkurrenz-Recherche); Kontingent 100 pro Aufruf dokumentieren (`feat: add search`)
- [ ] **Abonnements** – eigene Abos und Abonnenten auflisten (`feat: list subscriptions`)
- [ ] **Wasserzeichen** – setzen und entfernen (`feat: add channel watermark`)
- [ ] **Livestreams (optional)** – Broadcasts und Streams anlegen, Status ändern (`feat: add live broadcasts`)

## Analytics

- [ ] **Freie Abfrage** – `youtube_analytics_query` mit `metrics`, `dimensions`, `filters`, `sort`, `maxResults` (bisher feste Kennzahlen) (`feat: generic analytics query`)
- [ ] **Fertige Berichte** – Top-Videos, Traffic-Quellen, Suchbegriffe, Geräte, Länder, Alter/Geschlecht (`feat: preset analytics reports`)
- [ ] **Zuschauerbindung** – `audienceRetention` je Video (Verlauf über die Laufzeit) (`feat: add audience retention`)
- [ ] **Impressionen und Klickrate** – Thumbnail-Impressions und CTR, soweit die API sie liefert (`feat: add impressions and CTR`)
- [ ] **Einnahmen (optional)** – Scope `yt-analytics-monetary.readonly`, nur auf Wunsch aktivieren (`feat: add revenue reports`)

## Betrieb und Doku

- [ ] **Kontingent-Zähler** – verbrauchte Einheiten je Sitzung mitzählen, Tool `youtube_quota_status` (`feat: track API quota usage`)
- [ ] **Scopes minimieren** – Anmeldung mit nur lesenden Rechten als Option (`--readonly`) (`feat: read-only auth mode`)
- [ ] **Paket installierbar** – Einstiegspunkt in `pyproject.toml` (`youtube-studio-mcp`), `pipx`/`uvx`-Hinweis (`build: add console entry point`)
- [ ] **Doku** – `docs/tools.md` und README je Feature ergänzen, Windows-Pfade und Claude-Code-Setup (`docs: update for new tools`)
