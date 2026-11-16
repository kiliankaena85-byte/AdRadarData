"""
Strict Guardrail Tests: Verifies Zero OCR, Zero Vision, and Zero Generative LLM.
"""

import sys
import importlib
import pytest


def test_no_vision_or_ocr_in_dependencies():
    """Ensure no computer vision or OCR libraries are imported or required."""
    forbidden_modules = [
        "cv2",
        "PIL",
        "pillow",
        "pytesseract",
        "rapidocr",
        "tesseract",
        "torchvision.io",
    ]
    for mod in forbidden_modules:
        assert mod not in sys.modules, f"Forbidden module {mod} is loaded in sys.modules!"


def test_no_generative_llms():
    """Ensure no generative LLMs / autoregressive orchestrators are imported."""
    forbidden_generative = [
        "openai",
        "anthropic",
        "langchain",
        "vllm",
        "llama_cpp",
    ]
    for mod in forbidden_generative:
        assert mod not in sys.modules, f"Forbidden generative module {mod} is loaded!"


def test_pure_text_and_dom_contract():
    """Verify that DocumentInput only accepts pure text, HTML string, and DOM attributes."""
    from legal_sheaf.schema import DocumentInput

    doc = DocumentInput(
        clean_text="Простой текстовый фрагмент",
        raw_html="<div><p>Простой текстовый фрагмент</p></div>",
        page_url="https://example.com/post",
        author_domain="example.com",
    )
    assert isinstance(doc.clean_text, str)
    assert isinstance(doc.raw_html, str)
    assert hasattr(doc, "clean_text")
    # Verify no image or binary fields exist
    assert not hasattr(doc, "image")
    assert not hasattr(doc, "image_bytes")
    assert not hasattr(doc, "pixels")
