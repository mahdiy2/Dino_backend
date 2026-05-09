import os
import uuid
import time
import mimetypes
import base64
import json
import math
from datetime import datetime, timedelta, timezone
from flask import current_app, has_app_context, has_request_context, request
from minio.error import S3Error
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import selectinload

from app.extensions import redis_client as redis_backend
from app.extensions.minio_client import get_minio_client
from app.models.comment_model import Comment
from app.models.follow_model import Follow
from app.models.media_model import Media
from app.models.profile_model import Profile
from app.models.post_model import Post
from app.models.playlist_track_model import PlaylistTrack
from app.models.report_model import PostReport
from app.models.user_model import User
from app.models.vote_model import Vote
from app.repositories.post_repository import create_post_by_username
from app.repositories.media_repository import add_media
from app.repositories import user_repository
from app.services import block_service
from app.services import async_task_service
from app.services.media_security import (
    is_blocked_declared_mimetype,
    normalize_mimetype,
    validate_upload_content,
)
from app.db import db

MAX_MEDIA_FILES = 8
MAX_VIDEO_DURATION_SECONDS = 30 * 60
MUSIC_TITLE_FALLBACK = "Music track"
VIDEO_MIME_TYPES_WITH_RELIABLE_DURATION = {
    "video/mp4",
    "video/quicktime",
}
POST_UPLOAD_IN_PROGRESS_ERROR = "Another post upload is already in progress"
POST_UPLOAD_IDEMPOTENCY_KEY_PREFIX = "post_upload:idempotency"
POST_UPLOAD_IDEMPOTENCY_RESULT_TTL_SECONDS = 24 * 60 * 60
POST_UPLOAD_IDEMPOTENCY_PROCESSING_TTL_SECONDS = 30 * 60
POST_UPLOAD_IDEMPOTENCY_WAIT_SECONDS = 15.0
POST_UPLOAD_IDEMPOTENCY_POLL_INTERVAL_SECONDS = 0.25
FEED_MODE_MIXED = "mixed"
FEED_MODE_MUSIC = "music"
FEED_MODE_FOLLOWERS = "followers"
MUSIC_RANKING_WINDOW_HOURS = 24
FEED_MODE_ALIASES = {
    FEED_MODE_MIXED: FEED_MODE_MIXED,
    "global": FEED_MODE_MIXED,
    "all": FEED_MODE_MIXED,
    FEED_MODE_MUSIC: FEED_MODE_MUSIC,
    "music_only": FEED_MODE_MUSIC,
    FEED_MODE_FOLLOWERS: FEED_MODE_FOLLOWERS,
    "followers_only": FEED_MODE_FOLLOWERS,
}


class MediaStorageError(Exception):
    pass


class ConcurrentPostUploadError(Exception):
    pass


def _is_media_not_found(error: S3Error) -> bool:
    return error.code in {"NoSuchKey", "NoSuchBucket", "NoSuchObject"}


def _delete_media_object(object_name: str | None):
    if not object_name:
        return

    if object_name.startswith("static/"):
        relative_path = object_name[len("static/"):]
        absolute_path = os.path.join(current_app.static_folder, relative_path)
        if os.path.isfile(absolute_path):
            os.remove(absolute_path)
        return

    bucket = current_app.config["MINIO_BUCKET"]
    minio = get_minio_client()
    try:
        minio.remove_object(
            bucket_name=bucket,
            object_name=object_name,
        )
    except S3Error as e:
        if _is_media_not_found(e):
            return
        raise


def _build_media_url(object_name: str) -> str:
    base_url = current_app.config.get("APP_PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url and has_request_context():
        base_url = request.url_root.rstrip("/")

    if object_name.startswith("static/"):
        if base_url:
            return f"{base_url}/{object_name}"
        return f"/{object_name}"

    if base_url:
        return f"{base_url}/media/{object_name}"
    return f"/media/{object_name}"


def _build_author_maps(author_ids: set[int]):
    if not author_ids:
        return {}, {}

    user_rows = (
        db.session.query(User.id, User.username, User.badge)
        .filter(User.id.in_(author_ids))
        .all()
    )
    profile_rows = (
        db.session.query(
            Profile.user_id,
            Profile.name,
            Profile.image_object_name,
            Profile.profile_image_shape,
        )
        .filter(Profile.user_id.in_(author_ids))
        .all()
    )

    user_by_id = {
        user_id: {
            "id": user_id,
            "username": username,
            "badge": badge,
        }
        for user_id, username, badge in user_rows
    }
    profile_by_user_id = {
        user_id: {
            "user_id": user_id,
            "name": name,
            "image_object_name": image_object_name,
            "profile_image_shape": profile_image_shape or "circle",
        }
        for user_id, name, image_object_name, profile_image_shape in profile_rows
    }
    return user_by_id, profile_by_user_id


def _serialize_author(author_id: int, user_by_id: dict, profile_by_user_id: dict):
    user = user_by_id.get(author_id)
    profile = profile_by_user_id.get(author_id)

    username = user["username"] if user else f"user-{author_id}"
    name = profile["name"] if profile else username

    profile_image_url = None
    if profile and profile.get("image_object_name"):
        profile_image_url = _build_media_url(profile["image_object_name"])

    return {
        "id": author_id,
        "username": username,
        "name": name,
        "badge": user.get("badge") if user else None,
        "profile_image_url": profile_image_url,
        "profile_image_shape": (
            profile.get("profile_image_shape", "circle") if profile else "circle"
        ),
    }


def _build_playlist_adders_by_media(posts: list[Post]):
    audio_media_ids = {
        media.id
        for post in posts
        for media in post.media
        if _is_audio_mimetype(media.mime_type)
    }
    if not audio_media_ids:
        return {}

    rows = (
        db.session.query(
            PlaylistTrack.media_id,
            User.id,
            User.username,
            User.badge,
            Profile.name,
            Profile.image_object_name,
            Profile.profile_image_shape,
            PlaylistTrack.created_at,
            PlaylistTrack.id,
        )
        .join(User, User.id == PlaylistTrack.user_id)
        .outerjoin(Profile, Profile.user_id == User.id)
        .filter(
            PlaylistTrack.media_id.in_(audio_media_ids),
            User.is_suspended.is_(False),
        )
        .order_by(
            PlaylistTrack.media_id.asc(),
            PlaylistTrack.created_at.desc(),
            PlaylistTrack.id.desc(),
        )
        .all()
    )

    adders_by_media_id: dict[int, list[dict]] = {}
    for media_id, user_id, username, badge, name, image_object_name, profile_image_shape, _, _ in rows:
        profile_image_url = _build_media_url(image_object_name) if image_object_name else None
        adders = adders_by_media_id.setdefault(media_id, [])
        adders.append(
            {
                "id": user_id,
                "username": username,
                "name": (name or "").strip() or username,
                "badge": badge,
                "profile_image_url": profile_image_url,
                "profile_image_shape": profile_image_shape or "circle",
            }
        )

    return adders_by_media_id


def _serialize_media_item(
    media: Media,
    playlist_adders_by_media_id: dict[int, list[dict]] | None = None,
):
    payload = {
        "id": media.id,
        "url": _build_media_url(media.object_name),
        "mime_type": media.mime_type,
        "display_name": media.display_name,
        "title": media.title,
        "artist": media.artist,
    }
    if playlist_adders_by_media_id is not None:
        payload["playlist_adders"] = playlist_adders_by_media_id.get(media.id, [])
    return payload


def _serialize_post_preview(
    post: Post,
    user_by_id: dict,
    profile_by_user_id: dict,
):
    daily_winner_at = getattr(post, "daily_winner_at", None)
    return {
        "id": post.id,
        "text": post.text,
        "author": _serialize_author(post.author_id, user_by_id, profile_by_user_id),
        "created_at": post.created_at.isoformat(),
        "is_daily_winner": bool(getattr(post, "is_daily_winner", False)),
        "was_daily_winner": daily_winner_at is not None,
        "daily_winner_at": (
            daily_winner_at.isoformat()
            if daily_winner_at is not None
            else None
        ),
        "media": [
            _serialize_media_item(media)
            for media in post.media
        ],
    }


def _build_visible_quoted_posts(
    posts: list[Post],
    viewer_user_id: int | None,
    hidden_user_ids: set[int] | None = None,
):
    quoted_post_ids = {
        int(post.quoted_post_id)
        for post in posts
        if getattr(post, "quoted_post_id", None)
    }
    if not quoted_post_ids:
        return {}

    query = (
        Post.query
        .join(User, User.id == Post.author_id)
        .filter(
            Post.id.in_(quoted_post_ids),
            Post.is_hidden.is_(False),
            User.is_suspended.is_(False),
            _post_visibility_filter(viewer_user_id),
        )
        .options(selectinload(Post.media))
    )
    if hidden_user_ids:
        query = query.filter(~Post.author_id.in_(hidden_user_ids))

    return {post.id: post for post in query.all()}


def _serialize_post(
    post,
    user_by_id: dict,
    profile_by_user_id: dict,
    playlist_adders_by_media_id: dict[int, list[dict]] | None = None,
    quoted_posts_by_id: dict[int, Post] | None = None,
):
    author_payload = _serialize_author(post.author_id, user_by_id, profile_by_user_id)
    adders_by_media_id = playlist_adders_by_media_id or {}
    quoted_post_id = getattr(post, "quoted_post_id", None)
    daily_winner_at = getattr(post, "daily_winner_at", None)
    quoted_post = (
        (quoted_posts_by_id or {}).get(quoted_post_id)
        if quoted_post_id is not None
        else None
    )

    return {
        "id": post.id,
        "text": post.text,
        "author": author_payload,
        "created_at": post.created_at.isoformat(),
        "is_daily_winner": bool(getattr(post, "is_daily_winner", False)),
        "was_daily_winner": daily_winner_at is not None,
        "daily_winner_at": (
            daily_winner_at.isoformat()
            if daily_winner_at is not None
            else None
        ),
        "viewer_vote": 0,
        "quoted_post_id": quoted_post_id,
        "quoted_post": (
            _serialize_post_preview(
                quoted_post,
                user_by_id,
                profile_by_user_id,
            )
            if quoted_post is not None
            else None
        ),
        "media": [
            _serialize_media_item(
                media,
                playlist_adders_by_media_id=adders_by_media_id,
            )
            for media in post.media
        ]
    }


def _viewer_user_id(viewer_username: str | None) -> int | None:
    if not viewer_username:
        return None

    viewer_user = user_repository.get_by_username(viewer_username)
    if not viewer_user or getattr(viewer_user, "is_suspended", False):
        return None

    return viewer_user.id


def _post_visibility_filter(viewer_user_id: int | None):
    if viewer_user_id is None:
        return Post.followers_only.is_(False)

    follower_relation_exists = (
        db.session.query(Follow.id)
        .filter(
            Follow.follower_id == viewer_user_id,
            Follow.following_id == Post.author_id,
        )
        .exists()
    )

    return or_(
        Post.followers_only.is_(False),
        Post.author_id == viewer_user_id,
        follower_relation_exists,
    )


def _build_vote_map(post_ids: set[int], viewer_user_id: int | None):
    if not post_ids or not viewer_user_id:
        return {}

    votes = (
        Vote.query
        .filter(
            Vote.user_id == viewer_user_id,
            Vote.target_type == "post",
            Vote.target_id.in_(post_ids),
        )
        .all()
    )
    return {vote.target_id: vote.value for vote in votes}


def _log_feed_timing(
    *,
    endpoint: str,
    page: int,
    limit: int,
    include_total: bool,
    rows: int,
    started_at: float,
):
    if not has_app_context():
        return

    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    threshold_ms = int(current_app.config.get("QUERY_TIMING_LOG_SLOW_MS", 150))
    level = "info" if elapsed_ms >= threshold_ms else "debug"
    log_fn = current_app.logger.info if level == "info" else current_app.logger.debug
    log_fn(
        "feed_query endpoint=%s page=%s limit=%s include_total=%s rows=%s duration_ms=%s",
        endpoint,
        page,
        limit,
        include_total,
        rows,
        elapsed_ms,
    )


def _extension_for_mimetype(mimetype: str) -> str:
    mapping = {
        "image/jpeg": "jpeg",
        "image/png": "png",
        "image/webp": "webp",
        "video/mp4": "mp4",
        "video/quicktime": "mov",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/mp4": "m4a",
        "audio/x-m4a": "m4a",
        "audio/aac": "aac",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/ogg": "ogg",
        "audio/flac": "flac",
        "audio/webm": "webm",
    }
    return mapping.get(mimetype, mimetype.split("/")[-1])


def _normalize_mimetype(raw_mimetype: str | None) -> str:
    return normalize_mimetype(raw_mimetype)


def _guess_mimetype_from_filename(file_name: str | None) -> str:
    guessed, _ = mimetypes.guess_type((file_name or "").strip(), strict=False)
    return _normalize_mimetype(guessed)


def _build_post_idempotency_key_prefix(username: str, idempotency_key: str) -> str:
    prefix = (current_app.config.get("POST_UPLOAD_IDEMPOTENCY_KEY_PREFIX") or "").strip()
    if not prefix:
        prefix = POST_UPLOAD_IDEMPOTENCY_KEY_PREFIX
    return f"{prefix}:{username}:{idempotency_key}"


def _build_post_idempotency_result_key(username: str, idempotency_key: str) -> str:
    return f"{_build_post_idempotency_key_prefix(username, idempotency_key)}:result"


def _build_post_idempotency_processing_key(username: str, idempotency_key: str) -> str:
    return f"{_build_post_idempotency_key_prefix(username, idempotency_key)}:processing"


def _normalize_post_idempotency_key(idempotency_key: str | None) -> str | None:
    if idempotency_key is None:
        return None
    if not isinstance(idempotency_key, str):
        raise ValueError("idempotency_key must be a string")

    normalized = idempotency_key.strip()
    if not normalized:
        return None
    if len(normalized) > 128:
        raise ValueError("idempotency_key must be at most 128 characters")

    for ch in normalized:
        if ord(ch) < 33 or ord(ch) > 126:
            raise ValueError("idempotency_key contains unsupported characters")
    return normalized


def _read_idempotent_post_id(username: str, idempotency_key: str) -> int | None:
    redis_client = redis_backend.redis_client
    key = _build_post_idempotency_result_key(username, idempotency_key)
    try:
        value = redis_client.get(key)
    except Exception:
        return None

    if value in {None, ""}:
        return None

    try:
        post_id = int(value)
    except (TypeError, ValueError):
        return None
    if post_id <= 0:
        return None
    return post_id


def _acquire_post_idempotency_lock(username: str, idempotency_key: str):
    redis_client = redis_backend.redis_client
    processing_key = _build_post_idempotency_processing_key(username, idempotency_key)
    token = uuid.uuid4().hex
    processing_ttl_seconds = max(
        60,
        int(
            current_app.config.get(
                "POST_UPLOAD_IDEMPOTENCY_PROCESSING_TTL_SECONDS",
                POST_UPLOAD_IDEMPOTENCY_PROCESSING_TTL_SECONDS,
            )
        ),
    )
    wait_seconds = max(
        0.0,
        float(
            current_app.config.get(
                "POST_UPLOAD_IDEMPOTENCY_WAIT_SECONDS",
                POST_UPLOAD_IDEMPOTENCY_WAIT_SECONDS,
            )
        ),
    )
    poll_interval_seconds = max(
        0.05,
        float(
            current_app.config.get(
                "POST_UPLOAD_IDEMPOTENCY_POLL_INTERVAL_SECONDS",
                POST_UPLOAD_IDEMPOTENCY_POLL_INTERVAL_SECONDS,
            )
        ),
    )

    try:
        acquired = bool(
            redis_client.set(
                processing_key,
                token,
                nx=True,
                ex=processing_ttl_seconds,
            )
        )
    except Exception:
        if has_app_context():
            current_app.logger.warning(
                "post_idempotency_lock_unavailable username=%s",
                username,
            )
        return None

    if acquired:
        return processing_key, token

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        replay_post_id = _read_idempotent_post_id(username, idempotency_key)
        if replay_post_id is not None:
            return replay_post_id
        time.sleep(poll_interval_seconds)

    raise ConcurrentPostUploadError(POST_UPLOAD_IN_PROGRESS_ERROR)


def _release_lock(lock_handle, *, failure_log_label: str):
    if not lock_handle:
        return
    redis_client = redis_backend.redis_client
    key, token = lock_handle
    try:
        release_script = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
        redis_client.eval(release_script, 1, key, token)
    except Exception:
        try:
            if redis_client.get(key) == token:
                redis_client.delete(key)
        except Exception:
            if has_app_context():
                current_app.logger.warning(
                    "%s key=%s",
                    failure_log_label,
                    key,
                )


def _release_post_idempotency_lock(lock_handle):
    _release_lock(lock_handle, failure_log_label="post_idempotency_lock_release_failed")


def _store_idempotent_post_id(username: str, idempotency_key: str, post_id: int):
    redis_client = redis_backend.redis_client
    result_key = _build_post_idempotency_result_key(username, idempotency_key)
    result_ttl_seconds = max(
        300,
        int(
            current_app.config.get(
                "POST_UPLOAD_IDEMPOTENCY_RESULT_TTL_SECONDS",
                POST_UPLOAD_IDEMPOTENCY_RESULT_TTL_SECONDS,
            )
        ),
    )
    try:
        redis_client.setex(result_key, result_ttl_seconds, str(int(post_id)))
    except Exception:
        if has_app_context():
            current_app.logger.warning(
                "post_idempotency_result_store_failed username=%s post_id=%s",
                username,
                post_id,
            )


def _is_blocked_media_mimetype(mimetype: str) -> bool:
    return is_blocked_declared_mimetype(mimetype)


def _is_image_mimetype(mimetype: str) -> bool:
    normalized = _normalize_mimetype(mimetype)
    return normalized.startswith("image/") and not _is_blocked_media_mimetype(normalized)


def _is_video_mimetype(mimetype: str) -> bool:
    normalized = _normalize_mimetype(mimetype)
    return normalized.startswith("video/")


def _is_audio_mimetype(mimetype: str) -> bool:
    normalized = _normalize_mimetype(mimetype)
    return normalized.startswith("audio/")


def _normalize_music_text(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _split_track_metadata_from_display_name(display_name: str | None):
    base_name = os.path.splitext((display_name or "").strip())[0].strip()
    if not base_name:
        return None, None

    for separator in (" - ", " \u2013 ", " \u2014 "):
        if separator not in base_name:
            continue
        left, right = base_name.split(separator, 1)
        candidate_artist = left.strip() or None
        candidate_title = right.strip() or None
        if candidate_artist and candidate_title:
            return candidate_artist, candidate_title

    return None, base_name


def _build_audio_metadata(
    file_name: str | None,
    track_title: str | None = None,
    track_artist: str | None = None,
):
    display_name = (file_name or "").strip()

    parsed_artist, parsed_title = _split_track_metadata_from_display_name(display_name)
    normalized_title = _normalize_music_text(track_title) or parsed_title
    normalized_artist = _normalize_music_text(track_artist) or parsed_artist

    if normalized_title is None:
        if display_name:
            normalized_title = os.path.splitext(display_name)[0].strip() or display_name
        else:
            normalized_title = MUSIC_TITLE_FALLBACK

    if normalized_artist and normalized_artist.lower() == normalized_title.lower():
        normalized_artist = None

    return (display_name or None), normalized_title, normalized_artist


def _get_mp4_duration_seconds(file_storage):
    stream = getattr(file_storage, "stream", file_storage)

    try:
        original_pos = stream.tell()
    except Exception:
        original_pos = None

    try:
        try:
            stream.seek(0, 2)
            file_end = stream.tell()
            stream.seek(0)
        except Exception:
            return None

        def read_exact(num_bytes: int) -> bytes:
            data = stream.read(num_bytes)
            if len(data) != num_bytes:
                raise EOFError
            return data

        def walk_boxes(start: int, end: int) -> float | None:
            pos = start
            while pos + 8 <= end:
                stream.seek(pos)
                header = stream.read(8)
                if len(header) < 8:
                    return None

                size = int.from_bytes(header[0:4], "big")
                box_type = header[4:8]
                header_size = 8

                if size == 1:
                    try:
                        size = int.from_bytes(read_exact(8), "big")
                    except EOFError:
                        return None
                    header_size = 16
                elif size == 0:
                    size = end - pos

                if size < header_size:
                    return None

                box_end = pos + size
                if box_end > end:
                    return None

                payload_start = pos + header_size

                if box_type == b"moov":
                    duration = walk_boxes(payload_start, box_end)
                    if duration is not None:
                        return duration
                elif box_type == b"mvhd":
                    stream.seek(payload_start)
                    try:
                        version_flags = read_exact(4)
                    except EOFError:
                        return None

                    version = version_flags[0]
                    if version == 0:
                        try:
                            _ = read_exact(8)  # creation/modification
                            timescale = int.from_bytes(read_exact(4), "big")
                            duration = int.from_bytes(read_exact(4), "big")
                        except EOFError:
                            return None
                    elif version == 1:
                        try:
                            _ = read_exact(16)  # creation/modification
                            timescale = int.from_bytes(read_exact(4), "big")
                            duration = int.from_bytes(read_exact(8), "big")
                        except EOFError:
                            return None
                    else:
                        return None

                    if timescale <= 0:
                        return None
                    return duration / timescale

                pos = box_end
            return None

        return walk_boxes(0, file_end)
    finally:
        if original_pos is not None:
            try:
                stream.seek(original_pos)
            except Exception:
                pass


def _get_stream_and_length(file_storage):
    stream = getattr(file_storage, "stream", file_storage)
    try:
        stream.seek(0, 2)
        length = stream.tell()
        stream.seek(0)
        return stream, length
    except Exception:
        try:
            stream.seek(0)
        except Exception:
            pass
        return stream, -1


def _store_media_locally(file_storage, post_id: int, extension: str) -> str:
    filename = f"{uuid.uuid4()}.{extension}"
    relative_parts = ["uploads", "posts", str(post_id), filename]
    relative_path = os.path.join(*relative_parts)
    absolute_path = os.path.join(current_app.static_folder, relative_path)
    os.makedirs(os.path.dirname(absolute_path), exist_ok=True)

    try:
        file_storage.stream.seek(0)
    except Exception:
        pass

    file_storage.save(absolute_path)
    return "static/" + "/".join(relative_parts)


def _resolve_visible_quoted_post_for_author(
    quoted_post_id: int | None,
    author_username: str,
):
    if quoted_post_id is None:
        return None

    viewer_user_id = _viewer_user_id(author_username)
    hidden_user_ids = block_service.hidden_user_ids_for_viewer(author_username)
    query = (
        Post.query
        .join(User, User.id == Post.author_id)
        .filter(
            Post.id == quoted_post_id,
            Post.is_hidden.is_(False),
            User.is_suspended.is_(False),
            _post_visibility_filter(viewer_user_id),
        )
    )
    if hidden_user_ids:
        query = query.filter(~Post.author_id.in_(hidden_user_ids))

    quoted_post = query.first()
    if not quoted_post:
        raise ValueError("Quoted post not found")
    return quoted_post


def create_post_with_media(
    username,
    text,
    files,
    followers_only: bool = False,
    track_title: str | None = None,
    track_artist: str | None = None,
    quoted_post_id: int | None = None,
    idempotency_key: str | None = None,
):
    normalized_idempotency_key = _normalize_post_idempotency_key(idempotency_key)
    idempotency_lock_handle = None
    if normalized_idempotency_key:
        replay_post_id = _read_idempotent_post_id(username, normalized_idempotency_key)
        if replay_post_id is not None:
            return {"post_id": replay_post_id}
        acquired_idempotency_lock = _acquire_post_idempotency_lock(
            username,
            normalized_idempotency_key,
        )
        if isinstance(acquired_idempotency_lock, int):
            return {"post_id": acquired_idempotency_lock}
        idempotency_lock_handle = acquired_idempotency_lock

    try:
        files = files or []
        if len(files) > MAX_MEDIA_FILES:
            raise ValueError("Maximum 8 media files allowed")

        normalized_files = []
        for file in files:
            if not getattr(file, "filename", ""):
                raise ValueError("Media file is required")
            declared_mimetype = _normalize_mimetype(getattr(file, "mimetype", ""))
            guessed_mimetype = _guess_mimetype_from_filename(file.filename)
            mimetype = declared_mimetype
            if (
                mimetype in {"", "application/octet-stream"}
                and guessed_mimetype.startswith(("audio/", "image/", "video/"))
            ):
                mimetype = guessed_mimetype

            if bool(current_app.config.get("MEDIA_CONTENT_SNIFFING_ENABLED", True)):
                content_validation_error = validate_upload_content(
                    file,
                    mimetype,
                    allowed_categories={"image", "video", "audio"},
                    reject_active_text_payloads=bool(
                        current_app.config.get("MEDIA_CONTENT_REJECT_ACTIVE_TEXT", True)
                    ),
                    enforce_declared_category_match=bool(
                        current_app.config.get("MEDIA_CONTENT_ENFORCE_CATEGORY_MATCH", True)
                    ),
                    sniff_bytes=int(current_app.config.get("MEDIA_CONTENT_SNIFF_BYTES", 2048)),
                )
                if content_validation_error in {"unsupported_declared_type", "unsupported_detected_type"}:
                    raise ValueError(f"Unsupported media type: {mimetype or 'unknown'}")
                if content_validation_error in {"blocked_active_content", "declared_type_mismatch"}:
                    raise ValueError("Invalid media content for declared type")
                if content_validation_error is not None:
                    raise ValueError("Could not validate media file")
            normalized_files.append((file, file.filename, mimetype))

        has_audio_file = any(_is_audio_mimetype(mimetype) for _, _, mimetype in normalized_files)
        cleaned_text = text.strip() if isinstance(text, str) else ""

        if has_audio_file:
            if len(normalized_files) != 1:
                raise ValueError("Music posts can contain only one audio file")

            _, _, mimetype = normalized_files[0]
            if not _is_audio_mimetype(mimetype):
                raise ValueError(f"Unsupported media type: {mimetype}")
        elif cleaned_text == "":
            raise ValueError("Text is required")

        quoted_post = _resolve_visible_quoted_post_for_author(
            quoted_post_id=quoted_post_id,
            author_username=username,
        )

        validated_files = []
        for file, file_name, mimetype in normalized_files:
            if has_audio_file:
                extension = _extension_for_mimetype(mimetype)
                display_name, title, artist = _build_audio_metadata(
                    file_name=file_name,
                    track_title=track_title,
                    track_artist=track_artist,
                )
                validated_files.append(
                    (
                        file,
                        mimetype,
                        extension,
                        display_name,
                        title,
                        artist,
                    )
                )
                continue

            if _is_video_mimetype(mimetype):
                if mimetype in VIDEO_MIME_TYPES_WITH_RELIABLE_DURATION:
                    duration_seconds = _get_mp4_duration_seconds(file)
                    if duration_seconds is None:
                        raise ValueError("Could not determine video duration")
                    if duration_seconds > MAX_VIDEO_DURATION_SECONDS:
                        raise ValueError("Video must be 30 minutes or shorter")
            elif not _is_image_mimetype(mimetype):
                raise ValueError(f"Unsupported media type: {mimetype}")

            extension = _extension_for_mimetype(mimetype)
            validated_files.append((file, mimetype, extension, None, None, None))

        post = create_post_by_username(
            username,
            cleaned_text,
            followers_only=followers_only,
            quoted_post_id=(quoted_post.id if quoted_post is not None else None),
        )

        media_post_process_items = []
        if validated_files:
            minio = None
            bucket = current_app.config["MINIO_BUCKET"]
            local_fallback_enabled = bool(
                current_app.config.get("MEDIA_LOCAL_FALLBACK_ENABLED", True)
            )
            use_local_storage = False

            try:
                minio = get_minio_client()
                bucket_exists = minio.bucket_exists(bucket)
                if not bucket_exists:
                    minio.make_bucket(bucket)
            except Exception as e:
                if not local_fallback_enabled:
                    db.session.rollback()
                    raise MediaStorageError("Media storage is unavailable") from e
                use_local_storage = True

            for file, mimetype, extension, display_name, title, artist in validated_files:
                object_name = None

                if not use_local_storage:
                    try:
                        object_name = f"posts/{post.id}/{uuid.uuid4()}.{extension}"
                        stream, length = _get_stream_and_length(file)
                        upload_kwargs = {
                            "bucket_name": bucket,
                            "object_name": object_name,
                            "data": stream,
                            "length": length,
                            "content_type": mimetype,
                        }
                        if length == -1:
                            upload_kwargs["part_size"] = 10 * 1024 * 1024

                        minio.put_object(**upload_kwargs)
                    except Exception as e:
                        if not local_fallback_enabled:
                            db.session.rollback()
                            raise MediaStorageError("Media storage is unavailable") from e
                        use_local_storage = True

                if use_local_storage:
                    try:
                        object_name = _store_media_locally(file, post.id, extension)
                    except Exception as e:
                        db.session.rollback()
                        raise MediaStorageError("Media storage is unavailable") from e

                add_media(
                    post_id=post.id,
                    object_name=object_name,
                    mime_type=mimetype,
                    display_name=display_name,
                    title=title,
                    artist=artist,
                )
                media_post_process_items.append(
                    {
                        "object_name": object_name,
                        "mime_type": mimetype,
                    }
                )

        db.session.commit()
        if normalized_idempotency_key:
            _store_idempotent_post_id(username, normalized_idempotency_key, post.id)
        if media_post_process_items:
            async_task_service.enqueue_media_post_process_task(
                post_id=post.id,
                media_items=media_post_process_items,
                source="post_service.create_post_with_media",
            )
        return {"post_id": post.id}
    finally:
        _release_post_idempotency_lock(idempotency_lock_handle)


def _serialize_datetime_for_cursor(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def _parse_datetime_cursor(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Invalid feed cursor")
    normalized = value.strip()
    if not normalized:
        raise ValueError("Invalid feed cursor")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _encode_feed_cursor(payload: dict | None) -> str | None:
    if not payload:
        return None
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    encoded = base64.urlsafe_b64encode(serialized.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def _decode_feed_cursor(cursor: str | None) -> dict:
    if not cursor:
        return {}

    if not isinstance(cursor, str):
        raise ValueError("Invalid feed cursor")

    normalized = cursor.strip()
    if not normalized:
        return {}

    padding = "=" * (-len(normalized) % 4)
    try:
        decoded = base64.urlsafe_b64decode(normalized + padding).decode("utf-8")
        payload = json.loads(decoded)
    except Exception as exc:
        raise ValueError("Invalid feed cursor") from exc

    if not isinstance(payload, dict):
        raise ValueError("Invalid feed cursor")
    return payload


def _normalize_feed_mode(mode: str | None, *, music_filter: bool = False) -> str:
    if music_filter:
        return FEED_MODE_MUSIC

    candidate = (mode or FEED_MODE_MIXED).strip().lower()
    normalized = FEED_MODE_ALIASES.get(candidate)
    if normalized is None:
        raise ValueError("mode must be one of: mixed, music, followers")
    return normalized


def _music_post_exists_filter():
    return (
        db.session.query(Media.id)
        .filter(
            Media.post_id == Post.id,
            Media.mime_type.like("audio/%"),
        )
        .exists()
    )


def _apply_chronological_cursor(query, cursor_payload: dict | None):
    if not cursor_payload:
        return query

    try:
        cursor_id = int(cursor_payload["id"])
        cursor_created_at = _parse_datetime_cursor(cursor_payload["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid feed cursor") from exc

    return query.filter(
        or_(
            Post.created_at < cursor_created_at,
            and_(
                Post.created_at == cursor_created_at,
                Post.id < cursor_id,
            ),
        )
    )


def _load_posts_in_order(post_ids: list[int]):
    if not post_ids:
        return []

    posts = (
        Post.query
        .filter(Post.id.in_(post_ids))
        .options(selectinload(Post.media))
        .all()
    )
    posts_by_id = {post.id: post for post in posts}
    return [posts_by_id[post_id] for post_id in post_ids if post_id in posts_by_id]


def _fetch_posts_chronological(
    *,
    limit: int,
    viewer_user_id: int | None,
    hidden_user_ids: set[int],
    cursor_payload: dict | None = None,
    music_filter: bool | None = None,
    author_ids_subquery=None,
):
    filter_conditions = [
        Post.is_hidden.is_(False),
        User.is_suspended.is_(False),
        _post_visibility_filter(viewer_user_id),
    ]
    if hidden_user_ids:
        filter_conditions.append(~Post.author_id.in_(hidden_user_ids))
    if author_ids_subquery is not None:
        filter_conditions.append(Post.author_id.in_(author_ids_subquery))

    music_exists = _music_post_exists_filter()
    if music_filter is True:
        filter_conditions.append(music_exists)
    elif music_filter is False:
        filter_conditions.append(~music_exists)

    query = (
        db.session.query(Post.id)
        .join(User, User.id == Post.author_id)
        .filter(*filter_conditions)
        .order_by(Post.created_at.desc(), Post.id.desc())
    )
    query = _apply_chronological_cursor(query, cursor_payload)

    post_ids = [row[0] for row in query.limit(limit).all()]
    posts = _load_posts_in_order(post_ids)
    return posts


def _apply_ranked_music_cursor(query, cursor_payload: dict | None, playlist_count_expr):
    if not cursor_payload:
        return query

    try:
        cursor_id = int(cursor_payload["id"])
        cursor_created_at = _parse_datetime_cursor(cursor_payload["created_at"])
        cursor_playlist_count = int(
            cursor_payload.get(
                "playlist_count_24h",
                cursor_payload["playlist_count"],
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid feed cursor") from exc

    return query.filter(
        or_(
            playlist_count_expr < cursor_playlist_count,
            and_(
                playlist_count_expr == cursor_playlist_count,
                Post.created_at < cursor_created_at,
            ),
            and_(
                playlist_count_expr == cursor_playlist_count,
                Post.created_at == cursor_created_at,
                Post.id < cursor_id,
            ),
        )
    )


def _fetch_music_posts_ranked(
    *,
    limit: int,
    viewer_user_id: int | None,
    hidden_user_ids: set[int],
    cursor_payload: dict | None = None,
    author_ids_subquery=None,
):
    rank_window_start = datetime.utcnow() - timedelta(hours=MUSIC_RANKING_WINDOW_HOURS)
    playlist_counts_subquery = (
        db.session.query(
            Media.post_id.label("post_id"),
            func.count(PlaylistTrack.id).label("playlist_count_24h"),
        )
        .join(Media, Media.id == PlaylistTrack.media_id)
        .filter(PlaylistTrack.created_at >= rank_window_start)
        .group_by(Media.post_id)
        .subquery()
    )

    playlist_count_expr = func.coalesce(
        playlist_counts_subquery.c.playlist_count_24h,
        0,
    )
    filter_conditions = [
        Post.is_hidden.is_(False),
        User.is_suspended.is_(False),
        _post_visibility_filter(viewer_user_id),
        _music_post_exists_filter(),
    ]
    if hidden_user_ids:
        filter_conditions.append(~Post.author_id.in_(hidden_user_ids))
    if author_ids_subquery is not None:
        filter_conditions.append(Post.author_id.in_(author_ids_subquery))

    query = (
        db.session.query(
            Post.id,
            Post.created_at,
            playlist_count_expr.label("playlist_count_24h"),
        )
        .join(User, User.id == Post.author_id)
        .outerjoin(
            playlist_counts_subquery,
            playlist_counts_subquery.c.post_id == Post.id,
        )
        .filter(*filter_conditions)
    )
    query = _apply_ranked_music_cursor(query, cursor_payload, playlist_count_expr)
    rows = (
        query
        .order_by(
            playlist_count_expr.desc(),
            Post.created_at.desc(),
            Post.id.desc(),
        )
        .limit(limit)
        .all()
    )

    post_ids = [row.id for row in rows]
    posts = _load_posts_in_order(post_ids)
    posts_by_id = {post.id: post for post in posts}
    ranked_items = []
    for row in rows:
        post = posts_by_id.get(row.id)
        if post is None:
            continue
        ranked_items.append(
            {
                "post": post,
                "playlist_count_24h": int(row.playlist_count_24h or 0),
            }
        )
    return ranked_items


def _build_chronological_cursor_payload(post: Post) -> dict:
    return {
        "id": int(post.id),
        "created_at": _serialize_datetime_for_cursor(post.created_at),
    }


def _build_ranked_music_cursor_payload(ranked_item: dict) -> dict:
    post = ranked_item["post"]
    playlist_count_24h = int(
        ranked_item.get(
            "playlist_count_24h",
            ranked_item.get("playlist_count", 0),
        )
    )
    return {
        "id": int(post.id),
        "created_at": _serialize_datetime_for_cursor(post.created_at),
        "playlist_count_24h": playlist_count_24h,
        "playlist_count": playlist_count_24h,
    }


def _max_music_slots_for_normals(normal_count: int) -> int:
    if normal_count <= 0:
        return 0
    return int(math.floor((normal_count * 2) / 10))


def _interleave_music_posts(normal_posts: list[Post], music_posts: list[Post]):
    if not music_posts:
        return normal_posts
    if not normal_posts:
        return music_posts

    total_normals = len(normal_posts)
    total_music = len(music_posts)
    insertion_points = [
        ((slot + 1) * (total_normals + 1)) // (total_music + 1)
        for slot in range(total_music)
    ]

    merged = []
    music_index = 0
    for normal_index, post in enumerate(normal_posts, start=1):
        merged.append(post)
        while (
            music_index < total_music
            and insertion_points[music_index] == normal_index
        ):
            merged.append(music_posts[music_index])
            music_index += 1

    while music_index < total_music:
        merged.append(music_posts[music_index])
        music_index += 1
    return merged


def _build_ratio_feed_page(
    *,
    limit: int,
    viewer_user_id: int | None,
    hidden_user_ids: set[int],
    normal_cursor_payload: dict | None = None,
    music_cursor_payload: dict | None = None,
    author_ids_subquery=None,
):
    music_slot_budget = limit // 6
    normal_target = max(1, limit - music_slot_budget)
    normal_posts_with_lookahead = _fetch_posts_chronological(
        limit=normal_target + 1,
        viewer_user_id=viewer_user_id,
        hidden_user_ids=hidden_user_ids,
        cursor_payload=normal_cursor_payload,
        music_filter=False,
        author_ids_subquery=author_ids_subquery,
    )
    normal_has_more = len(normal_posts_with_lookahead) > normal_target
    normal_posts = normal_posts_with_lookahead[:normal_target]

    if not normal_posts:
        ranked_music = _fetch_music_posts_ranked(
            limit=limit + 1,
            viewer_user_id=viewer_user_id,
            hidden_user_ids=hidden_user_ids,
            cursor_payload=music_cursor_payload,
            author_ids_subquery=author_ids_subquery,
        )
        has_more = len(ranked_music) > limit
        page_music = ranked_music[:limit]
        page_posts = [item["post"] for item in page_music]

        next_cursor = None
        if has_more and page_music:
            next_cursor = {
                "music": _build_ranked_music_cursor_payload(page_music[-1])
            }
        return page_posts, has_more, next_cursor

    max_music_slots = _max_music_slots_for_normals(len(normal_posts))
    max_music_slots = min(
        max_music_slots,
        music_slot_budget,
        max(0, limit - len(normal_posts)),
    )

    ranked_music = []
    music_has_more = False
    if max_music_slots > 0:
        ranked_music_with_lookahead = _fetch_music_posts_ranked(
            limit=max_music_slots + 1,
            viewer_user_id=viewer_user_id,
            hidden_user_ids=hidden_user_ids,
            cursor_payload=music_cursor_payload,
            author_ids_subquery=author_ids_subquery,
        )
        music_has_more = len(ranked_music_with_lookahead) > max_music_slots
        ranked_music = ranked_music_with_lookahead[:max_music_slots]

    merged_posts = _interleave_music_posts(
        normal_posts,
        [item["post"] for item in ranked_music],
    )
    page_posts = merged_posts[:limit]
    has_more = normal_has_more or music_has_more

    if not has_more and not normal_has_more and max_music_slots == 0:
        music_probe = _fetch_music_posts_ranked(
            limit=1,
            viewer_user_id=viewer_user_id,
            hidden_user_ids=hidden_user_ids,
            cursor_payload=music_cursor_payload,
            author_ids_subquery=author_ids_subquery,
        )
        has_more = bool(music_probe)

    if not has_more:
        return page_posts, False, None

    next_cursor = {
        "normal": _build_chronological_cursor_payload(normal_posts[-1]),
    }
    if ranked_music:
        next_cursor["music"] = _build_ranked_music_cursor_payload(ranked_music[-1])
    elif music_cursor_payload:
        next_cursor["music"] = music_cursor_payload
    return page_posts, True, next_cursor


def _serialize_feed_posts(
    ordered_posts: list[Post],
    *,
    viewer_user_id: int | None,
    hidden_user_ids: set[int],
):
    if not ordered_posts:
        return []

    quoted_posts_by_id = _build_visible_quoted_posts(
        ordered_posts,
        viewer_user_id=viewer_user_id,
        hidden_user_ids=hidden_user_ids,
    )
    author_ids = {
        post.author_id
        for post in ordered_posts
    } | {
        quoted_post.author_id
        for quoted_post in quoted_posts_by_id.values()
    }
    post_ids = {post.id for post in ordered_posts}
    user_by_id, profile_by_user_id = _build_author_maps(author_ids)
    playlist_adders_by_media_id = _build_playlist_adders_by_media(ordered_posts)
    vote_by_post_id = _build_vote_map(
        post_ids=post_ids,
        viewer_user_id=viewer_user_id,
    )

    serialized_posts = []
    for post in ordered_posts:
        payload = _serialize_post(
            post,
            user_by_id,
            profile_by_user_id,
            playlist_adders_by_media_id=playlist_adders_by_media_id,
            quoted_posts_by_id=quoted_posts_by_id,
        )
        payload["viewer_vote"] = int(vote_by_post_id.get(post.id, 0))
        serialized_posts.append(payload)
    return serialized_posts


def get_feed(
    *,
    limit: int,
    mode: str | None = None,
    cursor: str | None = None,
    viewer_username: str | None = None,
    music_filter: bool = False,
):
    limit = max(1, min(int(limit or 20), 50))
    feed_mode = _normalize_feed_mode(mode, music_filter=music_filter)
    cursor_payload = _decode_feed_cursor(cursor)

    cursor_mode = cursor_payload.get("mode")
    if cursor_mode and cursor_mode != feed_mode:
        raise ValueError("Invalid feed cursor")

    viewer_user_id = _viewer_user_id(viewer_username)
    if feed_mode == FEED_MODE_FOLLOWERS and viewer_user_id is None:
        raise PermissionError("Authentication required for followers feed")

    hidden_user_ids = block_service.hidden_user_ids_for_viewer(viewer_username)

    next_cursor_payload = {"mode": feed_mode}
    has_more = False

    if feed_mode == FEED_MODE_MUSIC:
        music_posts = _fetch_posts_chronological(
            limit=limit + 1,
            viewer_user_id=viewer_user_id,
            hidden_user_ids=hidden_user_ids,
            cursor_payload=cursor_payload.get("primary"),
            music_filter=True,
        )
        has_more = len(music_posts) > limit
        page_posts = music_posts[:limit]
        if has_more and page_posts:
            next_cursor_payload["primary"] = _build_chronological_cursor_payload(
                page_posts[-1]
            )
        else:
            next_cursor_payload = None

    elif feed_mode == FEED_MODE_FOLLOWERS:
        following_ids_subquery = (
            db.session.query(Follow.following_id)
            .filter(Follow.follower_id == viewer_user_id)
        )
        follower_normal_cursor = (
            cursor_payload.get("normal")
            or cursor_payload.get("primary")
        )
        page_posts, has_more, ratio_next_cursor = _build_ratio_feed_page(
            limit=limit,
            viewer_user_id=viewer_user_id,
            hidden_user_ids=hidden_user_ids,
            normal_cursor_payload=follower_normal_cursor,
            music_cursor_payload=cursor_payload.get("music"),
            author_ids_subquery=following_ids_subquery,
        )
        if has_more:
            next_cursor_payload.update(ratio_next_cursor or {})
        else:
            next_cursor_payload = None

    else:
        page_posts, has_more, ratio_next_cursor = _build_ratio_feed_page(
            limit=limit,
            viewer_user_id=viewer_user_id,
            hidden_user_ids=hidden_user_ids,
            normal_cursor_payload=cursor_payload.get("normal"),
            music_cursor_payload=cursor_payload.get("music"),
        )
        if has_more:
            next_cursor_payload.update(ratio_next_cursor or {})
        else:
            next_cursor_payload = None

    serialized_posts = _serialize_feed_posts(
        page_posts,
        viewer_user_id=viewer_user_id,
        hidden_user_ids=hidden_user_ids,
    )

    return {
        "mode": feed_mode,
        "limit": limit,
        "has_more": bool(has_more),
        "next_cursor": _encode_feed_cursor(next_cursor_payload) if has_more else None,
        "posts": serialized_posts,
    }


def get_posts(
    page: int,
    limit: int,
    viewer_username: str | None = None,
    include_total: bool = True,
):
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 10), 50))
    started_at = time.perf_counter()

    viewer_user_id = _viewer_user_id(viewer_username)
    hidden_user_ids = block_service.hidden_user_ids_for_viewer(viewer_username)

    filter_conditions = [
        Post.is_hidden.is_(False),
        User.is_suspended.is_(False),
        _post_visibility_filter(viewer_user_id),
    ]
    if hidden_user_ids:
        filter_conditions.append(~Post.author_id.in_(hidden_user_ids))

    query = (
        db.session.query(Post.id)
        .join(User, User.id == Post.author_id)
        .filter(*filter_conditions)
    )

    total = None
    if include_total:
        total = (
            query.with_entities(func.count(Post.id))
            .order_by(None)
            .scalar()
            or 0
        )

    offset = (page - 1) * limit
    paged_post_ids = [
        row[0]
        for row in query
        .order_by(Post.created_at.desc(), Post.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    ]
    if not paged_post_ids:
        _log_feed_timing(
            endpoint="posts_feed",
            page=page,
            limit=limit,
            include_total=include_total,
            rows=0,
            started_at=started_at,
        )
        return {
            "page": page,
            "limit": limit,
            "total": total,
            "posts": [],
        }

    posts = (
        Post.query
        .filter(Post.id.in_(paged_post_ids))
        .options(selectinload(Post.media))
        .all()
    )
    posts_by_id = {post.id: post for post in posts}
    ordered_posts = [
        posts_by_id[post_id]
        for post_id in paged_post_ids
        if post_id in posts_by_id
    ]

    quoted_posts_by_id = _build_visible_quoted_posts(
        ordered_posts,
        viewer_user_id=viewer_user_id,
        hidden_user_ids=hidden_user_ids,
    )
    author_ids = {
        post.author_id
        for post in ordered_posts
    } | {
        quoted_post.author_id
        for quoted_post in quoted_posts_by_id.values()
    }
    post_ids = set(paged_post_ids)
    user_by_id, profile_by_user_id = _build_author_maps(author_ids)
    playlist_adders_by_media_id = _build_playlist_adders_by_media(ordered_posts)
    vote_by_post_id = _build_vote_map(
        post_ids=post_ids,
        viewer_user_id=viewer_user_id,
    )

    result = []
    for post in ordered_posts:
        payload = _serialize_post(
            post,
            user_by_id,
            profile_by_user_id,
            playlist_adders_by_media_id=playlist_adders_by_media_id,
            quoted_posts_by_id=quoted_posts_by_id,
        )
        payload["viewer_vote"] = int(vote_by_post_id.get(post.id, 0))
        result.append(payload)

    _log_feed_timing(
        endpoint="posts_feed",
        page=page,
        limit=limit,
        include_total=include_total,
        rows=len(result),
        started_at=started_at,
    )

    return {
        "page": page,
        "limit": limit,
        "total": total,
        "posts": result
    }


def get_post(post_id: int, viewer_username: str | None = None):
    viewer_user_id = _viewer_user_id(viewer_username)
    hidden_user_ids = block_service.hidden_user_ids_for_viewer(viewer_username)

    query = (
        Post.query
        .join(User, User.id == Post.author_id)
        .filter(
            Post.id == post_id,
            Post.is_hidden.is_(False),
            User.is_suspended.is_(False),
            _post_visibility_filter(viewer_user_id),
        )
        .options(selectinload(Post.media))
    )
    if hidden_user_ids:
        query = query.filter(~Post.author_id.in_(hidden_user_ids))

    post = query.first()
    if not post:
        raise ValueError("Post not found")

    quoted_posts_by_id = _build_visible_quoted_posts(
        [post],
        viewer_user_id=viewer_user_id,
        hidden_user_ids=hidden_user_ids,
    )
    author_ids = {post.author_id} | {
        quoted_post.author_id
        for quoted_post in quoted_posts_by_id.values()
    }
    user_by_id, profile_by_user_id = _build_author_maps(author_ids)
    playlist_adders_by_media_id = _build_playlist_adders_by_media([post])
    vote_by_post_id = _build_vote_map(
        post_ids={post.id},
        viewer_user_id=viewer_user_id,
    )

    payload = _serialize_post(
        post,
        user_by_id,
        profile_by_user_id,
        playlist_adders_by_media_id=playlist_adders_by_media_id,
        quoted_posts_by_id=quoted_posts_by_id,
    )
    payload["viewer_vote"] = int(vote_by_post_id.get(post.id, 0))
    return payload


def get_posts_by_username(
    username: str,
    page: int,
    limit: int,
    viewer_username: str | None = None,
):
    user = user_repository.get_by_username(username)
    if not user or getattr(user, "is_suspended", False):
        raise ValueError("User not found")
    if viewer_username and viewer_username != username:
        viewer = user_repository.get_by_username(viewer_username)
        if viewer and block_service.user_ids_have_block_relation(viewer.id, user.id):
            raise ValueError("User not found")

    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 10), 50))

    viewer_user_id = _viewer_user_id(viewer_username)

    base_query = (
        Post.query
        .filter(Post.author_id == user.id)
        .filter(
            Post.is_hidden.is_(False),
            _post_visibility_filter(viewer_user_id),
        )
    )

    total = (
        base_query.with_entities(func.count(Post.id))
        .order_by(None)
        .scalar()
        or 0
    )
    offset = (page - 1) * limit
    paged_post_ids = [
        row[0]
        for row in base_query
        .with_entities(Post.id)
        .order_by(Post.created_at.desc(), Post.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    ]

    if not paged_post_ids:
        return {
            "page": page,
            "limit": limit,
            "total": total,
            "posts": [],
        }

    posts = (
        Post.query
        .filter(Post.id.in_(paged_post_ids))
        .options(selectinload(Post.media))
        .all()
    )
    posts_by_id = {post.id: post for post in posts}
    ordered_posts = [
        posts_by_id[post_id]
        for post_id in paged_post_ids
        if post_id in posts_by_id
    ]

    hidden_user_ids = block_service.hidden_user_ids_for_viewer(viewer_username)
    quoted_posts_by_id = _build_visible_quoted_posts(
        ordered_posts,
        viewer_user_id=viewer_user_id,
        hidden_user_ids=hidden_user_ids,
    )
    author_ids = {post.author_id for post in ordered_posts} | {
        quoted_post.author_id
        for quoted_post in quoted_posts_by_id.values()
    }
    post_ids = set(paged_post_ids)
    user_by_id, profile_by_user_id = _build_author_maps(author_ids)
    playlist_adders_by_media_id = _build_playlist_adders_by_media(ordered_posts)
    vote_by_post_id = _build_vote_map(
        post_ids=post_ids,
        viewer_user_id=viewer_user_id,
    )

    serialized_posts = []
    for post in ordered_posts:
        payload = _serialize_post(
            post,
            user_by_id,
            profile_by_user_id,
            playlist_adders_by_media_id=playlist_adders_by_media_id,
            quoted_posts_by_id=quoted_posts_by_id,
        )
        payload["viewer_vote"] = int(vote_by_post_id.get(post.id, 0))
        serialized_posts.append(payload)

    return {
        "page": page,
        "limit": limit,
        "total": total,
        "posts": serialized_posts
    }


def delete_post_by_username(post_id: int, username: str):
    user = user_repository.get_by_username(username)
    if not user or getattr(user, "is_suspended", False):
        raise ValueError("User not found")

    post = Post.query.get(post_id)
    if not post or post.is_hidden:
        raise ValueError("Post not found")

    if post.author_id != user.id:
        raise PermissionError("You can only delete your own posts")

    media_rows = (
        db.session.query(Media.id, Media.object_name)
        .filter(Media.post_id == post.id)
        .all()
    )
    media_ids = [row[0] for row in media_rows]
    media_object_names = [row[1] for row in media_rows]
    comment_ids = [
        row[0]
        for row in db.session.query(Comment.id).filter(Comment.post_id == post.id).all()
    ]

    if comment_ids:
        Vote.query.filter(
            Vote.target_type == "comment",
            Vote.target_id.in_(comment_ids),
        ).delete(synchronize_session=False)

    Comment.query.filter_by(post_id=post.id).delete(synchronize_session=False)
    Vote.query.filter(
        Vote.target_type == "post",
        Vote.target_id == post.id,
    ).delete(synchronize_session=False)
    if media_ids:
        PlaylistTrack.query.filter(
            PlaylistTrack.media_id.in_(media_ids)
        ).delete(synchronize_session=False)
    Post.query.filter(
        Post.quoted_post_id == post.id
    ).update(
        {Post.quoted_post_id: None},
        synchronize_session=False,
    )
    Media.query.filter_by(post_id=post.id).delete(synchronize_session=False)
    PostReport.query.filter_by(post_id=post.id).delete(synchronize_session=False)
    db.session.delete(post)
    db.session.commit()

    for object_name in media_object_names:
        try:
            _delete_media_object(object_name)
        except Exception:
            pass
