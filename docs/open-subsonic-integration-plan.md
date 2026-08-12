# OpenSubsonic adapter and Symfonium offline sync

Status: implementation and production deployment completed on 2026-08-12.
Real-phone acceptance is in progress: a real player credential and Symfonium
provider exist, compatibility gate `5d3b6b2` is deployed, and initial sync must
now be repeated on the phone.

## Boundaries

- `/rest/*` is an additive, read-only FastAPI adapter over the existing catalog,
  ownership and local/Google Drive delivery layers.
- `/api/*`, the PWA, imports, matching and provider acquisition remain the control
  plane. No parallel media server or catalog is introduced.
- Shared `Track`/`File` rows do not grant shared rights. Every browse, search,
  playlist, artwork, stream and download query starts from the current user's own
  playlist items with a `READY` match and a currently playable file.
- OpenSubsonic credentials are separate per-device API keys. Web sessions,
  `APP_AUTH_TOKEN`, provider credentials and Google cookies are never reused.
- The adapter never edits a server playlist and never transcodes audio.

## Database contract

Alembic `0010_open_subsonic_players` adds `player_credentials`, stable UUID-backed
public IDs on artist/album/track/playlist, and playlist sync revision fields. Raw
API keys are returned once and only a domain-separated HMAC digest is stored.

Playlist `sync_revision` advances only when the canonical fingerprint of the
name or ordered visible READY entries changes. A fingerprint includes position,
public song ID, chosen file SHA-1/suffix/actual size/quality and visible metadata. Import,
matching, scanner and storage mutations reconcile all affected playlists in the
same transaction, once immediately before commit. Retagging updates existing
artist, album and song identities instead of replacing their public IDs.

## Protocol profile

The first profile exposes `ping`, `getLicense`, `getOpenSubsonicExtensions`,
`getMusicFolders`, empty `getStarred2`, `getBookmarks`, `getGenres`, `getArtists`,
`getArtist`, `getAlbum`, `getSong`,
`getAlbumList2`, `search3`, `getPlaylists`, `getPlaylist`, `stream`, `download`
and `getCoverArt`, with and without `.view`, in XML and JSON where applicable.

`getOpenSubsonicExtensions` is public and advertises only
`apiKeyAuthentication` version 1. Every other method requires `apiKey`; legacy
password and token/salt authentication are rejected. A foreign public ID and an
unknown public ID have the same protocol result.

The server advertises Subsonic API 1.16.1, accepts 1.13.x through 1.16.x, supports
empty and independently paged `search3`, preserves playlist order and duplicates,
marks every playlist `readonly`, and returns only original bytes with Range and
HEAD support. Metadata and binary delivery use the same local/Drive source resolver.

The operator-facing Symfonium setup and initial/add/remove/offline acceptance
sequence is documented in `player-sync-symfonium.md`. The server-side production
gate is complete; the real-phone gate is in progress and remains incomplete until
the initial/add/remove/offline/revoke sequence passes.

## Operations and rollback

Before production upgrade, create a PostgreSQL custom dump and prove it can be
restored. The migration backfills public IDs without changing existing primary
keys, file bytes, SHA-1 values, paths or Drive objects. No player key is created
automatically. Downgrade removes only adapter credentials and metadata; all
device keys must be recreated after a later re-upgrade.

The proxy must route `/rest/*` directly to FastAPI and omit the entire query from
access logs. Application diagnostics may record route templates, status and
parameter names, never parameter values, credentials or user email.
