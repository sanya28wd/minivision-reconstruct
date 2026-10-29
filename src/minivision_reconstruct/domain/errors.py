"""Specific failures raised while preserving and decoding video evidence."""


class EvidenceError(Exception):
    """Base error for evidence ingestion failures."""


class SourceVideoNotFoundError(EvidenceError):
    """Raised when the requested source video does not exist."""


class SourceVideoInvalidError(EvidenceError):
    """Raised when a source is not a non-empty regular file."""


class CaseAlreadyExistsError(EvidenceError):
    """Raised when creating a case would overwrite existing evidence."""


class InvalidCaseIdError(EvidenceError):
    """Raised when a case identifier is not portable and safe."""


class EvidenceIntegrityError(EvidenceError):
    """Raised when copied evidence differs from the supplied source."""


class VideoMetadataError(EvidenceError):
    """Raised when ffprobe cannot extract valid video metadata."""


class VideoDecodingError(EvidenceError):
    """Raised when OpenCV cannot open or completely decode evidence."""


class TimestampError(VideoDecodingError):
    """Raised when no valid presentation timestamp or frame rate is available."""


class DetectorConfigurationError(EvidenceError):
    """Raised when a detector configuration is incomplete or invalid."""


class ModelWeightsError(EvidenceError):
    """Raised when explicitly configured model weights are missing or invalid."""


class DetectorInitializationError(EvidenceError):
    """Raised when a configured detector cannot be initialized."""


class FrameInferenceError(EvidenceError):
    """Raised when detector inference fails for a specific evidence frame."""


class InvalidDetectionError(EvidenceError):
    """Raised when a detector emits an invalid detection."""


class DetectionRunExistsError(EvidenceError):
    """Raised when a detection run would overwrite existing artifacts."""


class CaseNotFoundError(EvidenceError):
    """Raised when a requested Week 1 case does not exist."""


class VideoWriterError(EvidenceError):
    """Raised when annotated-video output cannot be written or verified."""


class TrackerConfigurationError(EvidenceError):
    """Raised when a tracker configuration is incomplete or invalid."""


class TrackingRunExistsError(EvidenceError):
    """Raised when a tracking run would overwrite existing artifacts."""


class TrackingError(EvidenceError):
    """Raised when tracker integration or track serialization fails."""


class TrackReviewError(EvidenceError):
    """Raised when a review event, replay, or reviewed export is invalid."""


class CalibrationError(EvidenceError):
    """Raised when calibration inputs, geometry, or calibration output is invalid."""
