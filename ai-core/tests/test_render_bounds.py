"""Rendering bounds are decided before any bitmap exists.

A page of unusual proportions — a 20,000-point poster, a one-point-wide
strip — must come out inside the policy's edge and area ceilings, and a page
without a finite positive size is refused rather than allocated.
"""

import io
import math
from dataclasses import replace

import pytest
from PIL import Image

from app.services.documents import policy
from app.services.documents.pdf import (
    open_pdf,
    render_page,
    render_scale,
)
from tests.test_documents import make_pdf


def _pdf_with_mediabox(width: float, height: float) -> bytes:
    data = make_pdf([["A page of unusual size"]])
    return data.replace(b"/MediaBox[0 0 612 792]", f"/MediaBox[0 0 {width:g} {height:g}]".encode())


@pytest.mark.parametrize(
    ("width", "height"),
    [(612, 792), (20000, 20000), (14400, 200), (3, 3), (200, 14400), (1, 12000)],
)
def test_render_scale_keeps_every_page_inside_edge_and_area(width, height):
    scale = render_scale(width, height)
    pixels_w, pixels_h = width * scale, height * scale
    assert max(pixels_w, pixels_h) <= policy.PDF.render_max_edge + 1
    assert pixels_w * pixels_h <= policy.PDF.render_max_pixels + policy.PDF.render_max_edge
    assert math.ceil(min(pixels_w, pixels_h)) >= 1  # PDFium rounds a thin side up to one pixel
    assert scale <= 4.0


def test_render_scale_refuses_pages_without_a_size():
    for bad in ((0, 100), (100, -1), (float("nan"), 10), (float("inf"), 10)):
        with pytest.raises(ValueError):
            render_scale(*bad)


def test_huge_and_odd_pages_render_within_the_ceilings(monkeypatch):
    monkeypatch.setattr(policy, "PDF", replace(policy.PDF, render_max_edge=800, render_max_pixels=200_000))
    for width, height in ((20000, 20000), (14400, 100), (12, 12)):
        pdf = open_pdf(_pdf_with_mediabox(width, height))
        try:
            image = Image.open(io.BytesIO(render_page(pdf, 1)))
        finally:
            pdf.close()
        assert max(image.size) <= 800 + 1, (width, height, image.size)
        assert image.size[0] * image.size[1] <= 200_000 + 800, (width, height, image.size)
        assert min(image.size) >= 1
