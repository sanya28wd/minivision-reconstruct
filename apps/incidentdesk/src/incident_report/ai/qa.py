"""Separate public-research Q&A adapter. No live client or call is created by the web default.

SDK contract: https://googleapis.github.io/python-genai/ (Files, structured output,
system_instruction); verified against installed google-genai 2.31.0 and Jina Reader.
Files API is Developer API only: real/NDA footage remains blocked (ADR 0009).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from incident_report.ai import AIConnectionError
from incident_report.ai.gemini import GeminiSettings, PublicResearchVideo
from incident_report.ai.google_video import GoogleVideoTransport
from incident_report.hashing import hash_stream
from incident_report.questions import QA_PROMPT, QA_PROMPT_VERSION, QA_SCHEMA, QARequest, QAResponse


class GoogleQAConnection:
    """Explicitly injected opt-in adapter; only owner-declared public clip hashes can leave it.

    A single generate call, no automatic retries. The caller owns the transport/client lifecycle.
    No access to RunLog, analysis requests, rule engine, or report mutation methods.
    """

    def __init__(
        self,
        transport: GoogleVideoTransport,
        settings: GeminiSettings,
        *,
        public_videos: Sequence[PublicResearchVideo],
    ) -> None:
        self.transport = transport
        self.settings = settings
        allowed = set()
        for video in public_videos:
            with video.path.open("rb") as stream:
                allowed.add(hash_stream(stream)[0])
        self.public_hashes = frozenset(allowed)

    def answer(self, request: QARequest) -> QAResponse:
        if request.prompt_version != QA_PROMPT_VERSION or request.prompt != QA_PROMPT or not request.videos:
            raise AIConnectionError("Unsupported Q&A request.")
        # Check all streams before the first upload; a trusted fingerprint alone is insufficient.
        for video in request.videos:
            if video.view_id not in self.public_hashes:
                raise AIConnectionError("Q&A Developer API accepts declared public research footage only.")
            video.stream.seek(0)
            fingerprint, _ = hash_stream(video.stream)
            video.stream.seek(0)
            if fingerprint != video.view_id:
                raise AIConnectionError("Q&A footage fingerprint mismatch.")
        names: list[str] = []
        try:
            contents: list[dict[str, Any]] = []
            for video in request.videos:
                uploaded = self.transport.client.files.upload(file=video.stream, config={"mime_type": "video/mp4"})
                if not uploaded.name:
                    raise AIConnectionError("Q&A video upload failed.")
                names.append(uploaded.name)
                deadline = self.transport.clock() + self.settings.processing_timeout_seconds
                while not uploaded.state or uploaded.state.name != "ACTIVE":
                    if uploaded.state and uploaded.state.name == "FAILED":
                        raise AIConnectionError("Q&A video processing failed.")
                    remaining = deadline - self.transport.clock()
                    if remaining <= 0:
                        raise AIConnectionError("Q&A video processing timed out.")
                    self.transport.sleep(min(self.settings.poll_seconds, remaining))
                    uploaded = self.transport.client.files.get(name=uploaded.name)
                if not uploaded.uri:
                    raise AIConnectionError("Q&A video upload failed.")
                contents.extend(
                    [
                        {"text": f"View fingerprint: {video.view_id}"},
                        {
                            "file_data": {"file_uri": uploaded.uri, "mime_type": "video/mp4"},
                            "video_metadata": {"fps": self.settings.fps},
                        },
                    ]
                )
            contents.append({"text": request.data_json})
            response = self.transport.client.models.generate_content(
                model=self.settings.model,
                contents=contents,
                config={
                    "system_instruction": QA_PROMPT,
                    "response_mime_type": "application/json",
                    "response_schema": QA_SCHEMA,
                },
            )
            return QAResponse.model_validate_json(response.text)
        except Exception:
            raise AIConnectionError("Q&A model request failed. The Report is unchanged.") from None
        finally:
            for name in names:
                try:
                    self.transport.client.files.delete(name=name)
                except Exception:
                    # The owner must handle public-file cleanup if the provider is unavailable.
                    # Never expose a provider exception, URI or credential.
                    logging.getLogger(__name__).warning(
                        "Q&A public-file cleanup failed; ask the owner to check Files API storage."
                    )
