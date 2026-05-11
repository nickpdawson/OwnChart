"""Tesseract OCR pass for rendered page images.

Runs locally — no external network, no PHI consent required. Produces
per-page text + a coarse confidence score. Quality on faxed/handwritten
content is poor; that's expected. The Claude Vision pass (extract/vision.py,
consent-gated) is the higher-quality lane for those.

The function is intentionally synchronous and CPU-bound. Caller decides
whether to run inline (small N pages) or in a worker (large N).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytesseract
from PIL import Image

from ..core.logger import get_logger

log = get_logger("ownchart.extract.ocr")


@dataclass
class PageOcrResult:
    page_number: int
    text: str
    word_count: int
    mean_confidence: float | None  # 0..100, None if Tesseract returned no boxes


def ocr_image(image_path: str, page_number: int = 0, lang: str = "eng") -> PageOcrResult:
    """OCR a single PNG. Returns text and confidence."""
    p = Path(image_path)
    if not p.exists():
        return PageOcrResult(page_number=page_number, text="", word_count=0, mean_confidence=None)

    with Image.open(p) as img:
        text = pytesseract.image_to_string(img, lang=lang) or ""
        try:
            data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)
            confs = [int(c) for c in data.get("conf", []) if c not in ("-1", "", None) and str(c).strip().lstrip("-").isdigit()]
            confs = [c for c in confs if c >= 0]
            mean_conf = (sum(confs) / len(confs)) if confs else None
        except Exception:  # noqa: BLE001
            mean_conf = None

    text = text.strip()
    return PageOcrResult(
        page_number=page_number,
        text=text,
        word_count=len(text.split()) if text else 0,
        mean_confidence=mean_conf,
    )


def ocr_pages(image_paths: list[str], lang: str = "eng") -> list[PageOcrResult]:
    out: list[PageOcrResult] = []
    for i, ip in enumerate(image_paths, start=1):
        try:
            res = ocr_image(ip, page_number=i, lang=lang)
        except Exception as e:  # noqa: BLE001
            log.warning("ocr_failed", page=i, image_path=ip, error=str(e))
            res = PageOcrResult(page_number=i, text="", word_count=0, mean_confidence=None)
        out.append(res)
    log.info(
        "ocr_completed",
        page_count=len(out),
        total_words=sum(r.word_count for r in out),
        any_text=any(r.text for r in out),
    )
    return out
