#!/usr/bin/env python3
"""Verify local detector on public YOLOX dog image and synthetic blank negative."""
import base64
import io
import urllib.request
from PIL import Image

if __package__:
    from .look_room import read_survey
    from .local_objects import load_detector
else:
    from look_room import read_survey
    from local_objects import load_detector

PUBLIC_IMAGE = "https://raw.githubusercontent.com/Megvii-BaseDetection/YOLOX/main/assets/dog.jpg"


def main():
    req = urllib.request.Request(PUBLIC_IMAGE, headers={"User-Agent": "SIMSTIM-local-detector-check/1"})
    with urllib.request.urlopen(req, timeout=30) as response:
        image_bytes = response.read(8_000_001)
    if len(image_bytes) > 8_000_000:
        raise RuntimeError("public verification image exceeded size limit")
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    detector = load_detector()
    positive = detector.detect(image)
    negative = detector.detect(Image.new("RGB", (640, 480), (0, 0, 0)))
    labels = [label for label, _ in positive]
    if "dog" not in labels:
        raise RuntimeError(f"expected public known dog detection; got labels={labels}")
    if negative:
        raise RuntimeError(f"blank synthetic negative control produced labels={negative}")
    image_buffer = io.BytesIO()
    image.save(image_buffer, format="JPEG", quality=90)
    jpeg = image_buffer.getvalue()
    lines = ["LOOK_SURVEY_BEGIN", "LOOK_GLANCE 0",
             f"LOOK_FRAME_BEGIN {image.width} {image.height} JPEG {len(jpeg)}"]
    lines.extend("LOOK_DATA " + base64.b64encode(jpeg[i:i + 9000]).decode("ascii")
                 for i in range(0, len(jpeg), 9000))
    lines.append("LOOK_FRAME_END")
    lines.append("LOOK_SURVEY_END")
    survey = read_survey(lines, detector=detector)
    if not survey["ok"] or "dog" not in survey["text"]:
        raise RuntimeError(f"semantic LOOK_GLANCE survey integration failed: {survey}")
    print(f"positive=PASS labels={labels}; negative=PASS labels=[]; survey=PASS text={survey['text']!r}; provider=CPUExecutionProvider; media=memory-only")

if __name__ == "__main__":
    main()
