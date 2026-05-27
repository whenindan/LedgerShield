"""File loading utilities for invoices and supporting documents."""

import json
from pathlib import Path

import pandas as pd
import pdfplumber


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
