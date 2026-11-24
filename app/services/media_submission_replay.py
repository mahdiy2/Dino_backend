"""Transactional replay receipts for authenticated media submissions."""
import hashlib
import json
import re
import uuid

from flask import current_app, jsonify, make_response, request
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db import db


class MediaSubmissionReceipt(db.Model):
    __tablename__ = "media_submission_receipts"

    username = db.Column(db.String(255), primary_key=True)
    operation = db.Column(db.String(255), primary_key=True)
    key = db.Column(db.String(128), primary_key=True)
    fingerprint = db.Column(db.String(64), nullable=False)
    response_status = db.Column(db.Integer)
    response_json = db.Column(db.JSON)


def _request_fingerprint():
    fields = sorted((name, values) for name, values in request.form.lists())
    files = []
    for name, uploads in sorted(request.files.lists()):
        for upload in uploads:
            stream = upload.stream
            start = stream.tell()
            digest = hashlib.sha256()
            try:
                while chunk := stream.read(128 * 1024):
                    digest.update(chunk)
            finally:
                stream.seek(start)
            files.append((name, upload.filename, upload.mimetype, digest.hexdigest()))
    payload = request.get_json(silent=True) if request.is_json else [fields, files]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class MediaSubmission:
    def __init__(self, storage_id):
        self.storage_id = storage_id
        self.after_commit = []


def finish_mutation(submission, effects):
    if submission is None:
        db.session.commit()
        effects()
    else:
        db.session.flush()
        submission.after_commit.append(effects)


def run_media_submission(username, operation, execute, *, require_version=False):
    """Run a keyed operation whose successful writes use the caller's DB transaction.

    execute receives a MediaSubmission (or None for legacy requests).
    Successful operations must flush writes without committing independently.
    """
    key = request.headers.get("Idempotency-Key")
    if key is None or (require_version and request.headers.get("X-Dino-Media-Replay") != "1"):
        return execute(None)
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        return jsonify({"error": "Invalid Idempotency-Key"}), 400

    fingerprint = _request_fingerprint()
    identity = (username, operation, key)
    table = MediaSubmissionReceipt.__table__
    dialect = db.session.get_bind().dialect.name
    insert = {"postgresql": pg_insert, "sqlite": sqlite_insert}[dialect]
    try:
        claimed = db.session.execute(
            insert(table).values(username=username, operation=operation, key=key, fingerprint=fingerprint)
            .on_conflict_do_nothing(index_elements=[table.c.username, table.c.operation, table.c.key])
            .returning(table.c.key)
        ).scalar_one_or_none()
        receipt = db.session.get(MediaSubmissionReceipt, identity)
        if claimed is None:
            if receipt.fingerprint != fingerprint:
                db.session.rollback()
                return jsonify({"error": "Idempotency-Key already used with different media or fields"}), 409
            response = make_response(jsonify(receipt.response_json), receipt.response_status)
            response.headers["X-Dino-Media-Replay"] = "1"
            db.session.rollback()
            return response

        submission = MediaSubmission(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps((*identity, fingerprint))))
        response = make_response(execute(submission))
        if not 200 <= response.status_code < 300:
            db.session.rollback()
            return response
        receipt.response_json = response.get_json()
        receipt.response_status = response.status_code
        db.session.commit()
        for effect in submission.after_commit:
            try:
                effect()
            except Exception:
                db.session.rollback()
                current_app.logger.exception("Post-commit media side effect failed for %s", operation)
        response.headers["X-Dino-Media-Replay"] = "1"
        return response
    except Exception:
        db.session.rollback()
        raise
