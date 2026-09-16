"""The curated OpenAPI contract for the ZenStream Orchestrator.

The request handlers intentionally continue to accept ``Request`` objects and
perform their existing tolerant parsing.  This module is documentation-only:
it supplies the public contract after FastAPI has discovered the routes and
never installs runtime validation or response filtering.
"""

from __future__ import annotations

import re
import warnings
from copy import deepcopy
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

SCHEMA_REF = "#/components/schemas/{model}"


class DocsModel(BaseModel):
    """A permissive schema base for the additive, forward-compatible API."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class FlexibleObject(DocsModel):
    """A deliberately open object used where providers add dynamic fields."""


class ErrorResponse(DocsModel):
    detail: Any = Field(
        default=None,
        description="A human-readable message or a structured error payload.",
    )


class ValidationErrorItem(DocsModel):
    type: str | None = Field(default=None, description="Validation error type.")
    loc: list[str | int] = Field(
        default_factory=list,
        description="Request location of the invalid value.",
    )
    msg: str | None = Field(default=None, description="Validation message.")
    input: Any = Field(default=None, description="The rejected input value.")


class ValidationErrorResponse(DocsModel):
    detail: list[ValidationErrorItem] = Field(
        default_factory=list,
        description="Validation failures returned by FastAPI.",
    )


class HealthResponse(DocsModel):
    status: str = Field(default="ok", examples=["ok"])


class VersionResponse(DocsModel):
    version: str | None = Field(default=None, examples=["1.5.1"])
    main: str | None = Field(default=None, examples=["1.5.1"])


class PublicConfigResponse(DocsModel):
    apiVersion: int | None = Field(default=None, examples=[2])
    catalog: bool | None = Field(default=None, examples=[True])
    playback: bool | None = Field(default=None, examples=[True])
    version: str | None = Field(default=None, examples=["1.5.1"])
    main: str | None = Field(default=None, examples=["1.5.1"])


class PublicWebUrlResponse(DocsModel):
    publicWebUrl: str = Field(default="", examples=[""])


class LanguageOption(DocsModel):
    code: str | None = Field(default=None, examples=["en-US"])
    name: str | None = Field(default=None, examples=["English (United States)"])
    nativeName: str | None = Field(default=None, examples=["English (United States)"])


class LanguagesResponse(DocsModel):
    languages: list[Any] = Field(default_factory=list)
    languageOptions: list[LanguageOption] = Field(default_factory=list)


class MetadataLanguagesResponse(DocsModel):
    languages: list[str] = Field(default_factory=list, examples=[["en", "ja"]])


class DeviceMetadata(DocsModel):
    deviceId: str | None = Field(default=None, examples=["web-demo"])
    deviceName: str | None = Field(default=None, examples=["Browser"])
    client: str | None = Field(default=None, examples=["web"])
    clientVersion: str | None = Field(default=None, examples=["1.5.1"])
    platform: str | None = Field(default=None, examples=["Windows"])


class CredentialsRequest(DocsModel):
    username: str = Field(
        default="", description="Account name.", examples=["example-user"]
    )
    password: str = Field(
        default="",
        description="Account password. Never log or persist this value.",
        json_schema_extra={"writeOnly": True},
    )
    device: DeviceMetadata | None = Field(
        default=None, description="Optional client/device metadata."
    )
    deviceId: str | None = Field(default=None, examples=["web-demo"])


class RegisterRequest(CredentialsRequest):
    invite: str = Field(
        default="",
        description="Invitation code supplied by an administrator.",
        examples=["invite-example"],
    )


class PasswordChangeRequest(DocsModel):
    currentPassword: str = Field(
        default="",
        json_schema_extra={"writeOnly": True},
        description="The current password.",
    )
    newPassword: str = Field(
        default="",
        json_schema_extra={"writeOnly": True},
        description="The replacement password.",
    )
    confirmNewPassword: str = Field(
        default="",
        json_schema_extra={"writeOnly": True},
        description="The replacement password repeated for confirmation.",
    )


class PasswordResetRequest(DocsModel):
    password: str = Field(
        default="",
        json_schema_extra={"writeOnly": True},
        description="Replacement password for the selected user.",
    )


class SessionResponse(DocsModel):
    token: str | None = Field(
        default=None,
        description="Bearer session token returned to non-browser clients.",
        json_schema_extra={"readOnly": True},
    )
    expiresIn: int | None = Field(
        default=None, description="Session lifetime in seconds."
    )
    user: User | None = None


class User(DocsModel):
    id: str | None = Field(default=None, examples=["user-0001"])
    username: str | None = Field(default=None, examples=["example-user"])
    disabled: bool | None = Field(default=None, examples=[False])
    avatarVersion: str | None = Field(default=None, examples=["avatar-1"])
    libraries: list[str] = Field(default_factory=list)


class UserResponse(DocsModel):
    user: User | None = None


class BootstrapResponse(DocsModel):
    user: User | None = None
    resourceTicket: str | None = Field(
        default=None, json_schema_extra={"readOnly": True}
    )
    resourceTicketExpiresIn: int | None = None
    artworkTicket: str | None = Field(
        default=None, json_schema_extra={"readOnly": True}
    )
    artworkTicketExpiresIn: int | None = None
    locale: str | None = Field(default=None, examples=["en-US"])
    metadataLanguage: str | None = Field(default=None, examples=["en"])
    languages: list[Any] = Field(default_factory=list)
    languageOptions: list[LanguageOption] = Field(default_factory=list)


class TicketResponse(DocsModel):
    ticket: str = Field(
        default="ticket-example",
        description="Short-lived ticket for the requested capability.",
        json_schema_extra={"readOnly": True},
    )
    expiresIn: int = Field(default=60, description="Ticket lifetime in seconds.")


class AvatarVersionResponse(DocsModel):
    avatarVersion: str | None = Field(default=None, examples=["avatar-1"])


class LocalePatchRequest(DocsModel):
    locale: str = Field(default="en-US", examples=["en-US"])


class MetadataLanguagePatchRequest(DocsModel):
    language: str = Field(default="en", examples=["en"])


class MetadataLanguageResponse(DocsModel):
    mode: str | None = Field(default=None, examples=["auto"])
    language: str | None = Field(default=None, examples=["en"])


class TrackLanguageOption(DocsModel):
    value: str | None = Field(default=None, examples=["en"])
    label: str | None = Field(default=None, examples=["English"])


class PlaybackPreferences(DocsModel):
    audioLanguage: str | None = Field(default=None, examples=["en"])
    subtitleLanguage: str | None = Field(default=None, examples=["off"])
    audioLanguages: list[TrackLanguageOption] = Field(default_factory=list)
    subtitleLanguages: list[TrackLanguageOption] = Field(default_factory=list)


class WatchHistoryPreferences(DocsModel):
    enabled: bool | None = Field(default=None, examples=[True])


class CatalogArtwork(DocsModel):
    url: str | None = Field(
        default=None, examples=["/api/catalog/items/item-0001/images/Primary"]
    )
    blurHash: str | None = Field(
        default=None, examples=["LEHV6nWB2yk8pyo0adR*.7kCMdnj"]
    )
    width: int | None = Field(default=None, examples=[1280])
    height: int | None = Field(default=None, examples=[720])
    language: str | None = Field(default=None, examples=["en"])


class CatalogCredit(DocsModel):
    id: str | None = Field(default=None, examples=["person-0001"])
    name: str | None = Field(default=None, examples=["Example Person"])
    role: str | None = Field(default=None, examples=["Director"])
    character: str | None = Field(default=None, examples=["Lead"])
    image: CatalogArtwork | None = None


class CatalogMetadata(DocsModel):
    title: str | None = Field(default=None, examples=["Example Feature"])
    originalTitle: str | None = Field(default=None, examples=["Example Feature"])
    overview: str | None = Field(
        default=None, examples=["A credential-free example catalog item."]
    )
    date: str | None = Field(default=None, examples=["2025-01-15"])
    year: int | None = Field(default=None, examples=[2025])
    runtimeMinutes: int | None = Field(default=None, examples=[104])
    communityRating: float | None = Field(default=None, examples=[8.2])
    officialRating: str | None = Field(default=None, examples=["PG-13"])
    genres: list[str] = Field(default_factory=list, examples=[["Drama"]])
    tags: list[str] = Field(default_factory=list)
    album: str | None = Field(default=None, examples=["Example Album"])
    albumArtist: str | None = Field(default=None, examples=["Example Artist"])
    albumType: str | None = Field(default=None, examples=["Album"])
    albumSecondaryTypes: list[str] = Field(default_factory=list)
    label: str | None = Field(default=None, examples=["Example Records"])


class CatalogState(DocsModel):
    played: bool | None = Field(default=None, examples=[False])
    favorite: bool | None = Field(default=None, examples=[True])
    following: bool | None = Field(default=None, examples=[False])
    progress: float | None = Field(default=None, examples=[0.35])
    position: float | None = Field(default=None, examples=[120.5])
    duration: float | None = Field(default=None, examples=[3600.0])


class CatalogItem(DocsModel):
    id: str | None = Field(default=None, examples=["item-0001"])
    type: str | None = Field(default=None, examples=["movie"])
    title: str | None = Field(default=None, examples=["Example Feature"])
    parentId: str | None = Field(default=None, examples=["series-0001"])
    libraryId: str | None = Field(default=None, examples=["library-0001"])
    metadata: CatalogMetadata | None = None
    images: dict[str, CatalogArtwork] = Field(default_factory=dict)
    credits: list[CatalogCredit] = Field(default_factory=list)
    state: CatalogState | None = None
    children: list[Any] = Field(default_factory=list)


class CatalogPage(DocsModel):
    items: list[CatalogItem] = Field(default_factory=list)
    page: int | None = Field(default=None, examples=[1])
    pageSize: int | None = Field(default=None, examples=[40])
    total: int | None = Field(default=None, examples=[1])
    hasNext: bool | None = Field(default=None, examples=[False])
    totalPages: int | None = Field(default=None, examples=[1])
    facets: dict[str, int] = Field(
        default_factory=dict, examples=[{"all": 1, "movie": 1}]
    )
    libraryRows: list[Any] = Field(default_factory=list)
    sections: list[Any] = Field(default_factory=list)


class CatalogLibrary(DocsModel):
    id: str | None = Field(default=None, examples=["library-0001"])
    name: str | None = Field(default=None, examples=["Movies"])
    type: str | None = Field(default=None, examples=["movies"])
    sortOrder: int | None = Field(default=None, examples=[0])
    scanState: str | None = Field(default=None, examples=["ready"])
    lastScanFinishedAt: str | None = Field(
        default=None, examples=["2025-01-15T12:00:00Z"]
    )
    supportsLastAdded: bool | None = Field(default=None, examples=[True])
    catalogGeneration: int | None = Field(default=None, examples=[12])


class CatalogLibrariesResponse(DocsModel):
    libraries: list[CatalogLibrary] = Field(default_factory=list)
    initialPage: list[CatalogPage] = Field(default_factory=list)


class CatalogItemResponse(DocsModel):
    item: CatalogItem | None = None
    data: CatalogItem | None = None
    detail: Any = None


class CatalogStatusResponse(DocsModel):
    state: str | None = Field(default=None, examples=["ready"])
    generation: int | None = Field(default=None, examples=[12])
    updatedAt: str | None = Field(default=None, examples=["2025-01-15T12:00:00Z"])
    libraries: list[CatalogLibraryStatus] = Field(default_factory=list)


class CatalogLibraryStatus(DocsModel):
    id: str | None = Field(default=None, examples=["library-0001"])
    scanState: str | None = Field(default=None, examples=["ready"])
    lastScanFinishedAt: str | None = Field(
        default=None, examples=["2025-01-15T12:00:00Z"]
    )
    catalogGeneration: int | None = Field(default=None, examples=[12])
    lastRootEntityId: str | None = Field(default=None, examples=["entity-0001"])


class InviteValidationResponse(DocsModel):
    valid: bool = Field(default=True, examples=[True])


class NotificationPatchRequest(DocsModel):
    read: bool = Field(default=True, examples=[True])


class AdminUserUpdateRequest(DocsModel):
    disabled: bool = Field(default=False, examples=[False])


class MatchesResponse(DocsModel):
    query: str | None = Field(default=None, examples=["example"])
    matches: list[Any] = Field(default_factory=list)


class IntroOutroInspection(DocsModel):
    sourceId: str | None = Field(default=None, examples=["source-0001"])
    durationSeconds: float | None = Field(default=None, examples=[3600.0])
    state: str | None = Field(default=None, examples=["ready"])
    error: str | None = Field(default=None)
    updatedAt: str | None = Field(default=None, examples=["2025-01-15T12:00:00Z"])
    segments: list[Any] = Field(default_factory=list)
    fingerprints: list[Any] = Field(default_factory=list)


class ProviderTestResponse(DocsModel):
    ok: bool = Field(default=True, examples=[True])


class RefreshQueueResponse(DocsModel):
    backfill: Any = Field(default=None)


class MetadataResponse(DocsModel):
    metadata: CatalogMetadata | None = None
    languages: list[Any] = Field(default_factory=list)
    providers: dict[str, Any] = Field(default_factory=dict)


class CatalogStatePatchRequest(DocsModel):
    played: bool | None = Field(default=None, examples=[True])
    favorite: bool | None = Field(default=None, examples=[False])
    following: bool | None = Field(default=None, examples=[True])


class ProgressPatchRequest(DocsModel):
    position: float | None = Field(default=None, examples=[120.5])
    duration: float | None = Field(default=None, examples=[3600.0])
    watched: bool | None = Field(default=None, examples=[False])


class PlayStartRequest(DocsModel):
    sourceId: str | None = Field(default=None, examples=["source-0001"])
    position: float | None = Field(default=None, examples=[0])


class MusicTrack(DocsModel):
    id: str | None = Field(default=None, examples=["track-0001"])
    title: str | None = Field(default=None, examples=["Example Track"])
    trackNumber: int | None = Field(default=None, examples=[1])
    discNumber: int | None = Field(default=None, examples=[1])
    durationSeconds: float | None = Field(default=None, examples=[215.2])
    artists: list[Any] = Field(default_factory=list)
    credits: list[CatalogCredit] = Field(default_factory=list)


class MusicRelease(DocsModel):
    id: str | None = Field(default=None, examples=["release-0001"])
    title: str | None = Field(default=None, examples=["Example Album"])
    albumArtist: str | None = Field(default=None, examples=["Example Artist"])
    artists: list[Any] = Field(default_factory=list)
    releaseType: str | None = Field(default=None, examples=["Album"])
    secondaryTypes: list[str] = Field(default_factory=list)
    artwork: dict[str, CatalogArtwork] = Field(default_factory=dict)
    tracks: list[MusicTrack] = Field(default_factory=list)


class MusicAlbumPage(DocsModel):
    items: list[MusicRelease] = Field(default_factory=list)
    page: int | None = Field(default=None, examples=[1])
    pageSize: int | None = Field(default=None, examples=[40])
    total: int | None = Field(default=None, examples=[1])
    hasNext: bool | None = Field(default=None, examples=[False])


class MusicAlbumResponse(DocsModel):
    album: MusicRelease | None = None
    tracks: list[MusicTrack] = Field(default_factory=list)


class MusicArtist(DocsModel):
    id: str | None = Field(default=None, examples=["artist-0001"])
    name: str | None = Field(default=None, examples=["Example Artist"])
    sortName: str | None = Field(default=None, examples=["Artist, Example"])
    musicBrainzId: str | None = Field(default=None, examples=["mbid-example"])
    releases: list[MusicRelease] = Field(default_factory=list)
    tracks: list[MusicTrack] = Field(default_factory=list)
    trackCount: int | None = Field(default=None, examples=[4])
    creditedArtists: list[Any] = Field(default_factory=list)


class MusicArtistResponse(DocsModel):
    artist: MusicArtist | None = None
    albums: list[MusicRelease] = Field(default_factory=list)
    tracks: list[MusicTrack] = Field(default_factory=list)


class MusicTracksResponse(DocsModel):
    tracks: list[MusicTrack] = Field(default_factory=list)
    page: int | None = Field(default=None, examples=[1])
    pageSize: int | None = Field(default=None, examples=[40])
    total: int | None = Field(default=None, examples=[4])


class PlaybackCapabilityRequest(DocsModel):
    sourceId: str | None = Field(default=None, examples=["source-0001"])
    device: DeviceMetadata | None = None
    playerEngine: str | None = Field(default=None, examples=["media3"])
    directPlay: bool | None = Field(default=None, examples=[True])
    directStream: bool | None = Field(default=None, examples=[True])
    audioTrack: int | None = Field(default=None, examples=[0])
    subtitleTrack: int | None = Field(default=None, examples=[-1])


class MediaSource(DocsModel):
    id: str | None = Field(default=None, examples=["source-0001"])
    sourceId: str | None = Field(default=None, examples=["source-0001"])
    url: str | None = Field(
        default=None, examples=["/api/playback/items/item-0001/stream"]
    )
    mediaType: str | None = Field(default=None, examples=["video/mp4"])
    container: str | None = Field(default=None, examples=["mp4"])
    duration: float | None = Field(default=None, examples=[3600.0])


class PlaybackNegotiationResponse(DocsModel):
    sessionId: str | None = Field(default=None, examples=["session-0001"])
    viewerId: str | None = Field(default=None, examples=["viewer-0001"])
    sourceId: str | None = Field(default=None, examples=["source-0001"])
    mediaType: str | None = Field(default=None, examples=["video/mp4"])
    sources: list[MediaSource] = Field(default_factory=list)
    url: str | None = Field(
        default=None, examples=["/api/playback/items/item-0001/stream"]
    )


class PlaybackAccessRequest(DocsModel):
    sourceId: str | None = Field(default=None, examples=["source-0001"])
    sessionId: str | None = Field(default=None, examples=["session-0001"])


class PlaybackAccessResponse(DocsModel):
    sessionId: str | None = Field(default=None, examples=["session-0001"])
    sourceId: str | None = Field(default=None, examples=["source-0001"])
    access: str | None = Field(default=None, json_schema_extra={"readOnly": True})
    expiresIn: int | None = Field(default=None, examples=[900])


class ViewerHeartbeatRequest(DocsModel):
    position: float | None = Field(default=None, examples=[120.5])
    duration: float | None = Field(default=None, examples=[3600.0])
    playing: bool | None = Field(default=None, examples=[True])
    paused: bool | None = Field(default=None, examples=[False])


class PlaybackViewer(DocsModel):
    id: str | None = Field(default=None, examples=["viewer-0001"])
    sessionId: str | None = Field(default=None, examples=["session-0001"])
    userId: str | None = Field(default=None, examples=["user-0001"])
    itemId: str | None = Field(default=None, examples=["item-0001"])
    position: float | None = Field(default=None, examples=[120.5])
    duration: float | None = Field(default=None, examples=[3600.0])
    playing: bool | None = Field(default=None, examples=[True])


class PlaybackSourceResponse(DocsModel):
    sources: list[MediaSource] = Field(default_factory=list)
    source: MediaSource | None = None


class TrickplayManifest(DocsModel):
    state: str | None = Field(default=None, examples=["ready"])
    generation: str | None = Field(default=None, examples=["generation-1"])
    intervalSeconds: float | None = Field(default=None, examples=[10])
    tileWidth: int | None = Field(default=None, examples=[320])
    tileHeight: int | None = Field(default=None, examples=[180])
    columns: int | None = Field(default=None, examples=[5])
    rows: int | None = Field(default=None, examples=[5])
    sheets: list[Any] = Field(default_factory=list)


class PlaybackSegmentsResponse(DocsModel):
    segments: list[Any] = Field(default_factory=list)


class PlaybackSessionResponse(DocsModel):
    sessionId: str | None = Field(default=None, examples=["session-0001"])
    sessionState: str | None = Field(default=None, examples=["running"])
    status: str | None = Field(default=None, examples=["ready"])
    progress: float | None = Field(default=None, examples=[0.5])


class LyricsResponse(DocsModel):
    lyrics: str | None = Field(default=None, examples=["[00:01.00] Example lyric"])
    format: str | None = Field(default=None, examples=["lrc"])
    lines: list[Any] = Field(default_factory=list)


class NotificationThumbnail(DocsModel):
    url: str | None = Field(
        default=None, examples=["/api/catalog/items/item-0001/images/Primary"]
    )
    blurHash: str | None = Field(
        default=None, examples=["LEHV6nWB2yk8pyo0adR*.7kCMdnj"]
    )


class Notification(DocsModel):
    id: str | None = Field(default=None, examples=["notification-0001"])
    type: str | None = Field(default=None, examples=["new_release"])
    itemId: str | None = Field(default=None, examples=["release-0001"])
    artistId: str | None = Field(default=None, examples=["artist-0001"])
    title: str | None = Field(default=None, examples=["New release available"])
    message: str | None = Field(
        default=None, examples=["Example Artist released Example Album."]
    )
    createdAt: str | None = Field(default=None, examples=["2025-01-15T12:00:00Z"])
    read: bool | None = Field(default=None, examples=[False])
    thumbnail: NotificationThumbnail | None = None


class NotificationPage(DocsModel):
    notifications: list[Notification] = Field(default_factory=list)
    nextCursor: str | None = Field(default=None, examples=["cursor-next"])
    hasMore: bool | None = Field(default=None, examples=[False])


class NotificationSummary(DocsModel):
    unread: int | None = Field(default=None, examples=[2])
    total: int | None = Field(default=None, examples=[5])


class NotificationMutationResponse(DocsModel):
    notification: Notification | None = None
    updated: bool | None = Field(default=None, examples=[True])


class CalendarEvent(DocsModel):
    id: str | None = Field(default=None, examples=["event-0001"])
    title: str | None = Field(default=None, examples=["Example Feature"])
    type: str | None = Field(default=None, examples=["movie"])
    libraryId: str | None = Field(default=None, examples=["library-0001"])
    airDate: str | None = Field(default=None, examples=["2025-01-15"])
    releaseDate: str | None = Field(default=None, examples=["2025-01-15"])
    hasFile: bool | None = Field(default=None, examples=[False])
    following: bool | None = Field(default=None, examples=[True])
    metadata: CatalogMetadata | None = None


class CalendarPage(DocsModel):
    events: list[CalendarEvent] = Field(default_factory=list)
    start: str | None = Field(default=None, examples=["2025-01-01"])
    end: str | None = Field(default=None, examples=["2025-02-01"])


class CalendarSettings(DocsModel):
    enabled: bool | None = Field(default=None, examples=[True])
    connections: list[Any] = Field(default_factory=list)


class CalendarFollowRequest(DocsModel):
    following: bool = Field(default=False, examples=[True])


class BazarrSettings(DocsModel):
    enabled: bool | None = Field(default=None, examples=[True])
    url: str | None = Field(default=None, examples=["http://bazarr.example.invalid"])
    apiKey: str | None = Field(
        default=None,
        description="Encrypted provider credential; write-only in configuration requests.",
        json_schema_extra={"writeOnly": True},
    )
    mappings: list[Any] = Field(default_factory=list)


class BazarrStatus(DocsModel):
    state: str | None = Field(default=None, examples=["matched"])
    sourceId: str | None = Field(default=None, examples=["source-0001"])
    available: bool | None = Field(default=None, examples=[True])
    matches: list[Any] = Field(default_factory=list)


class BazarrSearchRequest(DocsModel):
    sourceId: str = Field(default="source-0001", examples=["source-0001"])
    languages: list[str] = Field(default_factory=list, examples=[["eng"]])


class BazarrDownloadRequest(DocsModel):
    sourceId: str = Field(default="source-0001", examples=["source-0001"])
    matchId: str = Field(default="match-0001", examples=["match-0001"])


class BazarrSearchResponse(DocsModel):
    state: str | None = Field(default=None, examples=["ready"])
    matches: list[Any] = Field(default_factory=list)


class BazarrDownloadResponse(DocsModel):
    state: str | None = Field(default=None, examples=["download_started"])
    matchId: str | None = Field(default=None, examples=["match-0001"])


class MetadataProviderSettings(DocsModel):
    configured: dict[str, bool] = Field(default_factory=dict)
    providers: dict[str, Any] = Field(default_factory=dict)


class MetadataLanguageSettingsResponse(DocsModel):
    locales: list[str] = Field(default_factory=list, examples=[["en-US", "en"]])
    preferNoLanguageForBackdrop: bool | None = Field(default=None, examples=[False])


class MetadataRefreshSettings(DocsModel):
    checks: dict[str, bool] = Field(default_factory=dict)
    providerDocumentAgeDays: int | None = Field(default=None, examples=[30])
    artworkAgeDays: int | None = Field(default=None, examples=[30])
    catalogAgeDays: int | None = Field(default=None, examples=[30])
    cooldownHours: int | None = Field(default=None, examples=[24])
    blockList: list[str] = Field(default_factory=list)
    replaceExisting: bool | None = Field(default=None, examples=[False])


class MetadataRefreshRequest(DocsModel):
    refreshAll: bool | None = Field(default=None, examples=[False])
    entityId: str | None = Field(default=None, examples=["item-0001"])
    entityType: str | None = Field(default=None, examples=["movie"])


class ProviderCredentialRequest(DocsModel):
    credential: str | None = Field(
        default=None,
        description="Provider API key or credential. It is never returned.",
        json_schema_extra={"writeOnly": True},
    )
    clear: bool | None = Field(default=None, examples=[False])


class Library(DocsModel):
    id: str | None = Field(default=None, examples=["library-0001"])
    name: str | None = Field(default=None, examples=["Movies"])
    type: str | None = Field(default=None, examples=["movie"])
    directory: str | None = Field(
        default=None,
        description="Configured library root; returned only to administrators.",
    )
    enabled: bool | None = Field(default=None, examples=[True])
    sortOrder: int | None = Field(default=None, examples=[0])


class LibraryRequest(DocsModel):
    name: str = Field(default="Movies", examples=["Movies"])
    type: str = Field(default="movie", examples=["movie"])
    directory: str = Field(
        default="<configured-library-root>", examples=["<configured-library-root>"]
    )
    enabled: bool | None = Field(default=True, examples=[True])


class LibraryMoveRequest(DocsModel):
    direction: str = Field(default="up", examples=["up"])


class LibraryIdsResponse(DocsModel):
    libraryIds: list[str] = Field(default_factory=list, examples=[["library-0001"]])


class JobRun(DocsModel):
    id: str | None = Field(default=None, examples=["run-0001"])
    state: str | None = Field(default=None, examples=["queued"])
    kind: str | None = Field(default=None, examples=["scan"])
    message: str | None = Field(default=None, examples=["Queued"])
    progress: float | None = Field(default=None, examples=[0.0])
    progressTotal: int | None = Field(default=None, examples=[10000])
    progressDetail: dict[str, Any] | None = None
    scanStats: dict[str, Any] | None = None
    createdAt: str | None = Field(default=None, examples=["2025-01-15T12:00:00Z"])
    finishedAt: str | None = Field(default=None, examples=["2025-01-15T12:05:00Z"])


class JobTrigger(DocsModel):
    id: str | None = Field(default=None, examples=["trigger-0001"])
    type: str | None = Field(default=None, examples=["daily"])
    enabled: bool | None = Field(default=None, examples=[True])
    config: dict[str, Any] = Field(default_factory=dict)
    nextRunAt: str | None = Field(default=None, examples=["2025-01-16T03:00:00Z"])


class Job(DocsModel):
    id: str | None = Field(default=None, examples=["job-0001"])
    key: str | None = Field(default=None, examples=["library_scan:library-0001"])
    name: str | None = Field(default=None, examples=["Movies scan"])
    kind: str | None = Field(default=None, examples=["scan"])
    config: dict[str, Any] = Field(default_factory=dict)
    triggers: list[JobTrigger] = Field(default_factory=list)
    recentRuns: list[JobRun] = Field(default_factory=list)
    lastState: str | None = Field(default=None, examples=["idle"])


class JobsResponse(DocsModel):
    jobs: list[Job] = Field(default_factory=list)


class JobRunResponse(DocsModel):
    run: JobRun | None = None
    job: Job | None = None


class AdminUser(DocsModel):
    id: str | None = Field(default=None, examples=["user-0001"])
    username: str | None = Field(default=None, examples=["example-user"])
    disabled: bool | None = Field(default=None, examples=[False])
    libraries: list[str] = Field(default_factory=list)


class AdminUsersResponse(DocsModel):
    users: list[AdminUser] = Field(default_factory=list)


class AdminProfile(DocsModel):
    username: str | None = Field(default=None, examples=["administrator"])
    disabled: bool | None = Field(default=None, examples=[False])


class AdminAccount(DocsModel):
    username: str | None = Field(default=None, examples=["operator"])
    is_root: bool | None = Field(default=None, examples=[False])
    disabled: bool | None = Field(default=None, examples=[False])


class AdminOverview(DocsModel):
    users: int | None = Field(default=None, examples=[3])
    active_users: int | None = Field(default=None, examples=[2])
    disabled_users: int | None = Field(default=None, examples=[1])
    administrators: int | None = Field(default=None, examples=[1])
    pending_invites: int | None = Field(default=None, examples=[0])


class AdminSession(DocsModel):
    viewerId: str | None = Field(default=None, examples=["viewer-0001"])
    sessionId: str | None = Field(default=None, examples=["session-0001"])
    userId: str | None = Field(default=None, examples=["user-0001"])
    username: str | None = Field(default=None, examples=["example-user"])
    itemId: str | None = Field(default=None, examples=["item-0001"])
    state: str | None = Field(default=None, examples=["playing"])


class AdminSessionsResponse(DocsModel):
    sessions: list[AdminSession] = Field(default_factory=list)


class AdminDevicesResponse(DocsModel):
    devices: list[DeviceMetadata] = Field(default_factory=list)


class AdminDeviceMutationResponse(DocsModel):
    deviceId: str | None = Field(default=None, examples=["device-0001"])
    removed: bool | None = Field(default=None, examples=[True])


class AdminPlaybackSettings(DocsModel):
    maxTranscodes: int | None = Field(default=None, examples=[2])
    maxTranscodesPerUser: int | None = Field(default=None, examples=[1])
    trickplayFrameWidth: int | None = Field(default=None, examples=[320])
    trickplayFrameHeight: int | None = Field(default=None, examples=[180])
    trickplayIntervalSeconds: int | None = Field(default=None, examples=[10])
    trickplayWorkers: int | None = Field(default=None, examples=[1])
    trickplayFfmpegThreads: int | None = Field(default=None, examples=[2])


class AdminIntroOutroSettings(DocsModel):
    enabled: bool | None = Field(default=None, examples=[True])
    task: Job | None = None


class ClearMaintenanceResponse(DocsModel):
    removedSegments: int | None = Field(default=None, examples=[4])
    removed: int | None = Field(default=None, examples=[4])


class Invite(DocsModel):
    id: str | None = Field(default=None, examples=["invite-0001"])
    code: str | None = Field(default=None, json_schema_extra={"readOnly": True})
    status: str | None = Field(default=None, examples=["active"])
    libraryIds: list[str] = Field(default_factory=list)
    maxUses: int | None = Field(default=None, examples=[1])
    expiresAt: str | None = Field(default=None, examples=["2025-01-22T12:00:00Z"])


class InvitesResponse(DocsModel):
    invites: list[Invite] = Field(default_factory=list)


class InviteRequest(DocsModel):
    libraryIds: list[str] = Field(default_factory=list, examples=[["library-0001"]])
    maxUses: int | None = Field(default=1, examples=[1])
    expiresInSeconds: int | None = Field(default=604800, examples=[604800])


class AdminAccountRequest(DocsModel):
    targetUsername: str = Field(default="operator", examples=["operator"])
    newPassword: str = Field(
        default="",
        json_schema_extra={"writeOnly": True},
        description="New administrator password.",
    )


class SyncplayMember(DocsModel):
    userId: str | None = Field(default=None, examples=["user-0001"])
    participantId: str | None = Field(default=None, examples=["browser-tab"])
    username: str | None = Field(default=None, examples=["example-user"])
    watchingTogether: bool | None = Field(default=None, examples=[True])
    viewing: bool | None = Field(default=None, examples=[True])
    loading: bool | None = Field(default=None, examples=[False])


class SyncplayGroup(DocsModel):
    id: str | None = Field(default=None, examples=["group-0001"])
    hostUserId: str | None = Field(default=None, examples=["user-0001"])
    itemId: str | None = Field(default=None, examples=["item-0001"])
    members: list[SyncplayMember] = Field(default_factory=list)
    revision: int | None = Field(default=None, examples=[4])
    timelineRevision: int | None = Field(default=None, examples=[2])
    mediaGeneration: int | None = Field(default=None, examples=[1])
    position: float | None = Field(default=None, examples=[120.5])
    playing: bool | None = Field(default=None, examples=[True])
    playbackState: str | None = Field(default=None, examples=["playing"])
    allowViewerControls: bool | None = Field(default=None, examples=[False])
    ended: bool | None = Field(default=None, examples=[False])


class SyncplayGroupsResponse(DocsModel):
    groups: list[SyncplayGroup] = Field(default_factory=list)


class SyncplayMutationRequest(DocsModel):
    expectedRevision: int | None = Field(default=None, examples=[4])
    operationId: str | None = Field(default=None, examples=["operation-0001"])


class SyncplaySettingsRequest(SyncplayMutationRequest):
    allowViewerControls: bool = Field(default=False, examples=[False])


class SyncplayCommandRequest(SyncplayMutationRequest):
    action: str = Field(default="play", examples=["play"])
    position: float = Field(default=0, examples=[120.5])
    itemId: str | None = Field(default=None, examples=["item-0001"])
    playing: bool | None = Field(default=None, examples=[True])


class SyncplayPresenceRequest(SyncplayMutationRequest):
    mediaGeneration: int = Field(default=1, examples=[1])
    timelineRevision: int = Field(default=2, examples=[2])
    presenceSequence: int = Field(default=5, examples=[5])
    viewing: bool = Field(default=True, examples=[True])
    loading: bool = Field(default=False, examples=[False])
    pauseRoom: bool = Field(default=False, examples=[False])


class SyncplayParticipationRequest(DocsModel):
    operationId: str | None = Field(default=None, examples=["operation-0001"])
    watchingTogether: bool = Field(default=True, examples=[True])


DOC_MODELS: tuple[type[BaseModel], ...] = (
    ErrorResponse,
    ValidationErrorItem,
    ValidationErrorResponse,
    HealthResponse,
    VersionResponse,
    PublicConfigResponse,
    PublicWebUrlResponse,
    LanguageOption,
    LanguagesResponse,
    MetadataLanguagesResponse,
    DeviceMetadata,
    CredentialsRequest,
    RegisterRequest,
    PasswordChangeRequest,
    PasswordResetRequest,
    SessionResponse,
    User,
    UserResponse,
    BootstrapResponse,
    TicketResponse,
    AvatarVersionResponse,
    LocalePatchRequest,
    MetadataLanguagePatchRequest,
    MetadataLanguageResponse,
    TrackLanguageOption,
    PlaybackPreferences,
    WatchHistoryPreferences,
    CatalogArtwork,
    CatalogCredit,
    CatalogMetadata,
    CatalogState,
    CatalogItem,
    CatalogPage,
    CatalogLibrary,
    CatalogLibrariesResponse,
    CatalogItemResponse,
    CatalogStatusResponse,
    CatalogLibraryStatus,
    InviteValidationResponse,
    NotificationPatchRequest,
    AdminUserUpdateRequest,
    MatchesResponse,
    IntroOutroInspection,
    ProviderTestResponse,
    RefreshQueueResponse,
    MetadataResponse,
    CatalogStatePatchRequest,
    ProgressPatchRequest,
    PlayStartRequest,
    MusicTrack,
    MusicRelease,
    MusicAlbumPage,
    MusicAlbumResponse,
    MusicArtist,
    MusicArtistResponse,
    MusicTracksResponse,
    PlaybackCapabilityRequest,
    MediaSource,
    PlaybackNegotiationResponse,
    PlaybackAccessRequest,
    PlaybackAccessResponse,
    ViewerHeartbeatRequest,
    PlaybackViewer,
    PlaybackSourceResponse,
    TrickplayManifest,
    PlaybackSegmentsResponse,
    PlaybackSessionResponse,
    LyricsResponse,
    NotificationThumbnail,
    Notification,
    NotificationPage,
    NotificationSummary,
    NotificationMutationResponse,
    CalendarEvent,
    CalendarPage,
    CalendarSettings,
    CalendarFollowRequest,
    BazarrSettings,
    BazarrStatus,
    BazarrSearchRequest,
    BazarrDownloadRequest,
    BazarrSearchResponse,
    BazarrDownloadResponse,
    MetadataProviderSettings,
    MetadataLanguageSettingsResponse,
    MetadataRefreshSettings,
    MetadataRefreshRequest,
    ProviderCredentialRequest,
    Library,
    LibraryRequest,
    LibraryMoveRequest,
    LibraryIdsResponse,
    JobRun,
    JobTrigger,
    Job,
    JobsResponse,
    JobRunResponse,
    AdminUser,
    AdminUsersResponse,
    AdminProfile,
    AdminAccount,
    AdminOverview,
    AdminSession,
    AdminSessionsResponse,
    AdminDevicesResponse,
    AdminDeviceMutationResponse,
    AdminPlaybackSettings,
    AdminIntroOutroSettings,
    ClearMaintenanceResponse,
    Invite,
    InvitesResponse,
    InviteRequest,
    AdminAccountRequest,
    SyncplayMember,
    SyncplayGroup,
    SyncplayGroupsResponse,
    SyncplayMutationRequest,
    SyncplaySettingsRequest,
    SyncplayCommandRequest,
    SyncplayPresenceRequest,
    SyncplayParticipationRequest,
    FlexibleObject,
)


OPENAPI_TAGS = [
    {
        "name": "System & Client Bootstrap",
        "description": "Health, version, public configuration, language catalogs, and client registration.",
    },
    {
        "name": "Authentication",
        "description": "Regular-user sessions, browser cookies, and short-lived capability tickets.",
    },
    {
        "name": "Account & Preferences",
        "description": "The authenticated user's account, avatar, locale, playback, and watch-history preferences.",
    },
    {
        "name": "Catalog",
        "description": "Grant-filtered movies, series, episodes, collections, search, artwork, and catalog state.",
    },
    {
        "name": "Music",
        "description": "Album, release, artist, credit, and track catalog reads.",
    },
    {
        "name": "Playback",
        "description": "Playback negotiation, viewer heartbeats, media sources, sessions, lyrics, subtitles, and streaming outputs.",
    },
    {
        "name": "Notifications",
        "description": "The authenticated in-app notification inbox and unread summary.",
    },
    {
        "name": "Calendar",
        "description": "Grant-filtered calendar events and per-event follow state.",
    },
    {
        "name": "Subtitles & Bazarr",
        "description": "Bazarr subtitle discovery/download flows and administrator connection settings.",
    },
    {
        "name": "Admin · Identity & Access",
        "description": "Administrator authentication, profiles, users, accounts, and invites.",
    },
    {
        "name": "Admin · Libraries & Jobs",
        "description": "Library configuration, scans, scheduler definitions, runs, catalog inspection, and matching.",
    },
    {
        "name": "Admin · Metadata & Integrations",
        "description": "Metadata providers, locale policy, refresh settings, and calendar integration settings.",
    },
    {
        "name": "Admin · Playback & Maintenance",
        "description": "Administrator playback capacity, devices, active sessions, trickplay, and intro/outro maintenance.",
    },
    {
        "name": "SyncPlay & Realtime",
        "description": "SyncPlay groups plus supplemental WebSocket connection and message guidance.",
    },
]


OPENAPI_DESCRIPTION = """# ZenStream Orchestrator API

This is the versioned HTTP contract for the ZenStream Orchestrator. The
examples use synthetic IDs and `example-*` values; they are safe to copy into
development requests. The server remains tolerant of additional JSON fields,
and clients should ignore fields added in future versions.

## Authentication

Regular users can authenticate with `Authorization: Bearer <session>` or the
HttpOnly browser session cookie. Production uses
`__Host-zenstream-session`; loopback HTTP uses the port-scoped
`zenstream-session-<api-port>` cookie. Login and registration set a browser
cookie when used by the browser flow. `/api/auth/resource-ticket`,
`/api/auth/artwork-ticket`, and `/api/auth/socket-ticket` issue short-lived
capability tickets. Pass resource/artwork tickets as the `access` query
parameter when a media URL cannot carry headers, and pass socket tickets as
the `ticket` query parameter when opening a WebSocket.

Administrator routes accept the HttpOnly administrator session cookie or the
legacy `Username` plus `TOKEN` headers. `/api/admin/login` accepts `Username`
and `Password` headers and sets the administrator cookie. The legacy headers
remain documented for existing dashboard clients.

## Requests, pagination, and locale

JSON write bodies are documented even though handlers still parse the raw
request body. `page` is one-based; `pageSize` is bounded by the endpoint and
is normally at most 100. `limit` is a response-size cap and can be used with
catalog list endpoints. Catalog and metadata reads accept a locale/language
selector; omitted values use the authenticated account preference and the
server's configured fallback chain.

Catalog responses support `view=full` (the default rich representation) and
`view=card` (a compact card projection). Home uses `section` values such as
`featured`, `continueWatching`, `nextUp`, `derived`, and `library`; invalid
values are rejected with a normal API error.

## Media, asynchronous work, and errors

Artwork and avatars are private WebP or GIF responses. Subtitle routes return
WebVTT, intro/outro previews return MP3, HLS playlists return
`application/vnd.apple.mpegurl`, and MPEG-TS segments return `video/mp2t`.
Direct media supports `Range: bytes=start-end`, returns `206` with
`Content-Range`, and advertises `Accept-Ranges: bytes`; invalid ranges return
`416` with `Content-Range: bytes */size`.

Artwork materialization and trickplay generation can be pending. Pending JSON
or empty responses use `202` and `Retry-After`; image responses also expose
`X-ZenStream-Image-State: pending`. Administrator scans, refreshes, and
analysis tasks return a job/run representation so callers can poll status and
observe the monotonic `progressTotal`/`progressDetail` fields. Cancellation
and conflict cases retain their existing `409` semantics.

JSON errors use `{"detail": ...}`. Validation errors use FastAPI's `detail`
array. HTTP status codes and non-JSON media types below describe the existing
handlers and do not install runtime validation.

## Realtime channels (supplemental)

OpenAPI has no native WebSocket operation type, so the two channels are
described here and in `x-zenstream-websockets`.

* `GET /api/ws/catalog` — open
  `wss://server.example.invalid/api/ws/catalog?ticket=socket-ticket-example`.
  The socket ticket comes from `/api/auth/socket-ticket`. The server sends
  `catalog.status` snapshots, coalesced `catalog.updated` events, and a
  terminal non-scan refresh event. Clients should treat event payloads as
  hints and refetch the affected HTTP catalog resources.
* `GET /api/ws/syncplay` — open
  `wss://server.example.invalid/api/ws/syncplay?ticket=socket-ticket-example&participantId=browser-tab`.
  The participant may instead be supplied with the
  `X-ZenStream-Participant` header. Messages include versioned `groups`,
  `group`, `clock`, `group-ended`, and `participant-replaced` events. HTTP
  mutations carry `expectedRevision` and an idempotent `operationId`; stale
  revisions are rejected or ignored according to the existing mutation.
  Representative messages are shown in the realtime extension.
"""


REALTIME_CHANNELS = {
    "/api/ws/catalog": {
        "protocol": "websocket",
        "connection": "wss://server.example.invalid/api/ws/catalog?ticket=socket-ticket-example",
        "ticket": "Issue a socket ticket with /api/auth/socket-ticket and send it as query parameter ticket.",
        "messages": [
            {
                "direction": "server",
                "type": "catalog.status",
                "example": {
                    "type": "catalog.status",
                    "scanning": False,
                    "generation": 12,
                    "libraries": [],
                },
            },
            {
                "direction": "server",
                "type": "catalog.updated",
                "example": {
                    "type": "catalog.updated",
                    "libraryId": "library-0001",
                    "generation": 12,
                    "reason": "refresh",
                },
            },
        ],
    },
    "/api/ws/syncplay": {
        "protocol": "websocket",
        "connection": "wss://server.example.invalid/api/ws/syncplay?ticket=socket-ticket-example&participantId=browser-tab",
        "ticket": "Issue a socket ticket with /api/auth/socket-ticket. Participant identity is required via participantId or X-ZenStream-Participant.",
        "revisionRules": [
            "HTTP mutations include expectedRevision and operationId when applicable.",
            "The server broadcasts the resulting state with a monotonically increasing revision.",
            "A replacement socket for the same user and participant identity replaces the older socket.",
        ],
        "messages": [
            {
                "direction": "server",
                "type": "groups",
                "example": {"version": 1, "type": "groups", "groups": []},
            },
            {
                "direction": "server",
                "type": "group",
                "example": {
                    "version": 1,
                    "type": "group",
                    "group": {"id": "group-0001", "revision": 4, "members": []},
                },
            },
            {
                "direction": "client",
                "type": "clock",
                "example": {
                    "version": 1,
                    "type": "clock",
                    "id": "group-0001",
                    "revision": 4,
                    "position": 120.5,
                    "playing": True,
                },
            },
        ],
    },
}


DOCUMENTATION_EXCLUDED_PATHS = frozenset(
    {
        "/api/docs/",
        "/favicon.ico",
        "/web/{path}",
    }
)


_PARAMETER_DESCRIPTIONS = {
    "entity_id": "Stable catalog entity identifier.",
    "release_id": "Stable music release identifier.",
    "artist_id": "Stable music artist identifier.",
    "person_id": "Stable catalog person identifier.",
    "library_id": "Stable administrator library identifier.",
    "job_id": "Stable scheduled-job identifier.",
    "run_id": "Stable job-run identifier.",
    "trigger_id": "Stable job-trigger identifier.",
    "viewer_id": "Stable playback viewer identifier.",
    "session_id": "Stable playback session identifier.",
    "media_file_id": "Stable media-file identifier.",
    "group_id": "Stable SyncPlay group identifier.",
    "member_id": "Stable SyncPlay member user identifier.",
    "notification_id": "Stable notification identifier.",
    "event_id": "Stable calendar event identifier.",
    "invite_id": "Stable invitation identifier.",
    "device_id": "Stable playback device identifier.",
    "provider": "Metadata provider identifier.",
    "generation": "Trickplay generation returned by the manifest.",
    "sheet_index": "Zero-based trickplay sheet index.",
    "kind": "Output kind. The supported administrator preview kind is `intro` or `outro`.",
    "image_type": "Artwork category, normally `Primary`, `Backdrop`, or `Logo`.",
    "displayLanguage": "Locale used for language display names, for example `en-US`.",
    "language": "Requested metadata locale; omitted uses the account/server fallback.",
    "locale": "Requested metadata locale.",
    "metadataLanguage": "Preferred provider metadata language.",
    "libraryId": "Optional library filter.",
    "parentId": "Optional parent entity filter.",
    "seasonId": "Optional season entity for episode detail.",
    "sourceId": "Playback source identifier selected during negotiation.",
    "userId": "Optional administrator user filter.",
    "start": "Inclusive calendar start date or timestamp.",
    "end": "Exclusive calendar end date or timestamp.",
    "page": "One-based page number.",
    "pageSize": "Number of rows per page, bounded by the endpoint.",
    "limit": "Optional response-size cap.",
    "query": "Search text.",
    "sortBy": "Server-supported sort field.",
    "sortOrder": "Sort direction; the default is `ascending`.",
    "view": "Catalog projection: `full` or compact `card`.",
    "section": "Home/detail section selector.",
    "type": "Optional catalog type filter.",
    "invite": "Invitation code.",
    "url": "Legacy invitation header accepted by the registration flow.",
    "disabled": "Administrator disabled-state value.",
    "v": "Artwork/avatar version used for immutable cache validation.",
    "w": "Optional persistent artwork variant width; supported values are 160 and 320 pixels.",
    "access": "Optional short-lived resource or artwork ticket for headerless media requests.",
    "ticket": "Short-lived WebSocket ticket.",
    "participantId": "Client participant identity for a SyncPlay WebSocket.",
    "includeFirstPage": "Whether to include the first page of each library in the response.",
    "cropX": "Horizontal crop origin in source-image pixels.",
    "cropY": "Vertical crop origin in source-image pixels.",
    "cropSize": "Square crop size in source-image pixels.",
    "rotation": "Clockwise image rotation in degrees.",
    "filename": "HLS playlist or MPEG-TS filename returned by playback negotiation.",
    "target_username": "Administrator username whose state is being changed.",
    "Target-Username": "New administrator username supplied to the legacy account-creation flow.",
    "New-Username": "Replacement administrator username.",
    "imageType": "Artwork category, normally `Primary`, `Backdrop`, or `Logo`.",
}


_PARAMETER_EXAMPLES = {
    "entity_id": "item-0001",
    "release_id": "release-0001",
    "artist_id": "artist-0001",
    "person_id": "person-0001",
    "library_id": "library-0001",
    "job_id": "job-0001",
    "run_id": "run-0001",
    "trigger_id": "trigger-0001",
    "viewer_id": "viewer-0001",
    "session_id": "session-0001",
    "media_file_id": "media-file-0001",
    "group_id": "group-0001",
    "member_id": "user-0002",
    "notification_id": "notification-0001",
    "event_id": "event-0001",
    "invite_id": "invite-0001",
    "device_id": "device-0001",
    "provider": "tmdb",
    "image_type": "Primary",
    "displayLanguage": "en-US",
    "language": "en",
    "locale": "en-US",
    "libraryId": "library-0001",
    "parentId": "series-0001",
    "seasonId": "season-0001",
    "sourceId": "source-0001",
    "userId": "user-0001",
    "start": "2025-01-01",
    "end": "2025-02-01",
    "query": "example",
    "view": "full",
    "section": "featured",
    "type": "movie",
    "invite": "invite-example",
    "disabled": False,
    "v": "avatar-1",
    "access": "ticket-example",
    "ticket": "ticket-example",
    "participantId": "browser-tab",
    "includeFirstPage": False,
    "cropX": 0,
    "cropY": 0,
    "cropSize": 100,
    "rotation": 0,
    "filename": "master.m3u8",
    "target_username": "operator",
    "Target-Username": "operator",
    "New-Username": "operator-new",
    "imageType": "Primary",
}


_SUMMARY_OVERRIDES = {
    ("GET", "/"): "Check API health",
    ("POST", "/api/auth/login"): "Create a bearer session",
    ("GET", "/api/auth/me"): "Get the current user",
    ("POST", "/api/account/avatar"): "Upload an avatar",
    ("DELETE", "/api/account/avatar"): "Remove the current avatar",
    ("POST", "/api/account/password"): "Change the account password",
    ("GET", "/api/users/{user_id}/avatar"): "Get a user avatar",
    ("GET", "/api/auth/bootstrap"): "Load client bootstrap data",
    ("POST", "/api/auth/browser-login"): "Create a browser session",
    ("POST", "/api/auth/logout"): "Revoke the current session",
    ("GET", "/api/auth/resource-ticket"): "Issue a resource ticket",
    ("GET", "/api/auth/artwork-ticket"): "Issue an artwork ticket",
    ("POST", "/api/auth/socket-ticket"): "Issue a WebSocket ticket",
    ("GET", "/api/catalog/status"): "Get catalog scan status",
    ("GET", "/api/metadata/languages"): "List metadata languages",
    ("GET", "/api/languages"): "List supported display languages",
    ("GET", "/api/preferences/locale"): "Get the account locale",
    ("PATCH", "/api/preferences/locale"): "Set the account locale",
    ("GET", "/api/preferences/metadata-language"): "Get the metadata language",
    ("PATCH", "/api/preferences/metadata-language"): "Set the metadata language",
    ("GET", "/api/preferences/playback"): "Get playback preferences",
    ("PATCH", "/api/preferences/playback"): "Set playback preferences",
    ("GET", "/api/preferences/watch-history"): "Get watch-history preferences",
    ("PATCH", "/api/preferences/watch-history"): "Set watch-history preferences",
    ("GET", "/api/catalog/libraries"): "List accessible catalog libraries",
    ("GET", "/api/catalog/home"): "Get the home catalog sections",
    ("GET", "/api/catalog/items"): "List items in a library",
    ("GET", "/api/catalog/music/albums"): "List music albums",
    ("GET", "/api/catalog/music/albums/{release_id}"): "Get a music album",
    ("GET", "/api/catalog/music/artists/{artist_id}"): "Get a music artist",
    ("GET", "/api/catalog/music/artists/{artist_id}/tracks"): "List an artist's tracks",
    ("GET", "/api/catalog/search"): "Search the catalog",
    ("GET", "/api/catalog/favorites"): "List favorite catalog items",
    ("GET", "/api/catalog/items/{entity_id}"): "Get a catalog item",
    ("GET", "/api/catalog/items/{entity_id}/similar"): "List similar items",
    ("GET", "/api/catalog/items/{entity_id}/metadata"): "Get item metadata",
    (
        "GET",
        "/api/catalog/items/{entity_id}/images/{image_type}",
    ): "Get catalog artwork",
    (
        "GET",
        "/api/catalog/items/{entity_id}/people/{person_id}/image",
    ): "Get a person image",
    ("GET", "/api/catalog/items/{entity_id}/detail"): "Get item detail sections",
    ("PATCH", "/api/catalog/items/{entity_id}/state"): "Update item state",
    ("PATCH", "/api/catalog/items/{entity_id}/progress"): "Update item progress",
    ("POST", "/api/catalog/items/{entity_id}/play-start"): "Record a play start",
    ("DELETE", "/api/account/watch-history"): "Clear watch history",
    ("POST", "/api/playback/items/{entity_id}/negotiate"): "Negotiate playback",
    ("POST", "/api/playback/items/{entity_id}/access"): "Refresh playback access",
    ("POST", "/api/playback/viewers/{viewer_id}/heartbeat"): "Update a playback viewer",
    ("DELETE", "/api/playback/viewers/{viewer_id}"): "End a playback viewer",
    ("GET", "/api/playback/items/{entity_id}/source"): "Get playback source metadata",
    ("GET", "/api/playback/items/{entity_id}/trickplay"): "Get the trickplay manifest",
    ("GET", "/api/playback/items/{entity_id}/segments"): "List intro/outro segments",
    (
        "GET",
        "/api/playback/items/{entity_id}/trickplay/{generation}/{sheet_index}.webp",
    ): "Get a trickplay sheet",
    ("GET", "/api/playback/items/{entity_id}/stream"): "Stream media",
    ("HEAD", "/api/playback/items/{entity_id}/stream"): "Inspect media headers",
    ("GET", "/api/playback/sessions/{session_id}/{filename}"): "Get an HLS output",
    ("GET", "/api/playback/sessions/{session_id}"): "Get playback session status",
    ("DELETE", "/api/playback/sessions/{session_id}"): "Cancel a playback session",
    ("GET", "/api/playback/items/{entity_id}/lyrics"): "Get item lyrics",
    (
        "GET",
        "/api/playback/items/{entity_id}/subtitles/{media_file_id}.vtt",
    ): "Get a subtitle track",
    ("GET", "/api/admin/users"): "List users",
    ("POST", "/api/admin/users"): "Create a user",
    ("GET", "/api/admin/users/{user_id}/avatar"): "Get a user avatar as administrator",
    ("PUT", "/api/admin/users/{user_id}/libraries"): "Set user library grants",
    ("POST", "/api/admin/users/{user_id}/reset-password"): "Reset a user password",
    ("PATCH", "/api/admin/users/{user_id}"): "Update a user",
    ("DELETE", "/api/admin/users/{user_id}"): "Delete a user",
    (
        "GET",
        "/api/catalog/items/{entity_id}/bazarr/status",
    ): "Get Bazarr subtitle status",
    ("POST", "/api/catalog/items/{entity_id}/bazarr/search"): "Search Bazarr subtitles",
    (
        "POST",
        "/api/catalog/items/{entity_id}/bazarr/download",
    ): "Download a Bazarr subtitle",
    ("GET", "/api/admin/bazarr/settings"): "Get Bazarr settings",
    ("PUT", "/api/admin/bazarr/settings"): "Update Bazarr settings",
    ("GET", "/api/calendar"): "List calendar events",
    ("PATCH", "/api/calendar/events/{event_id}/follow"): "Update calendar follow state",
    ("GET", "/api/admin/calendar/settings"): "Get calendar integration settings",
    ("PUT", "/api/admin/calendar/settings"): "Update calendar integration settings",
    ("GET", "/api/notifications"): "List notifications",
    ("PATCH", "/api/notifications/{notification_id}"): "Update a notification",
    ("DELETE", "/api/notifications/{notification_id}"): "Delete a notification",
    ("POST", "/api/notifications/read-all"): "Mark all notifications read",
    ("GET", "/api/notifications/summary"): "Get the notification summary",
    ("GET", "/api/admin/metadata/providers"): "Get metadata provider status",
    ("GET", "/api/admin/metadata/languages"): "Get metadata language settings",
    ("PUT", "/api/admin/metadata/languages"): "Update metadata language settings",
    ("GET", "/api/admin/metadata/refresh/settings"): "Get metadata refresh settings",
    ("PUT", "/api/admin/metadata/refresh/settings"): "Update metadata refresh settings",
    ("POST", "/api/admin/metadata/refresh"): "Queue metadata refresh",
    ("PUT", "/api/admin/metadata/providers/{provider}"): "Update provider credentials",
    (
        "POST",
        "/api/admin/metadata/providers/{provider}/test",
    ): "Test a metadata provider",
    ("GET", "/api/admin/libraries"): "List administrator libraries",
    ("POST", "/api/admin/libraries"): "Create a library",
    ("GET", "/api/admin/libraries/{library_id}"): "Get a library",
    ("PATCH", "/api/admin/libraries/{library_id}"): "Update a library",
    ("DELETE", "/api/admin/libraries/{library_id}"): "Delete a library",
    ("POST", "/api/admin/libraries/{library_id}/scan"): "Queue a library scan",
    (
        "POST",
        "/api/admin/libraries/{library_id}/move",
    ): "Move a library in display order",
    ("GET", "/api/admin/library-jobs/{job_id}"): "Get a library job",
    ("GET", "/api/admin/jobs"): "List scheduled jobs",
    ("GET", "/api/admin/jobs/{job_id}"): "Get a scheduled job",
    ("PATCH", "/api/admin/jobs/{job_id}"): "Update a scheduled job",
    ("POST", "/api/admin/jobs/{job_id}/triggers"): "Add a job trigger",
    (
        "DELETE",
        "/api/admin/jobs/{job_id}/triggers/{trigger_id}",
    ): "Remove a job trigger",
    ("POST", "/api/admin/jobs/{job_id}/run"): "Run a job now",
    ("POST", "/api/admin/jobs/{job_id}/runs/{run_id}/terminate"): "Terminate a job run",
    (
        "GET",
        "/api/admin/libraries/{library_id}/catalog-status",
    ): "Get library catalog status",
    ("GET", "/api/admin/libraries/{library_id}/items"): "Inspect library items",
    ("GET", "/api/admin/library-items/{entity_id}"): "Inspect a catalog item",
    (
        "POST",
        "/api/admin/library-items/{entity_id}/metadata/refresh",
    ): "Refresh item metadata",
    (
        "GET",
        "/api/admin/library-items/{entity_id}/trickplay/{generation}/{sheet_index}.webp",
    ): "Get an administrator trickplay sheet",
    (
        "GET",
        "/api/admin/library-items/{entity_id}/intro-outro",
    ): "Inspect intro/outro data",
    (
        "GET",
        "/api/admin/library-items/{entity_id}/intro-outro/{kind}.mp3",
    ): "Preview an intro/outro segment",
    ("GET", "/api/admin/library-items/{entity_id}/matches"): "Find metadata matches",
    ("POST", "/api/admin/library-items/{entity_id}/match"): "Set a metadata match",
    ("GET", "/api/admin/library-items/{entity_id}/image"): "Get administrator artwork",
    ("POST", "/api/admin/login"): "Create an administrator session",
    ("POST", "/api/admin/logout"): "End an administrator session",
    ("GET", "/api/admin/profile"): "Get the administrator profile",
    ("PATCH", "/api/admin/profile"): "Update the administrator profile",
    ("GET", "/api/admin/overview"): "Get administrator overview counts",
    ("GET", "/api/admin/sessions"): "List active playback sessions",
    ("GET", "/api/admin/sessions/{viewer_id}"): "Get an active playback session",
    ("POST", "/api/admin/sessions/{viewer_id}/command"): "Send a playback command",
    ("GET", "/api/admin/devices"): "List playback devices",
    ("DELETE", "/api/admin/devices/{device_id}"): "Remove a playback device",
    ("GET", "/api/admin/playback/settings"): "Get playback capacity settings",
    ("PUT", "/api/admin/playback/settings"): "Update playback capacity settings",
    ("GET", "/api/admin/intro-outro/settings"): "Get intro/outro settings",
    ("PUT", "/api/admin/intro-outro/settings"): "Update intro/outro settings",
    ("POST", "/api/admin/intro-outro/clear"): "Clear intro/outro analysis",
    ("POST", "/api/admin/trickplay/clear"): "Clear trickplay data",
    ("GET", "/api/admin/accounts"): "List administrator accounts",
    ("POST", "/api/admin/accounts"): "Create an administrator account",
    (
        "PATCH",
        "/api/admin/accounts/{target_username}",
    ): "Enable or disable an administrator",
    ("POST", "/api/admin/invites"): "Create an invitation",
    ("GET", "/api/admin/invites"): "List invitations",
    ("DELETE", "/api/admin/invites/{invite_id}"): "Delete an invitation",
    ("GET", "/api/user/check_invite"): "Validate an invitation",
    ("GET", "/api/version"): "Get API versions",
    ("GET", "/api/config/public-web-url"): "Get the public web URL",
    ("GET", "/api/config"): "Get client configuration",
    ("POST", "/api/user/register"): "Register a client account",
    ("GET", "/api/syncplay/groups"): "List SyncPlay groups",
    ("POST", "/api/syncplay/groups"): "Create a SyncPlay group",
    ("GET", "/api/syncplay/groups/{group_id}"): "Get a SyncPlay group",
    ("DELETE", "/api/syncplay/groups/{group_id}"): "Leave a SyncPlay group",
    ("PATCH", "/api/syncplay/groups/{group_id}"): "Update SyncPlay settings",
    ("POST", "/api/syncplay/groups/{group_id}/join"): "Join a SyncPlay group",
    (
        "DELETE",
        "/api/syncplay/groups/{group_id}/members/{member_id}",
    ): "Remove a SyncPlay member",
    ("POST", "/api/syncplay/groups/{group_id}/command"): "Send a SyncPlay command",
    ("POST", "/api/syncplay/groups/{group_id}/presence"): "Report SyncPlay presence",
    (
        "POST",
        "/api/syncplay/groups/{group_id}/participation",
    ): "Set SyncPlay participation",
}


def _stable_operation_id(method: str, path: str) -> str:
    """Return an operation ID derived only from the public method/path."""

    value = re.sub(r"\{([^}:]+)(?::[^}]+)?\}", r"by_\1", path.strip("/"))
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return f"{method.lower()}_{value or 'health'}"


def _tag_for_path(path: str) -> str:
    if path == "/" or path in {
        "/api/version",
        "/api/config",
        "/api/config/public-web-url",
        "/api/languages",
        "/api/metadata/languages",
        "/api/user/check_invite",
        "/api/user/register",
    }:
        return "System & Client Bootstrap"
    if path.startswith("/api/auth/"):
        return "Authentication"
    if (
        path.startswith("/api/account/")
        or path.startswith("/api/preferences/")
        or path == "/api/users/{user_id}/avatar"
    ):
        return "Account & Preferences"
    if path.startswith("/api/catalog/music/"):
        return "Music"
    if path.startswith("/api/playback/"):
        return "Playback"
    if path.startswith("/api/notifications"):
        return "Notifications"
    if path == "/api/calendar" or path.startswith("/api/calendar/"):
        return "Calendar"
    if "/bazarr/" in path or path.endswith("/bazarr/settings"):
        return "Subtitles & Bazarr"
    if path.startswith("/api/syncplay/"):
        return "SyncPlay & Realtime"
    if path.startswith("/api/admin/"):
        if path.startswith("/api/admin/metadata/") or path.startswith(
            "/api/admin/calendar/"
        ):
            return "Admin · Metadata & Integrations"
        if (
            path.startswith("/api/admin/libraries")
            or path.startswith("/api/admin/library")
            or path.startswith("/api/admin/jobs")
        ):
            return "Admin · Libraries & Jobs"
        if any(
            path.startswith(prefix)
            for prefix in (
                "/api/admin/sessions",
                "/api/admin/devices",
                "/api/admin/playback",
                "/api/admin/intro-outro",
                "/api/admin/trickplay",
            )
        ):
            return "Admin · Playback & Maintenance"
        return "Admin · Identity & Access"
    return "Catalog"


def _description_for(tag: str, method: str, path: str, summary: str) -> str:
    details = {
        "System & Client Bootstrap": "Use this endpoint during client startup or registration.",
        "Authentication": "This endpoint participates in regular-user authentication or capability-ticket issuance.",
        "Account & Preferences": "The response is scoped to the authenticated account and preserves the existing preference payload.",
        "Catalog": "Results are filtered by the authenticated user's library grants. Additional catalog fields may be added without a breaking change.",
        "Music": "Music reads preserve the release, artist-credit, and track relationships needed by clients.",
        "Playback": "Playback state and media access remain bound to the authenticated account and selected playback source.",
        "Notifications": "The inbox is authenticated and returns the shared notification shape used by web and mobile clients.",
        "Calendar": "Calendar data is grant-filtered and dates are returned in the existing normalized API format.",
        "Subtitles & Bazarr": "The exact catalog media source is carried through the subtitle workflow; provider identity is not a library lookup key.",
        "Admin · Identity & Access": "Administrator authentication is required; existing cookie and legacy-header clients remain supported.",
        "Admin · Libraries & Jobs": "This administrator operation exposes persisted library/job state without changing scheduler ownership or scan behavior.",
        "Admin · Metadata & Integrations": "This administrator operation manages provider, locale, refresh, or calendar integration state.",
        "Admin · Playback & Maintenance": "This administrator operation controls playback capacity or bounded maintenance work.",
        "SyncPlay & Realtime": "SyncPlay mutations use the existing revision and participant rules; stale state is reported with the existing conflict shape.",
    }
    return f"{summary}. {details[tag]} Method: `{method}`; path: `{path}`."


def _schema_ref(model: type[BaseModel]) -> dict[str, str]:
    return {"$ref": SCHEMA_REF.format(model=model.__name__)}


def _json_content(model: type[BaseModel], example: Any | None = None) -> dict[str, Any]:
    schema = _schema_ref(model) if isinstance(model, type) else model
    content: dict[str, Any] = {"application/json": {"schema": schema}}
    if example is not None:
        content["application/json"]["example"] = example
    return content


def _json_response(
    model: type[BaseModel] | dict[str, Any],
    description: str,
    example: Any | None = None,
    headers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "description": description,
        "content": _json_content(model, example),
    }
    if headers:
        value["headers"] = headers
    return value


def _empty_response(
    description: str, headers: dict[str, Any] | None = None
) -> dict[str, Any]:
    value: dict[str, Any] = {"description": description}
    if headers:
        value["headers"] = headers
    return value


def _binary_response(
    media_types: tuple[str, ...],
    description: str,
    headers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "description": description,
        "content": {
            media_type: {"schema": {"type": "string", "format": "binary"}}
            for media_type in media_types
        },
    }
    if headers:
        value["headers"] = headers
    return value


def _error_responses(statuses: tuple[int, ...]) -> dict[str, Any]:
    return {
        str(status): _json_response(
            ErrorResponse,
            {
                400: "The request is malformed or contains an unsupported value.",
                401: "Authentication is required or the supplied credential has expired.",
                403: "The authenticated principal is not allowed to perform this operation.",
                404: "The requested resource is not available to this principal.",
                409: "The resource is in conflict or the supplied revision is stale.",
                413: "The request body is too large.",
                415: "The uploaded media type is unsupported.",
                416: "The requested byte range is invalid.",
                422: "The request cannot be processed or a media conversion failed.",
                429: "The request rate limit has been exceeded.",
                503: "The required service or media tool is unavailable.",
            }[status],
        )
        for status in statuses
    }


_REQUEST_MODELS: dict[tuple[str, str], type[BaseModel]] = {
    ("POST", "/api/auth/login"): CredentialsRequest,
    ("POST", "/api/auth/browser-login"): CredentialsRequest,
    ("POST", "/api/account/avatar"): FlexibleObject,
    ("POST", "/api/account/password"): PasswordChangeRequest,
    ("POST", "/api/user/register"): RegisterRequest,
    ("PATCH", "/api/preferences/locale"): LocalePatchRequest,
    ("PATCH", "/api/preferences/metadata-language"): MetadataLanguagePatchRequest,
    ("PATCH", "/api/preferences/playback"): PlaybackPreferences,
    ("PATCH", "/api/preferences/watch-history"): WatchHistoryPreferences,
    ("PATCH", "/api/catalog/items/{entity_id}/state"): CatalogStatePatchRequest,
    ("PATCH", "/api/catalog/items/{entity_id}/progress"): ProgressPatchRequest,
    ("POST", "/api/catalog/items/{entity_id}/play-start"): PlayStartRequest,
    ("POST", "/api/playback/items/{entity_id}/negotiate"): PlaybackCapabilityRequest,
    ("POST", "/api/playback/items/{entity_id}/access"): PlaybackAccessRequest,
    ("POST", "/api/playback/viewers/{viewer_id}/heartbeat"): ViewerHeartbeatRequest,
    ("POST", "/api/catalog/items/{entity_id}/bazarr/search"): BazarrSearchRequest,
    ("POST", "/api/catalog/items/{entity_id}/bazarr/download"): BazarrDownloadRequest,
    ("PUT", "/api/admin/bazarr/settings"): BazarrSettings,
    ("PATCH", "/api/calendar/events/{event_id}/follow"): CalendarFollowRequest,
    ("PUT", "/api/admin/calendar/settings"): CalendarSettings,
    ("PATCH", "/api/notifications/{notification_id}"): NotificationPatchRequest,
    ("PUT", "/api/admin/metadata/languages"): MetadataLanguageSettingsResponse,
    ("PUT", "/api/admin/metadata/refresh/settings"): MetadataRefreshSettings,
    ("POST", "/api/admin/metadata/refresh"): MetadataRefreshRequest,
    ("PUT", "/api/admin/metadata/providers/{provider}"): ProviderCredentialRequest,
    (
        "POST",
        "/api/admin/metadata/providers/{provider}/test",
    ): ProviderCredentialRequest,
    ("POST", "/api/admin/libraries"): LibraryRequest,
    ("PATCH", "/api/admin/libraries/{library_id}"): LibraryRequest,
    ("POST", "/api/admin/libraries/{library_id}/move"): LibraryMoveRequest,
    ("PATCH", "/api/admin/jobs/{job_id}"): FlexibleObject,
    ("POST", "/api/admin/jobs/{job_id}/triggers"): JobTrigger,
    ("POST", "/api/admin/jobs/{job_id}/run"): FlexibleObject,
    ("POST", "/api/admin/library-items/{entity_id}/match"): FlexibleObject,
    ("POST", "/api/admin/sessions/{viewer_id}/command"): FlexibleObject,
    ("PUT", "/api/admin/playback/settings"): AdminPlaybackSettings,
    ("PUT", "/api/admin/intro-outro/settings"): AdminIntroOutroSettings,
    ("POST", "/api/admin/intro-outro/clear"): FlexibleObject,
    ("POST", "/api/admin/trickplay/clear"): FlexibleObject,
    ("POST", "/api/admin/invites"): InviteRequest,
    ("POST", "/api/admin/users"): CredentialsRequest,
    ("PUT", "/api/admin/users/{user_id}/libraries"): LibraryIdsResponse,
    ("POST", "/api/admin/users/{user_id}/reset-password"): PasswordResetRequest,
    ("PATCH", "/api/admin/users/{user_id}"): AdminUserUpdateRequest,
    ("POST", "/api/syncplay/groups/{group_id}/join"): SyncplayMutationRequest,
    ("DELETE", "/api/syncplay/groups/{group_id}"): SyncplayMutationRequest,
    ("PATCH", "/api/syncplay/groups/{group_id}"): SyncplaySettingsRequest,
    (
        "DELETE",
        "/api/syncplay/groups/{group_id}/members/{member_id}",
    ): SyncplayMutationRequest,
    ("POST", "/api/syncplay/groups/{group_id}/command"): SyncplayCommandRequest,
    ("POST", "/api/syncplay/groups/{group_id}/presence"): SyncplayPresenceRequest,
    (
        "POST",
        "/api/syncplay/groups/{group_id}/participation",
    ): SyncplayParticipationRequest,
}


_NO_REQUEST_BODY = frozenset(
    {
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/socket-ticket"),
        ("DELETE", "/api/account/avatar"),
        ("DELETE", "/api/account/watch-history"),
        ("DELETE", "/api/playback/viewers/{viewer_id}"),
        ("DELETE", "/api/playback/sessions/{session_id}"),
        ("DELETE", "/api/notifications/{notification_id}"),
        ("POST", "/api/admin/login"),
        ("POST", "/api/admin/logout"),
        ("DELETE", "/api/admin/users/{user_id}"),
        ("DELETE", "/api/admin/libraries/{library_id}"),
        ("DELETE", "/api/admin/devices/{device_id}"),
        ("DELETE", "/api/admin/invites/{invite_id}"),
        ("DELETE", "/api/admin/jobs/{job_id}/triggers/{trigger_id}"),
        ("POST", "/api/admin/libraries/{library_id}/scan"),
        ("POST", "/api/admin/library-items/{entity_id}/metadata/refresh"),
        ("POST", "/api/admin/jobs/{job_id}/runs/{run_id}/terminate"),
        ("PATCH", "/api/admin/profile"),
        ("POST", "/api/admin/accounts"),
        ("PATCH", "/api/admin/accounts/{target_username}"),
        ("POST", "/api/syncplay/groups"),
    }
)

# Public for contract tests and future route additions. These writes are
# intentionally header-only or action-only in the existing wire contract.
NO_REQUEST_BODY_ROUTES = _NO_REQUEST_BODY


_NO_CONTENT_RESPONSES = frozenset(
    {
        ("POST", "/api/account/password"),
        ("POST", "/api/auth/logout"),
        ("DELETE", "/api/account/watch-history"),
        ("DELETE", "/api/admin/users/{user_id}"),
        ("DELETE", "/api/admin/libraries/{library_id}"),
        ("DELETE", "/api/admin/invites/{invite_id}"),
        ("POST", "/api/admin/logout"),
        ("DELETE", "/api/syncplay/groups/{group_id}"),
    }
)


_RESPONSE_MODELS: dict[tuple[str, str], type[BaseModel]] = {
    ("GET", "/"): HealthResponse,
    ("POST", "/api/auth/login"): SessionResponse,
    ("GET", "/api/auth/me"): UserResponse,
    ("POST", "/api/account/avatar"): AvatarVersionResponse,
    ("DELETE", "/api/account/avatar"): AvatarVersionResponse,
    ("GET", "/api/users/{user_id}/avatar"): FlexibleObject,
    ("GET", "/api/auth/bootstrap"): BootstrapResponse,
    ("POST", "/api/auth/browser-login"): UserResponse,
    ("GET", "/api/auth/resource-ticket"): TicketResponse,
    ("GET", "/api/auth/artwork-ticket"): TicketResponse,
    ("POST", "/api/auth/socket-ticket"): TicketResponse,
    ("GET", "/api/catalog/status"): CatalogStatusResponse,
    ("GET", "/api/metadata/languages"): MetadataLanguagesResponse,
    ("GET", "/api/languages"): LanguagesResponse,
    ("GET", "/api/preferences/locale"): LocalePatchRequest,
    ("PATCH", "/api/preferences/locale"): LocalePatchRequest,
    ("GET", "/api/preferences/metadata-language"): MetadataLanguageResponse,
    ("PATCH", "/api/preferences/metadata-language"): MetadataLanguageResponse,
    ("GET", "/api/preferences/playback"): PlaybackPreferences,
    ("PATCH", "/api/preferences/playback"): PlaybackPreferences,
    ("GET", "/api/preferences/watch-history"): WatchHistoryPreferences,
    ("PATCH", "/api/preferences/watch-history"): WatchHistoryPreferences,
    ("GET", "/api/catalog/libraries"): CatalogLibrariesResponse,
    ("GET", "/api/catalog/home"): CatalogPage,
    ("GET", "/api/catalog/items"): CatalogPage,
    ("GET", "/api/catalog/music/albums"): MusicAlbumPage,
    ("GET", "/api/catalog/music/albums/{release_id}"): MusicAlbumResponse,
    ("GET", "/api/catalog/music/artists/{artist_id}"): MusicArtistResponse,
    ("GET", "/api/catalog/music/artists/{artist_id}/tracks"): MusicTracksResponse,
    ("GET", "/api/catalog/search"): CatalogPage,
    ("GET", "/api/catalog/favorites"): CatalogPage,
    ("GET", "/api/catalog/items/{entity_id}"): CatalogItemResponse,
    ("GET", "/api/catalog/items/{entity_id}/similar"): CatalogPage,
    ("GET", "/api/catalog/items/{entity_id}/metadata"): MetadataResponse,
    ("GET", "/api/catalog/items/{entity_id}/detail"): CatalogPage,
    ("PATCH", "/api/catalog/items/{entity_id}/state"): CatalogState,
    ("PATCH", "/api/catalog/items/{entity_id}/progress"): CatalogState,
    ("POST", "/api/catalog/items/{entity_id}/play-start"): CatalogState,
    ("POST", "/api/playback/items/{entity_id}/negotiate"): PlaybackNegotiationResponse,
    ("POST", "/api/playback/items/{entity_id}/access"): PlaybackAccessResponse,
    ("POST", "/api/playback/viewers/{viewer_id}/heartbeat"): PlaybackViewer,
    ("DELETE", "/api/playback/viewers/{viewer_id}"): PlaybackViewer,
    ("GET", "/api/playback/items/{entity_id}/source"): PlaybackSourceResponse,
    ("GET", "/api/playback/items/{entity_id}/trickplay"): TrickplayManifest,
    ("GET", "/api/playback/items/{entity_id}/segments"): PlaybackSegmentsResponse,
    ("GET", "/api/playback/sessions/{session_id}"): PlaybackSessionResponse,
    ("DELETE", "/api/playback/sessions/{session_id}"): PlaybackSessionResponse,
    ("GET", "/api/playback/items/{entity_id}/lyrics"): LyricsResponse,
    ("GET", "/api/admin/users"): AdminUsersResponse,
    ("POST", "/api/admin/users"): AdminUser,
    ("PUT", "/api/admin/users/{user_id}/libraries"): LibraryIdsResponse,
    ("POST", "/api/admin/users/{user_id}/reset-password"): AdminUser,
    ("PATCH", "/api/admin/users/{user_id}"): AdminUser,
    ("GET", "/api/catalog/items/{entity_id}/bazarr/status"): BazarrStatus,
    ("POST", "/api/catalog/items/{entity_id}/bazarr/search"): BazarrSearchResponse,
    ("POST", "/api/catalog/items/{entity_id}/bazarr/download"): BazarrDownloadResponse,
    ("GET", "/api/admin/bazarr/settings"): BazarrSettings,
    ("PUT", "/api/admin/bazarr/settings"): BazarrSettings,
    ("GET", "/api/calendar"): CalendarPage,
    ("PATCH", "/api/calendar/events/{event_id}/follow"): CalendarEvent,
    ("GET", "/api/admin/calendar/settings"): CalendarSettings,
    ("PUT", "/api/admin/calendar/settings"): CalendarSettings,
    ("GET", "/api/notifications"): NotificationPage,
    ("PATCH", "/api/notifications/{notification_id}"): NotificationMutationResponse,
    ("DELETE", "/api/notifications/{notification_id}"): NotificationMutationResponse,
    ("POST", "/api/notifications/read-all"): NotificationSummary,
    ("GET", "/api/notifications/summary"): NotificationSummary,
    ("GET", "/api/admin/metadata/providers"): MetadataProviderSettings,
    ("GET", "/api/admin/metadata/languages"): MetadataLanguageSettingsResponse,
    ("PUT", "/api/admin/metadata/languages"): MetadataLanguageSettingsResponse,
    ("GET", "/api/admin/metadata/refresh/settings"): MetadataRefreshSettings,
    ("PUT", "/api/admin/metadata/refresh/settings"): MetadataRefreshSettings,
    ("POST", "/api/admin/metadata/refresh"): RefreshQueueResponse,
    ("PUT", "/api/admin/metadata/providers/{provider}"): MetadataProviderSettings,
    ("POST", "/api/admin/metadata/providers/{provider}/test"): ProviderTestResponse,
    ("GET", "/api/admin/libraries"): FlexibleObject,
    ("POST", "/api/admin/libraries"): Library,
    ("GET", "/api/admin/libraries/{library_id}"): Library,
    ("PATCH", "/api/admin/libraries/{library_id}"): Library,
    ("POST", "/api/admin/libraries/{library_id}/scan"): JobRunResponse,
    ("POST", "/api/admin/libraries/{library_id}/move"): Library,
    ("GET", "/api/admin/library-jobs/{job_id}"): Job,
    ("GET", "/api/admin/jobs"): JobsResponse,
    ("GET", "/api/admin/jobs/{job_id}"): Job,
    ("PATCH", "/api/admin/jobs/{job_id}"): Job,
    ("POST", "/api/admin/jobs/{job_id}/triggers"): Job,
    ("DELETE", "/api/admin/jobs/{job_id}/triggers/{trigger_id}"): Job,
    ("POST", "/api/admin/jobs/{job_id}/run"): JobRunResponse,
    ("POST", "/api/admin/jobs/{job_id}/runs/{run_id}/terminate"): JobRunResponse,
    ("GET", "/api/admin/libraries/{library_id}/catalog-status"): CatalogStatusResponse,
    ("GET", "/api/admin/libraries/{library_id}/items"): CatalogPage,
    ("GET", "/api/admin/library-items/{entity_id}"): CatalogItemResponse,
    ("GET", "/api/admin/library-items/{entity_id}/intro-outro"): IntroOutroInspection,
    ("GET", "/api/admin/library-items/{entity_id}/matches"): MatchesResponse,
    ("POST", "/api/admin/library-items/{entity_id}/match"): CatalogItemResponse,
    ("POST", "/api/admin/login"): AdminProfile,
    ("GET", "/api/admin/profile"): AdminProfile,
    ("PATCH", "/api/admin/profile"): AdminProfile,
    ("GET", "/api/admin/overview"): AdminOverview,
    ("GET", "/api/admin/sessions"): AdminSessionsResponse,
    ("GET", "/api/admin/sessions/{viewer_id}"): AdminSession,
    ("POST", "/api/admin/sessions/{viewer_id}/command"): AdminSession,
    ("GET", "/api/admin/devices"): AdminDevicesResponse,
    ("DELETE", "/api/admin/devices/{device_id}"): AdminDeviceMutationResponse,
    ("GET", "/api/admin/playback/settings"): AdminPlaybackSettings,
    ("PUT", "/api/admin/playback/settings"): AdminPlaybackSettings,
    ("GET", "/api/admin/intro-outro/settings"): AdminIntroOutroSettings,
    ("PUT", "/api/admin/intro-outro/settings"): AdminIntroOutroSettings,
    ("POST", "/api/admin/intro-outro/clear"): ClearMaintenanceResponse,
    ("POST", "/api/admin/trickplay/clear"): ClearMaintenanceResponse,
    ("GET", "/api/admin/accounts"): AdminAccount,
    ("POST", "/api/admin/accounts"): FlexibleObject,
    ("PATCH", "/api/admin/accounts/{target_username}"): AdminProfile,
    ("POST", "/api/admin/invites"): Invite,
    ("GET", "/api/admin/invites"): InvitesResponse,
    ("GET", "/api/user/check_invite"): InviteValidationResponse,
    ("GET", "/api/version"): VersionResponse,
    ("GET", "/api/config/public-web-url"): PublicWebUrlResponse,
    ("GET", "/api/config"): PublicConfigResponse,
    ("POST", "/api/user/register"): UserResponse,
    ("GET", "/api/syncplay/groups"): SyncplayGroupsResponse,
    ("POST", "/api/syncplay/groups"): SyncplayGroup,
    ("GET", "/api/syncplay/groups/{group_id}"): SyncplayGroup,
    ("DELETE", "/api/syncplay/groups/{group_id}"): SyncplayGroup,
    ("PATCH", "/api/syncplay/groups/{group_id}"): SyncplayGroup,
    ("POST", "/api/syncplay/groups/{group_id}/join"): SyncplayGroup,
    ("DELETE", "/api/syncplay/groups/{group_id}/members/{member_id}"): SyncplayGroup,
    ("POST", "/api/syncplay/groups/{group_id}/command"): SyncplayGroup,
    ("POST", "/api/syncplay/groups/{group_id}/presence"): SyncplayGroup,
    ("POST", "/api/syncplay/groups/{group_id}/participation"): SyncplayGroup,
}


_RESPONSE_ARRAY_ITEMS: dict[tuple[str, str], type[BaseModel]] = {
    ("GET", "/api/admin/libraries"): Library,
    ("GET", "/api/admin/accounts"): AdminAccount,
    ("GET", "/api/admin/sessions"): AdminSession,
    ("GET", "/api/admin/devices"): DeviceMetadata,
}


_REQUEST_EXAMPLES: dict[type[BaseModel], Any] = {
    CredentialsRequest: {
        "username": "example-user",
        "password": "example-password",
        "deviceId": "web-demo",
    },
    RegisterRequest: {
        "invite": "invite-example",
        "username": "example-user",
        "password": "example-password",
    },
    PasswordChangeRequest: {
        "currentPassword": "example-current-password",
        "newPassword": "example-new-password",
        "confirmNewPassword": "example-new-password",
    },
    PasswordResetRequest: {"password": "example-new-password"},
    LocalePatchRequest: {"locale": "en-US"},
    MetadataLanguagePatchRequest: {"language": "en"},
    PlaybackPreferences: {"audioLanguage": "en", "subtitleLanguage": "off"},
    WatchHistoryPreferences: {"enabled": True},
    CatalogStatePatchRequest: {"favorite": True, "played": False, "following": True},
    ProgressPatchRequest: {"position": 120.5, "duration": 3600.0},
    PlayStartRequest: {"sourceId": "source-0001", "position": 0},
    PlaybackCapabilityRequest: {
        "sourceId": "source-0001",
        "directPlay": True,
        "playerEngine": "media3",
    },
    PlaybackAccessRequest: {"sourceId": "source-0001", "sessionId": "session-0001"},
    ViewerHeartbeatRequest: {"position": 120.5, "duration": 3600.0, "playing": True},
    NotificationPatchRequest: {"read": True},
    AdminUserUpdateRequest: {"disabled": False},
    BazarrSearchRequest: {"sourceId": "source-0001", "languages": ["eng"]},
    BazarrDownloadRequest: {"sourceId": "source-0001", "matchId": "match-0001"},
    CalendarFollowRequest: {"following": True},
    InviteRequest: {
        "libraryIds": ["library-0001"],
        "maxUses": 1,
        "expiresInSeconds": 604800,
    },
    LibraryMoveRequest: {"direction": "up"},
    SyncplaySettingsRequest: {
        "expectedRevision": 4,
        "operationId": "operation-0001",
        "allowViewerControls": False,
    },
    SyncplayCommandRequest: {
        "expectedRevision": 4,
        "operationId": "operation-0001",
        "action": "play",
        "position": 120.5,
    },
    SyncplayPresenceRequest: {
        "mediaGeneration": 1,
        "timelineRevision": 2,
        "presenceSequence": 5,
        "viewing": True,
        "loading": False,
    },
    SyncplayParticipationRequest: {
        "operationId": "operation-0001",
        "watchingTogether": True,
    },
}


_RESPONSE_EXAMPLES: dict[type[BaseModel], Any] = {
    HealthResponse: {"status": "ok"},
    SessionResponse: {
        "token": "session-example",
        "expiresIn": 604800,
        "user": {"id": "user-0001", "username": "example-user"},
    },
    UserResponse: {
        "user": {"id": "user-0001", "username": "example-user", "disabled": False}
    },
    BootstrapResponse: {
        "user": {"id": "user-0001", "username": "example-user"},
        "resourceTicket": "ticket-example",
        "resourceTicketExpiresIn": 900,
        "artworkTicket": "ticket-example",
        "artworkTicketExpiresIn": 900,
        "locale": "en-US",
        "metadataLanguage": "en",
        "languages": [],
        "languageOptions": [],
    },
    VersionResponse: {"version": "1.5.1", "main": "1.5.1"},
    PublicConfigResponse: {
        "apiVersion": 2,
        "catalog": True,
        "playback": True,
        "version": "1.5.1",
        "main": "1.5.1",
    },
    PublicWebUrlResponse: {"publicWebUrl": ""},
    TicketResponse: {"ticket": "ticket-example", "expiresIn": 900},
    AvatarVersionResponse: {"avatarVersion": "avatar-1"},
    MetadataLanguagesResponse: {"languages": ["en", "ja"]},
    MetadataLanguageResponse: {"mode": "auto", "language": "en"},
    PlaybackPreferences: {
        "audioLanguage": "en",
        "subtitleLanguage": "off",
        "audioLanguages": [],
        "subtitleLanguages": [],
    },
    CatalogStatusResponse: {
        "state": "ready",
        "generation": 12,
        "updatedAt": "2025-01-15T12:00:00Z",
        "libraries": [],
    },
    CatalogPage: {"items": [], "page": 1, "pageSize": 40, "total": 0, "hasNext": False},
    MusicAlbumPage: {
        "items": [],
        "page": 1,
        "pageSize": 40,
        "total": 0,
        "hasNext": False,
    },
    NotificationPage: {"notifications": [], "nextCursor": None, "hasMore": False},
    CalendarPage: {"events": [], "start": "2025-01-01", "end": "2025-02-01"},
    SyncplayGroupsResponse: {"groups": []},
    SyncplayGroup: {"id": "group-0001", "revision": 4, "members": [], "ended": False},
    JobRunResponse: {
        "run": {"id": "run-0001", "state": "queued", "progressTotal": 10000}
    },
    RefreshQueueResponse: {"backfill": {"id": "run-0001", "state": "queued"}},
}


def _register_models(components: dict[str, Any]) -> None:
    schemas = components.setdefault("schemas", {})
    for model in DOC_MODELS:
        schema = model.model_json_schema(by_alias=True, ref_template=SCHEMA_REF)
        definitions = schema.pop("$defs", {})
        for name, value in definitions.items():
            schemas.setdefault(name, value)
        schemas[model.__name__] = schema


def _ensure_parameter(
    operation: dict[str, Any],
    name: str,
    location: str,
    *,
    required: bool = False,
    schema: dict[str, Any] | None = None,
    description: str | None = None,
    example: Any | None = None,
) -> None:
    parameters = operation.setdefault("parameters", [])
    current = next(
        (
            item
            for item in parameters
            if item.get("name") == name and item.get("in") == location
        ),
        None,
    )
    if current is None:
        current = {
            "name": name,
            "in": location,
            "required": required,
            "schema": schema or {"type": "string"},
        }
        parameters.append(current)
    if description:
        current["description"] = description
    if example is not None and "example" not in current:
        current["example"] = example


def _annotate_parameters(operation: dict[str, Any], path: str) -> None:
    for parameter in operation.get("parameters", []):
        name = parameter.get("name", "")
        if name in _PARAMETER_DESCRIPTIONS:
            parameter["description"] = _PARAMETER_DESCRIPTIONS[name]
        if name in _PARAMETER_EXAMPLES and "example" not in parameter:
            parameter["example"] = _PARAMETER_EXAMPLES[name]
        if name in {"TOKEN", "Password", "New-Password", "New_Password"}:
            parameter["description"] = (
                "Sensitive legacy administrator credential; never log this value."
            )
            parameter.setdefault("schema", {})["writeOnly"] = True
        if name in {"Username", "New-Username", "New_Username"}:
            parameter["description"] = (
                "Legacy administrator identity header retained for dashboard compatibility."
            )
        if name == "view":
            parameter.setdefault("schema", {})["enum"] = ["full", "card"]
        if name == "section":
            parameter.setdefault("schema", {})["enum"] = [
                "featured",
                "continueWatching",
                "nextUp",
                "derived",
                "library",
            ]
        if name == "sortOrder":
            parameter.setdefault("schema", {})["enum"] = ["ascending", "descending"]
        if name == "image_type":
            parameter.setdefault("schema", {})["enum"] = ["Primary", "Backdrop", "Logo"]
        if name == "kind":
            parameter.setdefault("schema", {})["enum"] = ["intro", "outro"]
        if name == "action":
            parameter.setdefault("schema", {})["enum"] = [
                "media",
                "play",
                "pause",
                "seek",
            ]
        if name == "provider":
            parameter.setdefault("schema", {})["enum"] = ["tmdb", "tvdb", "lastfm"]
        if name == "imageType":
            parameter.setdefault("schema", {})["enum"] = ["Primary", "Backdrop", "Logo"]
        if "description" not in parameter:
            parameter["description"] = (
                f"{name} value supplied in the {parameter.get('in', 'request')} component."
            )
        if (
            "example" not in parameter
            and name not in {"TOKEN", "Password", "New-Password", "New_Password"}
            and parameter.get("schema", {}).get("default") is not None
        ):
            parameter["example"] = parameter["schema"]["default"]
    if (
        path.startswith("/api/playback/")
        or path.startswith("/api/catalog/items/")
        and ("/images/" in path or "/people/" in path)
    ):
        _ensure_parameter(
            operation,
            "access",
            "query",
            description=_PARAMETER_DESCRIPTIONS["access"],
            example="ticket-example",
        )
    if path.startswith("/api/catalog/items/") and (
        "/images/" in path or "/people/" in path
    ):
        _ensure_parameter(
            operation,
            "w",
            "query",
            schema={"type": "integer", "enum": [160, 320]},
            description=_PARAMETER_DESCRIPTIONS["w"],
            example=320,
        )


def _security_for(path: str, method: str) -> list[dict[str, list[Any]]] | list[Any]:
    if path in {
        "/",
        "/api/version",
        "/api/config",
        "/api/config/public-web-url",
        "/api/user/check_invite",
        "/api/user/register",
        "/api/auth/login",
        "/api/auth/browser-login",
        "/api/admin/login",
    }:
        return []
    if path.startswith("/api/admin/"):
        return [
            {"AdminSessionCookie": []},
            {"AdminTokenHeader": [], "AdminUsernameHeader": []},
        ]
    if path.startswith("/api/playback/"):
        return [
            {"UserBearerAuth": []},
            {"UserSessionCookie": []},
            {"ResourceTicket": []},
        ]
    if "/images/" in path or "/people/" in path:
        return [
            {"UserBearerAuth": []},
            {"UserSessionCookie": []},
            {"ArtworkTicket": []},
        ]
    return [{"UserBearerAuth": []}, {"UserSessionCookie": []}]


def _response_for(path: str, method: str) -> dict[str, Any]:
    if method == "HEAD" and path == "/api/playback/items/{entity_id}/stream":
        response = _binary_response(
            ("video/mp4", "audio/mpeg", "application/octet-stream"),
            "Media headers; the body is empty for HEAD.",
            headers={
                "Accept-Ranges": {
                    "description": "Always `bytes` for direct media.",
                    "schema": {"type": "string", "example": "bytes"},
                },
                "Content-Length": {
                    "description": "Selected media size in bytes.",
                    "schema": {
                        "type": "integer",
                        "format": "int64",
                        "example": 1048576,
                    },
                },
                "Content-Range": {
                    "description": "Present for a partial response or an invalid range.",
                    "schema": {"type": "string", "example": "bytes 0-1023/1048576"},
                },
            },
        )
        response["content"] = {}
        return {
            "200": response,
            "206": deepcopy(response),
            "416": _empty_response(
                "Invalid or unsatisfiable byte range.",
                {
                    "Content-Range": {
                        "description": "The valid size is returned as `bytes */size`.",
                        "schema": {"type": "string", "example": "bytes */1048576"},
                    }
                },
            ),
        }
    if path == "/api/playback/items/{entity_id}/stream":
        headers = {
            "Accept-Ranges": {
                "description": "Byte-range support indicator.",
                "schema": {"type": "string", "example": "bytes"},
            },
            "Content-Length": {
                "description": "Returned media length in bytes.",
                "schema": {"type": "integer", "format": "int64", "example": 1048576},
            },
            "Content-Range": {
                "description": "Range served for a `206` response.",
                "schema": {"type": "string", "example": "bytes 0-1023/1048576"},
            },
        }
        return {
            "200": _binary_response(
                ("video/mp4", "audio/mpeg", "application/octet-stream"),
                "Complete or directly playable media.",
                headers,
            ),
            "206": _binary_response(
                ("video/mp4", "audio/mpeg", "application/octet-stream"),
                "Partial media selected by the `Range` request header.",
                headers,
            ),
            "416": _empty_response(
                "Invalid or unsatisfiable byte range.",
                {
                    "Content-Range": {
                        "description": "The valid size is returned as `bytes */size`.",
                        "schema": {"type": "string", "example": "bytes */1048576"},
                    }
                },
            ),
        }
    if path == "/api/playback/sessions/{session_id}/{filename}":
        return {
            "200": _binary_response(
                ("application/vnd.apple.mpegurl", "video/mp2t"),
                "HLS playlist or MPEG-TS segment.",
            ),
        }
    if path.endswith(".vtt"):
        return {"200": _binary_response(("text/vtt",), "WebVTT subtitle content.")}
    if path.endswith(".mp3"):
        return {"200": _binary_response(("audio/mpeg",), "MP3 intro/outro preview.")}
    if (method, path) in _NO_CONTENT_RESPONSES:
        return {
            "204": _empty_response("The operation completed without a response body.")
        }
    if (
        path.endswith(".webp")
        or path.endswith("/image")
        or "/images/" in path
        or "/people/" in path
        or (path.endswith("/avatar") and method == "GET")
    ):
        media_types = (
            ("image/webp", "image/gif") if path.endswith("/avatar") else ("image/webp",)
        )
        result = {
            "200": _binary_response(
                media_types,
                "Private image content.",
                {
                    "Cache-Control": {
                        "description": "Private cache policy for the selected image.",
                        "schema": {"type": "string"},
                    }
                },
            ),
        }
        if path == "/api/catalog/items/{entity_id}/images/{image_type}":
            result["202"] = _empty_response(
                "Image materialization is pending.",
                {
                    "Retry-After": {
                        "description": "Seconds before retrying.",
                        "schema": {"type": "integer", "example": 2},
                    },
                    "X-ZenStream-Image-State": {
                        "description": "Pending image state.",
                        "schema": {"type": "string", "example": "pending"},
                    },
                },
            )
        return result
    model = _RESPONSE_MODELS.get((method, path), FlexibleObject)
    array_item = _RESPONSE_ARRAY_ITEMS.get((method, path))
    if method == "POST" and path in {
        "/api/admin/users",
        "/api/admin/libraries",
        "/api/admin/accounts",
        "/api/admin/invites",
        "/api/user/register",
        "/api/syncplay/groups",
    }:
        status = "201"
    elif (
        method == "POST"
        and path
        in {"/api/admin/libraries/{library_id}/scan", "/api/admin/jobs/{job_id}/run"}
        or method == "POST"
        and path == "/api/admin/login"
    ):
        status = "202"
    else:
        status = "200"
    if array_item is not None:
        result = {
            "200": _json_response(
                {"type": "array", "items": _schema_ref(array_item)},
                "The operation completed successfully.",
                [],
            )
        }
    else:
        result = {
            status: _json_response(
                model,
                "The operation completed successfully.",
                _RESPONSE_EXAMPLES.get(model),
            )
        }
    if path in {"/api/auth/browser-login", "/api/user/register", "/api/admin/login"}:
        result[status].setdefault("headers", {})["Set-Cookie"] = {
            "description": "HttpOnly session cookie set by the browser flow; the value is generated by the server.",
            "schema": {"type": "string"},
        }
    if method == "GET" and path == "/api/playback/items/{entity_id}/trickplay":
        result["202"] = _json_response(
            TrickplayManifest,
            "Trickplay generation is still pending.",
            {"state": "pending", "generation": "generation-1"},
            {
                "Retry-After": {
                    "description": "Seconds before polling again.",
                    "schema": {"type": "integer", "example": 5},
                }
            },
        )
    if method == "GET" and path == "/api/catalog/items/{entity_id}/images/{image_type}":
        result["202"] = _empty_response(
            "Artwork materialization is pending.",
            {
                "Retry-After": {
                    "description": "Seconds before retrying.",
                    "schema": {"type": "integer", "example": 2},
                },
                "X-ZenStream-Image-State": {
                    "description": "Pending image state.",
                    "schema": {"type": "string", "example": "pending"},
                },
            },
        )
    return result


def _error_statuses(path: str, method: str) -> tuple[int, ...]:
    if path in {"/", "/api/version", "/api/config", "/api/config/public-web-url"}:
        return ()
    if path == "/api/account/avatar" and method == "POST":
        return (401, 413, 415, 422)
    if path == "/api/account/avatar" and method == "DELETE":
        return (401,)
    if path in {"/api/auth/login", "/api/auth/browser-login"}:
        return (401, 429)
    if path == "/api/user/register":
        return (403, 409, 429)
    if path == "/api/admin/login":
        return (403,)
    if path.endswith("/stream") and method in {"GET", "HEAD"}:
        return (401, 403, 404)
    if path.endswith(".vtt") or path.endswith(".mp3"):
        return (401, 403, 404, 422, 503)
    if (
        path.endswith(".webp")
        or path.endswith("/image")
        or "/images/" in path
        or "/people/" in path
        or (path.endswith("/avatar") and method == "GET")
    ):
        return (401, 403, 404)
    if path.startswith("/api/admin/"):
        return (400, 401, 403, 404, 409)
    if path.startswith("/api/syncplay/"):
        return (400, 401, 403, 404, 409)
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        return (400, 401, 403, 404, 409)
    return (400, 401, 404)


def _apply_operation_contract(
    operation: dict[str, Any], path: str, method: str
) -> None:
    method = method.upper()
    tag = _tag_for_path(path)
    summary = _SUMMARY_OVERRIDES.get((method, path), f"{method.title()} {path}")
    operation["operationId"] = _stable_operation_id(method, path)
    operation["summary"] = summary
    operation["description"] = _description_for(tag, method, path, summary)
    operation["tags"] = [tag]
    operation["security"] = _security_for(path, method)
    _annotate_parameters(operation, path)

    if (
        method in {"POST", "PUT", "PATCH", "DELETE"}
        and (method, path) not in _NO_REQUEST_BODY
    ):
        model = _REQUEST_MODELS.get((method, path), FlexibleObject)
        body: dict[str, Any] = {
            "required": False,
            "content": _json_content(model, _REQUEST_EXAMPLES.get(model)),
        }
        if path == "/api/account/avatar":
            body = {
                "required": True,
                "description": "Raw JPEG, PNG, WebP, or GIF bytes. The crop query parameters describe the transform.",
                "content": {
                    media_type: {"schema": {"type": "string", "format": "binary"}}
                    for media_type in (
                        "image/jpeg",
                        "image/png",
                        "image/webp",
                        "image/gif",
                    )
                },
            }
        operation["requestBody"] = body

    responses = _response_for(path, method)
    responses.update(_error_responses(_error_statuses(path, method)))
    if "422" in responses and path not in {"/api/account/avatar"}:
        responses["422"] = _json_response(
            ValidationErrorResponse,
            "Request validation failed.",
            {
                "detail": [
                    {
                        "loc": ["query", "page"],
                        "msg": "Input should be greater than or equal to 1",
                        "type": "greater_than_equal",
                    }
                ]
            },
        )
    operation["responses"] = responses


def _iter_api_routes(app: FastAPI):
    """Yield routes from FastAPI's lazy included-router representation."""

    def visit(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
                continue
            nested = getattr(route, "original_router", None)
            if nested is not None:
                yield from visit(nested.routes)

    yield from visit(app.routes)


def configure_routes(app: FastAPI) -> None:
    """Apply route metadata before FastAPI builds its base OpenAPI document."""

    for route in _iter_api_routes(app):
        if route.path in DOCUMENTATION_EXCLUDED_PATHS:
            route.include_in_schema = False
            continue
        methods = sorted(route.methods or ())
        method = next(
            (value for value in methods if value != "HEAD"),
            methods[0] if methods else "GET",
        )
        tag = _tag_for_path(route.path)
        summary = _SUMMARY_OVERRIDES.get(
            (method, route.path), f"{method.title()} {route.path}"
        )
        route.tags = [tag]
        route.summary = summary
        route.description = _description_for(tag, method, route.path, summary)
        route.operation_id = _stable_operation_id(method, route.path)


def _install_security_schemes(components: dict[str, Any]) -> None:
    components["securitySchemes"] = {
        "UserBearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque session token",
            "description": "Regular-user bearer session sent as `Authorization: Bearer <session>`.",
        },
        "UserSessionCookie": {
            "type": "apiKey",
            "in": "cookie",
            "name": "__Host-zenstream-session",
            "description": "HttpOnly regular-user browser cookie. Loopback HTTP uses the port-scoped zenstream-session-<api-port> name.",
        },
        "ResourceTicket": {
            "type": "apiKey",
            "in": "query",
            "name": "access",
            "description": "Short-lived resource ticket for media URLs that cannot send an Authorization header.",
        },
        "ArtworkTicket": {
            "type": "apiKey",
            "in": "query",
            "name": "access",
            "description": "Short-lived session-bound artwork capability for private image URLs.",
        },
        "SocketTicket": {
            "type": "apiKey",
            "in": "query",
            "name": "ticket",
            "description": "Short-lived ticket issued by /api/auth/socket-ticket for WebSocket connections.",
        },
        "AdminSessionCookie": {
            "type": "apiKey",
            "in": "cookie",
            "name": "__Host-zenstream-admin",
            "description": "HttpOnly administrator session cookie. Loopback HTTP uses zenstream-admin-session.",
        },
        "AdminTokenHeader": {
            "type": "apiKey",
            "in": "header",
            "name": "TOKEN",
            "description": "Legacy administrator session token header; use with Username.",
        },
        "AdminUsernameHeader": {
            "type": "apiKey",
            "in": "header",
            "name": "Username",
            "description": "Legacy administrator identity header; use with TOKEN.",
        },
    }


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """Build and return the complete, cached-schema-compatible contract."""

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate Operation ID")
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=OPENAPI_DESCRIPTION,
            routes=app.routes,
            tags=OPENAPI_TAGS,
            openapi_version="3.1.0",
        )
    paths = schema.setdefault("paths", {})
    for path in tuple(paths):
        if path in DOCUMENTATION_EXCLUDED_PATHS:
            paths.pop(path, None)
            continue
        for method, operation in list(paths[path].items()):
            if method.lower() not in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
            }:
                continue
            _apply_operation_contract(operation, path, method.upper())
    components = schema.setdefault("components", {})
    _register_models(components)
    _install_security_schemes(components)
    schema["tags"] = deepcopy(OPENAPI_TAGS)
    schema["x-zenstream-websockets"] = deepcopy(REALTIME_CHANNELS)
    schema["x-zenstream-documentation"] = {
        "excludedStaticPaths": sorted(DOCUMENTATION_EXCLUDED_PATHS),
        "requestParsing": "Documentation-only metadata; handlers retain raw Request parsing.",
        "pagination": "page is one-based; pageSize and limit are endpoint-bounded.",
        "catalogViews": ["full", "card"],
    }
    return schema


def install_openapi(app: FastAPI) -> None:
    """Install route metadata and a FastAPI-compatible custom schema factory."""

    configure_routes(app)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build_openapi(app)
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
