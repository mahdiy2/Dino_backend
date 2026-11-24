import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, request

from app.db import db
from app.services.media_submission_replay import run_media_submission


class MediaSubmissionReplayTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = Flask(__name__)
        self.app.config.update(SQLALCHEMY_DATABASE_URI=f"sqlite:///{directory.name}/replay.db")
        db.init_app(self.app)
        with self.app.app_context():
            db.create_all()

        self.executions = []
        self.reject_next = False
        self.concurrent = False
        self.started = threading.Event()
        self.release = threading.Event()

        @self.app.post("/upload/<operation>")
        def upload(operation):
            def execute(submission):
                storage_id = submission.storage_id
                self.executions.append(storage_id)
                if self.reject_next:
                    self.reject_next = False
                    return jsonify({"error": "Temporary failure"}), 503
                if self.concurrent:
                    self.started.set()
                    if not self.release.wait(4):
                        raise RuntimeError("test did not release upload")
                return jsonify({"object": str(storage_id), "size": len(request.files["file"].read())}), 201

            return run_media_submission(request.headers.get("Username", "alice"), operation, execute)

    def send(self, content=b"voice", operation="private", username="alice"):
        with self.app.test_client() as client:
            return client.post(
                f"/upload/{operation}",
                headers={"Idempotency-Key": "stable-key", "Username": username},
                data={"file": (io.BytesIO(content), "voice.wav", "audio/wav")},
            )

    def test_replays_success_rejects_different_content_and_scopes_identity(self):
        first = self.send()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json["size"], 5)
        self.assertEqual(self.send().json, first.json)
        self.assertEqual(self.send(b"different").status_code, 409)
        self.assertEqual(len(self.executions), 1)
        self.assertNotEqual(self.send(operation="group:5").json["object"], first.json["object"])
        self.assertNotEqual(self.send(username="bob").json["object"], first.json["object"])

    def test_failed_submission_can_retry_with_same_storage_key(self):
        self.reject_next = True
        self.assertEqual(self.send().status_code, 503)
        self.assertEqual(self.send().status_code, 201)
        self.assertEqual(len(self.executions), 2)
        self.assertEqual(self.executions[0], self.executions[1])

    def test_two_concurrent_requests_execute_once(self):
        self.concurrent = True
        with ThreadPoolExecutor(2) as executor:
            first = executor.submit(self.send)
            try:
                self.assertTrue(self.started.wait(4))
                duplicate = executor.submit(self.send)
            finally:
                self.release.set()
            a, b = first.result(), duplicate.result()
        self.assertEqual((a.status_code, b.status_code), (201, 201))
        self.assertEqual(a.json, b.json)
        self.assertEqual(len(self.executions), 1)
