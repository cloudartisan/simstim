"""Local-only YOLOX-Nano COCO object detection for SIMSTIM frames.

No image or inference output leaves the process. Person detections are excluded;
COCO labels never include OCR, face, or identity inference.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PIL import Image

COCO_LABELS = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle", "wine glass",
    "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange", "broccoli",
    "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant", "bed",
    "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)

MODEL_URL = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx"
MODEL_SHA256 = "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d"
DEFAULT_MODEL = Path.home() / ".hermes/cache/scratch/simstim-yolox/yolox_nano.onnx"
INPUT_SIZE = 416


def load_detector(model_path: Path | None = None):
    """Load CPU ONNX model and run a warm-up before any physical action."""
    model_path = model_path or Path(os.environ.get("SIMSTIM_YOLOX_MODEL", DEFAULT_MODEL))
    if not model_path.is_file():
        raise RuntimeError(f"local YOLOX-Nano model missing: {model_path}; run tools/setup_look_model.py")
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if digest != MODEL_SHA256:
        raise RuntimeError(f"YOLOX-Nano model SHA-256 mismatch: {digest}")
    try:
        import numpy as np
        import onnxruntime as ort
        session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        inputs = session.get_inputs()
        if not inputs or len(inputs[0].shape) != 4:
            raise RuntimeError("unexpected YOLOX ONNX input signature")
        name = inputs[0].name
        # Warm-up input has no image semantics and remains in memory.
        session.run(None, {name: np.zeros((1, 3, INPUT_SIZE, INPUT_SIZE), dtype=np.float32)})
        return YoloXDetector(session, name)
    except Exception as exc:
        raise RuntimeError(f"could not initialize local CPU YOLOX-Nano: {exc}") from exc


class YoloXDetector:
    def __init__(self, session, input_name: str):
        self.session, self.input_name = session, input_name

    def detect(self, image: Image.Image, threshold: float = 0.35) -> list[tuple[str, float]]:
        """Return class/confidence pairs after YOLOX decode and class-wise NMS."""
        import numpy as np
        rgb = image.convert("RGB")
        w, h = rgb.size
        scale = min(INPUT_SIZE / w, INPUT_SIZE / h)
        nw, nh = int(w * scale), int(h * scale)
        resized = rgb.resize((nw, nh), Image.Resampling.BILINEAR)
        canvas = Image.new("RGB", (INPUT_SIZE, INPUT_SIZE), (114, 114, 114))
        canvas.paste(resized, (0, 0))
        arr = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None, ...]
        raw = self.session.run(None, {self.input_name: arr})[0]
        pred = np.asarray(raw).reshape(-1, 5 + len(COCO_LABELS)).copy()
        # YOLOX raw output: cx,cy,w,h,obj,class probabilities; grid decode.
        grids, strides = [], []
        for stride in (8, 16, 32):
            yv, xv = np.meshgrid(np.arange(INPUT_SIZE // stride), np.arange(INPUT_SIZE // stride), indexing="ij")
            grids.append(np.stack((xv, yv), axis=-1).reshape(-1, 2))
            strides.append(np.full((len(xv.reshape(-1)), 1), stride))
        grid = np.concatenate(grids).astype(np.float32)
        stride = np.concatenate(strides).astype(np.float32)
        if len(pred) != len(grid):
            raise RuntimeError(f"unexpected YOLOX output rows: {len(pred)}")
        boxes = pred[:, :4].copy()
        boxes[:, :2] = (boxes[:, :2] + grid) * stride
        boxes[:, 2:4] = np.exp(np.clip(boxes[:, 2:4], -10, 10)) * stride
        scores = pred[:, 4:5] * pred[:, 5:]
        class_ids = scores.argmax(axis=1)
        confidence = scores.max(axis=1)
        boxes[:, 0] = (boxes[:, 0] - boxes[:, 2] / 2) / scale
        boxes[:, 1] = (boxes[:, 1] - boxes[:, 3] / 2) / scale
        boxes[:, 2] /= scale
        boxes[:, 3] /= scale
        candidates = np.where((confidence >= threshold) & (class_ids != 0))[0]
        selected = []
        for cls in np.unique(class_ids[candidates]):
            ids = candidates[class_ids[candidates] == cls]
            ids = ids[np.argsort(confidence[ids])[::-1]]
            while len(ids):
                best = int(ids[0]); selected.append(best)
                if len(ids) == 1:
                    break
                b = boxes[best]
                rest = boxes[ids[1:]]
                x1 = np.maximum(b[0] - b[2] / 2, rest[:, 0] - rest[:, 2] / 2)
                y1 = np.maximum(b[1] - b[3] / 2, rest[:, 1] - rest[:, 3] / 2)
                x2 = np.minimum(b[0] + b[2] / 2, rest[:, 0] + rest[:, 2] / 2)
                y2 = np.minimum(b[1] + b[3] / 2, rest[:, 1] + rest[:, 3] / 2)
                inter = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
                area_b = max(0, b[2]) * max(0, b[3])
                area_r = np.maximum(0, rest[:, 2]) * np.maximum(0, rest[:, 3])
                iou = inter / np.maximum(1e-9, area_b + area_r - inter)
                ids = ids[1:][iou <= 0.45]
        selected.sort(key=lambda i: float(confidence[i]), reverse=True)
        result, seen = [], set()
        for i in selected:
            label = COCO_LABELS[int(class_ids[i])]
            if label not in seen:
                seen.add(label)
                result.append((label, float(confidence[i])))
            if len(result) == 5:
                break
        return result


def describe_detections(detections: list[tuple[str, float]]) -> str:
    labels = [name for name, _ in detections if name != "person"]
    if not labels:
        return "I couldn't identify a clear object in the views."
    labels = labels[:3]
    if len(labels) == 1:
        return f"I can see a {labels[0]}."
    if len(labels) == 2:
        return f"I can see a {labels[0]} and a {labels[1]}."
    return f"I can see a {labels[0]}, a {labels[1]}, and a {labels[2]}."
