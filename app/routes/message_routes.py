from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.services import message_service
from app.services.media_submission_replay import run_media_submission

message_bp = Blueprint("messages", __name__)


@message_bp.route("/send", methods=["POST"])
@jwt_required()
def send():
    return jsonify({
        "error": "HTTP send is deprecated. Use socket event 'send_message'."
    }), 410


@message_bp.route("/media-submission-capabilities", methods=["GET"])
def media_submission_capabilities():
    return jsonify({"replay_version": 1})


@message_bp.route("/attachments", methods=["POST"])
@jwt_required()
def upload_attachment():
    username = get_jwt_identity()
    file = request.files.get("file") or request.files.get("attachment")

    def execute(submission):
        try:
            payload = message_service.upload_message_attachment(
                username=username,
                file_storage=file,
                storage_id=submission.storage_id if submission else None,
                upload_scope="private",
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except message_service.MessageAttachmentStorageError as exc:
            return jsonify({"error": str(exc)}), 503

        return jsonify({"attachment": payload}), 201

    return run_media_submission(username, "private-attachment", execute)


@message_bp.route("/inbox", methods=["GET"])
@jwt_required()
def inbox():
    username = get_jwt_identity()
    consume = request.args.get("consume", "").strip().lower()
    if consume in ("1", "true", "yes"):
        messages = message_service.receive_messages(username)
    else:
        messages = message_service.peek_messages(username)
    return jsonify({"messages": messages})


@message_bp.route("/history/private/<chat_id>", methods=["GET"])
@jwt_required()
def private_history(chat_id):
    username = get_jwt_identity()
    chat_id = (chat_id or "").strip()
    if not chat_id:
        return jsonify({"error": "chat_id is required"}), 400

    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    before_timestamp = request.args.get("before")

    payload = message_service.get_private_message_history(
        username=username,
        chat_id=chat_id,
        limit=limit,
        before_timestamp=before_timestamp,
    )
    return jsonify(
        {
            "chat_id": chat_id,
            "messages": payload["messages"],
            "has_more": payload["has_more"],
            "next_before": payload["next_before"],
        }
    ), 200


@message_bp.route("/history/group/<int:group_id>", methods=["GET"])
@jwt_required()
def group_history(group_id):
    username = get_jwt_identity()
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    before_timestamp = request.args.get("before")

    from app.repositories import group_repository, user_repository

    user = user_repository.get_by_username(username)
    if not user:
        return jsonify({"error": "User not found"}), 404
    if not group_repository.is_member(group_id, user.id):
        return jsonify({"error": "You are not a member of this group"}), 403

    payload = message_service.get_group_message_history(
        username=username,
        group_id=group_id,
        limit=limit,
        before_timestamp=before_timestamp,
    )
    return jsonify(
        {
            "group_id": group_id,
            "messages": payload["messages"],
            "has_more": payload["has_more"],
            "next_before": payload["next_before"],
        }
    ), 200


@message_bp.route("/reactions", methods=["POST"])
@jwt_required()
def react_to_message():
    username = get_jwt_identity()
    body = request.get_json(silent=True) or {}
    message_id = body.get("message_id")
    emoji = body.get("emoji")

    if not isinstance(message_id, str) or not message_id.strip():
        return jsonify({"error": "message_id is required"}), 400
    if emoji is not None and not isinstance(emoji, str):
        return jsonify({"error": "emoji must be a string"}), 400

    message_id = message_id.strip()
    if isinstance(emoji, str):
        emoji = emoji.strip() or None

    context = message_service.get_message_scope_context(message_id)
    if not isinstance(context, dict):
        return jsonify({"error": "Message not found"}), 404

    scope = context.get("scope")
    if scope == "private":
        sender = context.get("sender")
        recipient = context.get("recipient")
        if username != sender and username != recipient:
            return jsonify({"error": "Not allowed"}), 403
        counterpart = recipient if username == sender else sender
        if emoji is None:
            message_service.remove_message_reaction(message_id, username)
        else:
            message_service.set_message_reaction(
                message_id=message_id,
                username=username,
                emoji=emoji,
                message_scope="private",
                chat_counterpart=counterpart,
            )
        reactions = message_service.get_message_reactions(message_id)
        return jsonify(
            {
                "scope": "private",
                "message_id": message_id,
                "chat_id": counterpart,
                "reactions": reactions,
            }
        ), 200

    if scope == "group":
        group_id = context.get("group_id")
        from app.repositories import group_repository, user_repository

        user = user_repository.get_by_username(username)
        if not user or not group_repository.is_member(group_id, user.id):
            return jsonify({"error": "You are not a member of this group"}), 403
        if emoji is None:
            message_service.remove_message_reaction(message_id, username)
        else:
            message_service.set_message_reaction(
                message_id=message_id,
                username=username,
                emoji=emoji,
                message_scope="group",
                group_id=group_id,
            )
        reactions = message_service.get_message_reactions(message_id)
        return jsonify(
            {
                "scope": "group",
                "group_id": int(group_id),
                "message_id": message_id,
                "reactions": reactions,
            }
        ), 200

    return jsonify({"error": "Unsupported message scope"}), 400
