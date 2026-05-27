"""Fuzzy contract-to-vendor matching utilities."""

import logging
from pathlib import Path

from rapidfuzz import process

logger = logging.getLogger(__name__)


def get_all_contract_names(contracts_dir: Path) -> list[str]:
    """Return the stem names of every file in the contracts directory.

    Only regular files are included; subdirectories are ignored.

    Args:
        contracts_dir: Path to the directory containing contract files.

    Returns:
        A list of filename stems (names without extension), e.g.
        ``["acme_corp_agreement", "globex_msa"]``.

    Raises:
        FileNotFoundError: If ``contracts_dir`` does not exist.
    """
    return [p.stem for p in contracts_dir.iterdir() if p.is_file()]


def match_vendor_to_contract(
    vendor_name: str,
    contracts_dir: Path,
    min_confidence: int = 80,
) -> tuple[Path, int] | None:
    """Fuzzy-match a vendor name against contract filenames in a directory.

    Uses ``rapidfuzz.process.extractOne`` with default WRatio scorer to find
    the best-matching contract file for the given vendor name.

    Args:
        vendor_name: The vendor name string to match, e.g. ``"Acme Corp"``.
        contracts_dir: Path to the directory containing contract files.
        min_confidence: Minimum score (0–100) required to accept a match.
            Defaults to ``80``.

    Returns:
        A ``(contract_path, confidence_score)`` tuple if a match at or above
        ``min_confidence`` is found, where ``contract_path`` is the full
        ``Path`` to the matched file. Returns ``None`` if no match meets the
        threshold.

    Raises:
        FileNotFoundError: If ``contracts_dir`` does not exist.
    """
    stems = get_all_contract_names(contracts_dir)
    if not stems:
        logger.warning("contract_matcher: contracts_dir '%s' is empty", contracts_dir)
        return None

    result = process.extractOne(vendor_name, stems)
    if result is None:
        logger.warning(
            "contract_matcher: no match found for vendor '%s'", vendor_name
        )
        return None

    best_stem, score, _ = result
    if score < min_confidence:
        logger.warning(
            "contract_matcher: best match for '%s' was '%s' (score=%d) — "
            "below threshold %d",
            vendor_name,
            best_stem,
            score,
            min_confidence,
        )
        return None

    # Reconstruct the full path by finding the file whose stem matches.
    matched_path = next(
        p for p in contracts_dir.iterdir() if p.is_file() and p.stem == best_stem
    )
    return matched_path, int(score)
