from flask import Blueprint, request, jsonify
from flask_jwt_extended import get_jwt_identity, jwt_required
from app.services.post_service import (
    ConcurrentPostUploadError,
    MediaStorageError,
    create_post_with_media,
    edit_post_by_username,
    get_feed,
    get_post,
    delete_post_by_username,
    get_posts,
)

post_bp = Blueprint("posts", __name__)


def _parse_followers_only(value):
    if value is None:
        return False

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False

    raise ValueError("followers_only must be a boolean")


def _parse_include_total(value):
    if value is None:
        return True

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False

    raise ValueError("include_total must be a boolean")


def _parse_music_filter(value):
    if value is None:
        return False

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False

    raise ValueError("music_filter must be a boolean")


def _parse_optional_post_id(value):
    if value is None:
        return None

    if isinstance(value, bool):
        raise ValueError("quoted_post_id must be a positive integer")

    if isinstance(value, int):
        if value <= 0:
            raise ValueError("quoted_post_id must be a positive integer")
        return value

    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if not normalized.isdigit():
            raise ValueError("quoted_post_id must be a positive integer")
        parsed_value = int(normalized)
        if parsed_value <= 0:
            raise ValueError("quoted_post_id must be a positive integer")
        return parsed_value

    raise ValueError("quoted_post_id must be a positive integer")


def _parse_media_id_collection(value):
    if value is None:
        return None

    if isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = [value]

    parsed_ids = []
    for item in values:
        if isinstance(item, bool):
            raise ValueError("keep_media_ids must contain positive integers")
        if isinstance(item, int):
            media_id = item
        elif isinstance(item, str):
            normalized = item.strip()
            if not normalized:
                continue
            if not normalized.isdigit():
                raise ValueError("keep_media_ids must contain positive integers")
            media_id = int(normalized)
        else:
            raise ValueError("keep_media_ids must contain positive integers")

        if media_id <= 0:
            raise ValueError("keep_media_ids must contain positive integers")
        parsed_ids.append(media_id)

    return parsed_ids


def _extract_post_idempotency_key(data, *, header_value=None):
    candidates = [header_value]
    if hasattr(data, "get"):
        candidates.extend(
            [
                data.get("idempotency_key"),
                data.get("client_post_id"),
                data.get("client_nonce"),
            ]
        )

    for candidate in candidates:
        if candidate is None:
            continue
        if not isinstance(candidate, str):
            raise ValueError("idempotency_key must be a string")
        normalized = candidate.strip()
        if normalized:
            return normalized
    return None


@post_bp.route("/posts", methods=["POST"])
@jwt_required()
def create_post():
    username = get_jwt_identity()

    content_type = (request.content_type or "").lower()
    text = None
    files = []
    followers_only = False
    track_title = None
    track_artist = None
    quoted_post_id = None
    idempotency_key = None
    idempotency_key_header = (
        request.headers.get("Idempotency-Key")
        or request.headers.get("X-Idempotency-Key")
    )

    if "multipart/form-data" in content_type:
        text = request.form.get("text")
        track_title = request.form.get("track_title")
        track_artist = request.form.get("track_artist")
        try:
            quoted_post_id = _parse_optional_post_id(request.form.get("quoted_post_id"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            followers_only = _parse_followers_only(request.form.get("followers_only"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            idempotency_key = _extract_post_idempotency_key(
                request.form,
                header_value=idempotency_key_header,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        files = (
            request.files.getlist("media")
            or request.files.getlist("media[]")
            or request.files.getlist("files")
        )
        single = request.files.get("file")
        if single:
            files.append(single)
    else:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON body"}), 400
        text = data.get("text")
        try:
            quoted_post_id = _parse_optional_post_id(data.get("quoted_post_id"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            followers_only = _parse_followers_only(data.get("followers_only"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            idempotency_key = _extract_post_idempotency_key(
                data,
                header_value=idempotency_key_header,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    try:
        result = create_post_with_media(
            username,
            text,
            files,
            followers_only=followers_only,
            track_title=track_title,
            track_artist=track_artist,
            quoted_post_id=quoted_post_id,
            idempotency_key=idempotency_key,
        )
        return jsonify({
            "message": "Post created successfully",
            "post_id": result["post_id"]
        }), 201
    except MediaStorageError as e:
        return jsonify({"error": str(e)}), 503
    except ConcurrentPostUploadError as e:
        return jsonify({"error": str(e)}), 409
    except ValueError as e:
        if str(e) == "Account suspended":
            return jsonify({"error": str(e)}), 403
        return jsonify({"error": str(e)}), 400



@post_bp.route("/posts", methods=["GET"])
@jwt_required(optional=True)
def list_posts():
    viewer_username = get_jwt_identity()
    page = request.args.get("page", default=1, type=int)
    limit = request.args.get("limit", default=10, type=int)
    try:
        include_total = _parse_include_total(request.args.get("include_total"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    data = get_posts(
        page,
        limit,
        viewer_username=viewer_username,
        include_total=include_total,
    )
    return jsonify(data), 200


@post_bp.route("/posts/feed", methods=["GET"])
@jwt_required(optional=True)
def list_feed():
    viewer_username = get_jwt_identity()
    limit = request.args.get("limit", default=20, type=int)
    mode = request.args.get("mode")
    cursor = request.args.get("cursor")

    try:
        music_filter = _parse_music_filter(request.args.get("music_filter"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        payload = get_feed(
            limit=limit,
            mode=mode,
            cursor=cursor,
            viewer_username=viewer_username,
            music_filter=music_filter,
        )
        return jsonify(payload), 200
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@post_bp.route("/posts/<int:post_id>", methods=["GET"])
@jwt_required(optional=True)
def get_post_detail(post_id):
    viewer_username = get_jwt_identity()

    try:
        post = get_post(post_id, viewer_username=viewer_username)
        return jsonify({"post": post}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@post_bp.route("/posts/<int:post_id>", methods=["DELETE"])
@jwt_required()
def delete_post(post_id):
    username = get_jwt_identity()

    try:
        delete_post_by_username(post_id=post_id, username=username)
        return jsonify({"message": "Post deleted", "post_id": post_id}), 200
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@post_bp.route("/posts/<int:post_id>", methods=["PUT"])
@jwt_required()
def edit_post(post_id):
    username = get_jwt_identity()
    content_type = (request.content_type or "").lower()
    text = None
    files = []
    followers_only = None
    track_title = None
    track_artist = None
    keep_media_ids = None

    if "multipart/form-data" in content_type:
        text = request.form.get("text")
        track_title = request.form.get("track_title")
        track_artist = request.form.get("track_artist")

        keep_values = request.form.getlist("keep_media_ids")
        if not keep_values:
            keep_values = request.form.getlist("keep_media_ids[]")
        if not keep_values and "keep_media_ids" in request.form:
            keep_values = [request.form.get("keep_media_ids")]
        try:
            keep_media_ids = _parse_media_id_collection(keep_values if keep_values else None)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        followers_raw = request.form.get("followers_only")
        if followers_raw is not None:
            try:
                followers_only = _parse_followers_only(followers_raw)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

        files = (
            request.files.getlist("media")
            or request.files.getlist("media[]")
            or request.files.getlist("files")
        )
        single = request.files.get("file")
        if single:
            files.append(single)
    else:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON body"}), 400
        text = data.get("text")
        track_title = data.get("track_title")
        track_artist = data.get("track_artist")
        try:
            keep_media_ids = _parse_media_id_collection(data.get("keep_media_ids"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        if "followers_only" in data:
            try:
                followers_only = _parse_followers_only(data.get("followers_only"))
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

    try:
        edit_post_by_username(
            post_id=post_id,
            username=username,
            text=text,
            files=files,
            keep_media_ids=keep_media_ids,
            followers_only=followers_only,
            track_title=track_title,
            track_artist=track_artist,
        )
        return jsonify({"message": "Post updated", "post_id": post_id}), 200
    except MediaStorageError as e:
        return jsonify({"error": str(e)}), 503
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        message = str(e)
        if message == "Account suspended":
            return jsonify({"error": message}), 403
        if message in {"Post not found", "User not found"}:
            return jsonify({"error": message}), 404
        return jsonify({"error": message}), 400
