# Local metadata and artwork

ZenStream accepts Kodi/Jellyfin-style XML NFO sidecars during library scans. An
NFO is indexed as a `metadata` media-file row and its normalized document is
stored under the `local` metadata provider. Local values have highest field
precedence; a configured provider supplies a value only when the NFO does not
contain one. Provider identities found in an NFO are retained alongside the
local identity and are used as normal matching hints.

## Sidecar placement

The scanner chooses the first matching file in the entity's directory, using
the following names before a deterministic directory-order fallback:

| Entity | Preferred sidecar names |
| --- | --- |
| Movie | `movie.nfo`, `<movie-name>.nfo` |
| Series | `tvshow.nfo`, `series.nfo`, `show.nfo` |
| Season | `season.nfo`, `<season-name>.nfo` |
| Episode | `episodedetails.nfo`, `episode.nfo`, `<media-name>.nfo` |
| Artist | `artist.nfo`, `<artist-name>.nfo` |
| Release | `album.nfo`, `release.nfo`, `<album-name>.nfo` |
| Track | `track.nfo`, `recording.nfo`, `<media-name>.nfo` |

Generic episode and track names are associated when their directory contains a
single media file. If a directory contains multiple episodes or tracks, use
the media-name form to make the association unambiguous.

Malformed or inaccessible NFO files are ignored. Files larger than 4 MiB are
ignored so metadata parsing remains bounded and cannot delay scanner admission.

## NFO examples

Save each example in the directory for the matching entity, using the preferred
sidecar name from the table above. Elements not needed by an entity can be
omitted.

Movie (`movie.nfo`):

```xml
<movie>
  <title>The Example</title>
  <originaltitle>Example Film</originaltitle>
  <plot>A locally maintained synopsis.</plot>
  <premiered>2026-04-12</premiered>
  <runtime>118</runtime>
  <genre>Science Fiction</genre>
  <actor><name>Alex Example</name><role>Rin</role></actor>
  <director>Sam Example</director>
  <uniqueid type="tmdb">12345</uniqueid>
  <uniqueid type="imdb">tt0123456</uniqueid>
</movie>
```

TV series (`tvshow.nfo`) and an episode (`episodedetails.nfo`):

```xml
<tvshow>
  <title>The Example Show</title>
  <plot>A locally maintained series synopsis.</plot>
  <status>Continuing</status>
  <premiered>2025-09-01</premiered>
  <genre>Drama</genre>
  <uniqueid type="tvdb">54321</uniqueid>
</tvshow>
```

```xml
<episodedetails>
  <title>The First Example</title>
  <season>1</season>
  <episode>2</episode>
  <plot>A locally maintained episode synopsis.</plot>
  <airdate>2025-09-08</airdate>
  <uniqueid type="tvdb">5432102</uniqueid>
</episodedetails>
```

Season artwork and metadata can be kept inside the season directory in
`season.nfo`:

```xml
<season>
  <title>Season One</title>
  <seasonnumber>1</seasonnumber>
  <plot>The first season.</plot>
</season>
```

Music artist (`artist.nfo`), release (`album.nfo`), and track (`track.nfo`):

```xml
<artist>
  <name>Example Artist</name>
  <genre>Electronic</genre>
  <musicbrainz_artistid>11111111-1111-1111-1111-111111111111</musicbrainz_artistid>
</artist>
```

```xml
<album>
  <album>Example Album</album>
  <albumartist>Example Artist</albumartist>
  <artist>Example Artist</artist>
  <releasedate>2026-02-20</releasedate>
  <label>Example Records</label>
  <musicbrainz_releaseid>22222222-2222-2222-2222-222222222222</musicbrainz_releaseid>
</album>
```

```xml
<track>
  <title>Example Track</title>
  <album>Example Album</album>
  <albumartist>Example Artist</albumartist>
  <artist>Example Artist</artist>
  <tracknumber>3</tracknumber>
  <discnumber>1</discnumber>
  <musicbrainz_recordingid>33333333-3333-3333-3333-333333333333</musicbrainz_recordingid>
</track>
```

MusicBrainz IDs are accepted only for the matching entity type. Remote image
URLs in an NFO are retained as metadata references but are not downloaded;
local image files are served only after scanner admission.

## Normalized fields

The parser accepts the common XML spellings used by movie, TV, and music NFOs.
Repeated values preserve file order and are de-duplicated case-insensitively.

| Catalog field | NFO elements |
| --- | --- |
| `title`, `originalTitle` | `title`, `name`, `album`, `originaltitle`, `originalname` |
| `overview`, `description` | `plot`, `description`, `summary`, `outline` |
| `date`, `releaseDate`, `firstAired`, `lastAired`, `year` | `releasedate`, `released`, `premiered`, `firstaired`, `lastaired`, `airdate`, `date`, `year` |
| `runtimeMinutes`, `durationSeconds` | `runtime`, `runtimeminutes`, `duration`, `durationseconds` |
| `seasonNumber`, `episodeNumber` | `seasonnumber`, `episodenumber`, `position` |
| `discNumber`, `trackNumber` | `discnumber`, `tracknumber` |
| `tags`, `genres` | `genre`, `tag`, `style`, `mood` |
| `studios`, `productionCompanies`, `networks` | `studio`, `productioncompany`, `company`, `network` |
| `status`, `airTime`, `originalCountry`, `originalLanguage` | same-named elements plus `country`, `language` |
| `communityRating`, `criticRating` | `communityrating`, `rating`, `userrating`, `criticrating`, `criticscore`, `metascore`, `tomatometer` |
| `people` | `actor`, `director`, `writer`, `producer`, `creator` |
| Music credits | `artist`, `albumartist`, `releaseartist` |
| Music release fields | `album`, `label`, `publisher`, `organization`, `albumtype`, `releasetype`, `secondarytype` |

Remote IDs are read from `<uniqueid type="...">` and the conventional
`tmdbid`, `tvdbid`, `imdbid`, and MusicBrainz `*id` elements. Video IDs are
typed for the owning entity; MusicBrainz IDs are constrained to the matching
artist, release, or recording identity types.

## Local artwork

Admitted image files are materialized into the authenticated local artwork
cache. The same matcher is used by the scanner, catalog projection, catalog
image route, and screen-extractor fallback.

| Artwork type | Conventional stems |
| --- | --- |
| Primary | `poster`, `folder`, `cover`, `primary`, `tvshow`, `movie`, `season`, `album`, `front`, `frontcover`, `thumb`, `thumbnail` |
| Backdrop | `backdrop`, `fanart`, `background`, `landscape` |
| Logo | `logo`, `clearlogo`, `clear-logo` |
| Banner | `banner` |

Names are case-insensitive, and trailing numeric variants such as
`poster-2.jpg` and `fanart_03.webp` are accepted. Movie, series, season,
episode, artist, release, and track entities can each own local artwork;
season artwork is read from the season directory, while music artists and
releases read their own directory-level artwork.

Local artwork takes precedence over provider artwork for the served image, and
the previous provider selection is retained as a fallback if the local file is
removed.
