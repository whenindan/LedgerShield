"""Fuzzy contract-to-vendor matching utilities."""

import logging
import re
from pathlib import Path

from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Normalize text for fuzzy matching.

    1. Convert to lowercase.
    2. Replace non-alphanumeric characters with spaces.
    3. Strip common corporate suffixes.
    4. Remove redundant whitespace.
    """
    text = text.lower()
    # Replace non-alphanumeric characters with spaces
    text = re.sub(r"[^a-z0-9]", " ", text)
    # Strip common corporate suffixes (as isolated words)
    suffixes = r"\b(corp|inc|llc|ltd|limited|co|incorporated|corporation)\b"
    text = re.sub(suffixes, "", text)
    # Collapse multiple spaces and strip
    return " ".join(text.split())


def match_vendor_to_contract(
    vendor_name: str,
    contract_paths: list[Path],
    min_confidence: int = 80,
) -> tuple[Path, int] | None:
    """Fuzzy-match a vendor name against a list of contract file paths.

    Uses ``rapidfuzz.process.extractOne`` with WRatio scorer to find
    the best-matching contract file for the given vendor name. Both inputs
    are normalized before matching.

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

    # Map normalized stems back to their original paths
    normalized_stems = {}
    for p in contract_paths:
        norm = _normalize(p.stem)
        if norm:
            normalized_stems[norm] = p

    if not normalized_stems:
        logger.warning("contract_matcher: no valid contract stems after normalization")
        return None

    normalized_vendor = _normalize(vendor_name)
    
    # We use extractOne on the keys of our mapping
    result = process.extractOne(
        normalized_vendor, 
        normalized_stems.keys(), 
        scorer=fuzz.WRatio
    )
    
    if result is None:
        logger.warning(
            "contract_matcher: no match found for vendor '%s'", vendor_name
        )
        return None

    best_norm, score, _ = result
    if score < min_confidence:
        logger.warning(
            "contract_matcher: best match for '%s' (normalized: '%s') was '%s' (score=%.1f) — "
            "below threshold %d",
            vendor_name,
            normalized_vendor,
            best_norm,
            score,
            min_confidence,
        )
        return None

    matched_path = normalized_stems[best_norm]
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
