"""Run PaddleOCR in the project's OCR interpreter.

Invoked by `readers._ocr_pdf_external` when the active interpreter cannot import paddlepaddle.
Usage: python ocr_bridge.py <pdf path> <output text path>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: ocr_bridge.py <pdf> <out.txt>", file=sys.stderr)
        return 2
    pdf = Path(sys.argv[1])
    out = Path(sys.argv[2])

    from readers import _ocr_pdf

    text = _ocr_pdf(pdf) or ""
    out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
