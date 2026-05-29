"""File loading utilities for invoices and supporting documents."""

import json
import logging
from pathlib import Path

import pandas as pd
import pdfplumber

logger = logging.getLogger(__name__)


def load_text_file(path: Path) -> str:
    """Read a plain-text or Markdown file and return its contents.

    Args:
        path: Absolute or relative path to a ``.txt`` or ``.md`` file.

    Returns:
        The full string contents of the file.

    Raises:
        FileNotFoundError: If ``path`` does not point to an existing file.
        IOError: If the file cannot be read.
    """
    return path.read_text(encoding="utf-8")


def load_pdf_file(path: Path) -> str:
    """Extract all text from a PDF using pdfplumber.

    Each page's text is joined with a newline character. Pages that contain
    no extractable text contribute an empty string.

    Args:
        path: Absolute or relative path to a ``.pdf`` file.

    Returns:
        All extracted text from every page, joined by newlines.

    Raises:
        FileNotFoundError: If ``path`` does not point to an existing file.
        pdfplumber.PDFSyntaxError: If the file is not a valid PDF.
    """
    with pdfplumber.open(path) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    return "\n".join(pages)


def load_csv_as_text(path: Path) -> str:
    """Load a CSV file and return its contents as a formatted string.

    Converts the DataFrame to a string representation suitable for embedding
    in an LLM prompt.

    Args:
        path: Absolute or relative path to a ``.csv`` file.

    Returns:
        The ``DataFrame.to_string()`` representation of the CSV data.

    Raises:
        FileNotFoundError: If ``path`` does not point to an existing file.
        pd.errors.ParserError: If the file cannot be parsed as CSV.
    """
    df = pd.read_csv(path)
    return df.to_string()


def load_csv_as_dataframe(path: Path) -> pd.DataFrame:
    """Load a CSV file and return the raw pandas DataFrame.

    Args:
        path: Absolute or relative path to a ``.csv`` file.

    Returns:
        A ``pandas.DataFrame`` containing the parsed CSV data.

    Raises:
        FileNotFoundError: If ``path`` does not point to an existing file.
        pd.errors.ParserError: If the file cannot be parsed as CSV.
    """
    return pd.read_csv(path)


def load_json_file(path: Path) -> dict | list:
    """Load a JSON file and return the parsed Python object.

    Args:
        path: Absolute or relative path to a ``.json`` file.

    Returns:
        A ``dict`` or ``list`` depending on the top-level JSON structure.

    Raises:
        FileNotFoundError: If ``path`` does not point to an existing file.
        json.JSONDecodeError: If the file does not contain valid JSON.
    """
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def extract_text_from_upload(path: Path, ext: str) -> str:
    """Extract plain text from an uploaded file for pipeline ingestion.

    Dispatches by extension:
      .txt / .md  → direct UTF-8 read
      .pdf        → pdfplumber page extraction
      .png / .jpg → pytesseract OCR (gracefully degrades if not installed)

    Args:
        path: Absolute path to the uploaded raw file.
        ext:  Lowercase extension without the dot (e.g. "pdf", "png").

    Returns:
        Extracted text string ready to be written to the pipeline directory.
    """
    if ext in ("txt", "md"):
        return path.read_text(encoding="utf-8")

    if ext == "pdf":
        with pdfplumber.open(path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        text = "\n".join(pages).strip()
        if not text:
            logger.warning("pdfplumber extracted no text from %s — may be a scanned PDF", path.name)
            text = f"[PDF FILE: {path.name} — no extractable text layer; manual review required]"
        return text

    if ext in ("png", "jpg", "jpeg"):
        try:
            import pytesseract
            from PIL import Image
            img = Image.open(path)
            text = pytesseract.image_to_string(img).strip()
            if not text:
                text = f"[IMAGE FILE: {path.name} — OCR returned no text; manual review required]"
            return text
        except ImportError:
            logger.warning(
                "pytesseract / Pillow not installed — returning placeholder for %s", path.name
            )
            return (
                f"[IMAGE FILE: {path.name} — install pytesseract and Pillow for OCR. "
                "Manual review required until then.]"
            )
        except Exception as exc:
            logger.error("OCR failed for %s: %s", path.name, exc)
            return f"[IMAGE FILE: {path.name} — OCR error: {exc}]"

    raise ValueError(f"Unsupported upload extension '{ext}' for file '{path.name}'")


def load_invoice_file(path: Path) -> str:
    """Dispatch to the correct loader based on the file extension.

    Supported extensions: ``.txt``, ``.md``, ``.csv``, ``.pdf``.

    Args:
        path: Absolute or relative path to an invoice file.

    Returns:
        The extracted text content of the invoice.

    Raises:
        FileNotFoundError: If ``path`` does not point to an existing file.
        ValueError: If the file extension is not one of the supported types.
    """
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return load_text_file(path)
    if suffix == ".csv":
        return load_csv_as_text(path)
    if suffix == ".pdf":
        return load_pdf_file(path)
    raise ValueError(
        f"Unsupported invoice file extension '{suffix}' for file '{path}'. "
        "Supported extensions are: .txt, .md, .csv, .pdf"
    )
