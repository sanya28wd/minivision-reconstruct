"""Explicit Ultralytics YOLO connector for independent frame detections."""

from pathlib import Path
from time import perf_counter

from numpy.typing import NDArray
from numpy import uint8

from minivision_reconstruct.domain.errors import DetectorInitializationError, FrameInferenceError, ModelWeightsError
from minivision_reconstruct.domain.models import DetectorConfiguration, DetectorProvenance, RawDetection
from minivision_reconstruct.evidence.integrity import sha256_file


class YoloDetector:
    """External-model connector with no implicit model or device fallback."""

    def __init__(self, configuration: DetectorConfiguration) -> None:
        if not configuration.weights_path.is_file():
            raise ModelWeightsError(f"Configured weights do not exist: '{configuration.weights_path}'.")
        if configuration.weights_path.stat().st_size == 0:
            raise ModelWeightsError(f"Configured weights are empty: '{configuration.weights_path}'.")
        try:
            from ultralytics import YOLO
            import ultralytics

            self._model = YOLO(str(configuration.weights_path))
        except Exception as error:
            raise DetectorInitializationError(
                f"Could not initialize '{configuration.model_name}' from '{configuration.weights_path}': {error}"
            ) from error
        self._configuration = configuration
        self.provenance = DetectorProvenance(
            model_family=configuration.model_family,
            model_name=configuration.model_name,
            model_version=configuration.model_name,
            library_version=ultralytics.__version__,
            weight_source=configuration.weight_source,
            weight_sha256=sha256_file(configuration.weights_path),
            device=configuration.device,
            model_license=configuration.model_license,
            model_size_bytes=configuration.weights_path.stat().st_size,
        )

    def detect(self, image: NDArray[uint8], case_id: str, frame_number: int) -> tuple[tuple[RawDetection, ...], float]:
        """Run one batch-size-one inference and return raw independent detections."""
        started_at = perf_counter()
        try:
            results = self._model.predict(
                source=image,
                conf=self._configuration.confidence_threshold,
                imgsz=self._configuration.inference_image_size,
                device=self._configuration.device,
                verbose=False,
            )
        except Exception as error:
            raise FrameInferenceError(
                f"Inference failed for case '{case_id}', frame {frame_number}, model '{self._configuration.model_name}': {error}"
            ) from error
        elapsed = perf_counter() - started_at
        if len(results) != 1:
            raise FrameInferenceError(
                f"Model '{self._configuration.model_name}' returned {len(results)} result sets for case '{case_id}', frame {frame_number}; expected one."
            )
        result = results[0]
        names = result.names
        boxes = result.boxes
        if boxes is None:
            return (), elapsed
        detections: list[RawDetection] = []
        for box in boxes:
            class_index = int(box.cls.item())
            label_value = names[class_index]
            label = str(label_value)
            coordinates = box.xyxy[0].tolist()
            detections.append(
                RawDetection(
                    label=label,
                    confidence=float(box.conf.item()),
                    x1=float(coordinates[0]),
                    y1=float(coordinates[1]),
                    x2=float(coordinates[2]),
                    y2=float(coordinates[3]),
                )
            )
        return tuple(detections), elapsed
