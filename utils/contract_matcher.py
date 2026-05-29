"""Fuzzy contract-to-vendor matching utilities."""

import logging
from pathlib import Path

from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)


def match_vendor_to_contract(
    vendor_name: str,
    contract_paths: list[Path],
    min_confidence: int = 80,
) -> tuple[Path, int] | None:
    """Fuzzy-match a vendor name against a list of contract file paths.

    Uses ``rapidfuzz.process.extractOne`` with default WRatio scorer to find
    the best-matching contract file for the given vendor name.

    Args:
        vendor_name: The vendor name string to match, e.g. ``"Acme Corp"``.
        contract_paths: List of paths to contract files to search.
        min_confidence: Minimum score (0–100) required to accept a match.
            Defaults to ``80``.

    Returns:
        A ``(contract_path, confidence_score)`` tuple if a match at or above
        ``min_confidence`` is found. Returns ``None`` if no match meets the
        threshold or the list is empty.
    """
    if not contract_paths:
        logger.warning("contract_matcher: no contract files provided for vendor '%s'", vendor_name)
        return None

    stems = [p.stem for p in contract_paths]

    # Normalize to lowercase+underscores so "ACME CORP" matches "acme_corp_agreement"
    normalized_vendor = vendor_name.lower().replace(" ", "_")
    result = process.extractOne(normalized_vendor, stems, scorer=fuzz.WRatio)
    if result is None:
        logger.warning(
            "contract_matcher: no match found for vendor '%s'", vendor_name
        )
        return None

    best_stem, score, _ = result
    if score < min_confidence:
        logger.warning(
            "contract_matcher: best match for '%s' (normalized: '%s') was '%s' (score=%.1f) — "
            "below threshold %d",
            vendor_name,
            normalized_vendor,
            best_stem,
            score,
            min_confidence,
        )
        return None

    matched_path = next(p for p in contract_paths if p.stem == best_stem)
    return matched_path, int(score)


def match_vendor_to_contract_in_dir(
    vendor_name: str,
    contracts_dir: Path,
    min_confidence: int = 80,
) -> tuple[Path, int] | None:
    """Fuzzy-match a vendor name against contract filenames in a directory.

    Scans ``contracts_dir`` for all regular files and delegates to
    ``match_vendor_to_contract``. Used by the CLI pipeline.

    Args:
        vendor_name: The vendor name string to match, e.g. ``"Acme Corp"``.
        contracts_dir: Path to the directory containing contract files.
        min_confidence: Minimum score (0–100) required to accept a match.

    Returns:
        A ``(contract_path, confidence_score)`` tuple or ``None``.

    Raises:
        FileNotFoundError: If ``contracts_dir`` does not exist.
    """
    paths = [p for p in contracts_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
    return match_vendor_to_contract(vendor_name, paths, min_confidence)
