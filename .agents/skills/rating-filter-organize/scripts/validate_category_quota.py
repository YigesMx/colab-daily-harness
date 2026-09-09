#!/usr/bin/env python3
"""Validate rating category labels and the release-selection quotas."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path


VALID_CATEGORIES = ("Paper", "News", "Policy")
VALID_CATEGORY_SET = frozenset(VALID_CATEGORIES)
NEWS_POLICY_CATEGORIES = frozenset({"News", "Policy"})
QUOTA_CONTRACTS = frozenset({"two-track-v2", "three-track-v3"})
LEGACY_TOTAL_MAX = 15
LEGACY_NEWS_POLICY_MAX = 5
TOTAL_MAX = 20
PAPER_MAX = 10
NEWS_MAX = 10
POLICY_MAX = 3
NEWS_POLICY_MAX = 10


def _category_values(items: Iterable[object], category_field: str) -> list[object]:
    values = []
    for item in items:
        if isinstance(item, Mapping):
            if category_field in item:
                values.append(item[category_field])
            elif "category" in item:
                values.append(item["category"])
            else:
                values.append(None)
        else:
            values.append(item)
    return values


def _display_invalid(values: Iterable[object]) -> list[str]:
    # repr gives deterministic, unambiguous output for non-string values too.
    return sorted({repr(value) for value in values})


def validate_categories(
    categories: Iterable[object],
    *,
    category_field: str = "category",
    selection_limit: int = TOTAL_MAX,
    quota_contract: str = "three-track-v3",
) -> dict[str, object]:
    """Return a deterministic quota report for category labels.

    Categories are deliberately compared exactly. No case folding, trimming,
    or alias expansion is performed here; semantic classification belongs to
    the rating agents.
    """
    values = _category_values(categories, category_field)
    counts = {category: 0 for category in VALID_CATEGORIES}
    invalid = []
    for value in values:
        if isinstance(value, str) and value in VALID_CATEGORY_SET:
            counts[value] += 1
        else:
            invalid.append(value)

    total_count = len(values)
    news_policy_count = counts["News"] + counts["Policy"]
    legacy = quota_contract == "two-track-v2"
    total_max = LEGACY_TOTAL_MAX if legacy else TOTAL_MAX
    invalid_limit = (
        not isinstance(selection_limit, int)
        or isinstance(selection_limit, bool)
        or (
            not 1 <= selection_limit <= LEGACY_TOTAL_MAX
            if legacy
            else selection_limit != TOTAL_MAX
        )
    )
    if invalid_limit:
        effective_paper_max = 0
    elif legacy:
        effective_paper_max = max(0, selection_limit - news_policy_count)
    else:
        effective_paper_max = PAPER_MAX
    violations = []
    if quota_contract not in QUOTA_CONTRACTS:
        violations.append(f"invalid_quota_contract:{quota_contract!r}")
    if invalid:
        violations.append(
            "invalid_categories:" + ",".join(_display_invalid(invalid))
        )
    news_policy_max = (
        LEGACY_NEWS_POLICY_MAX if legacy else NEWS_POLICY_MAX
    )
    if news_policy_count > news_policy_max:
        violations.append(
            f"news_policy_max_exceeded:{news_policy_count}>{news_policy_max}"
        )
    if not legacy:
        if counts["Policy"] > POLICY_MAX:
            violations.append(f"policy_max_exceeded:{counts['Policy']}>{POLICY_MAX}")
        if counts["News"] > NEWS_MAX:
            violations.append(f"news_max_exceeded:{counts['News']}>{NEWS_MAX}")
    if invalid_limit:
        violations.append(f"invalid_selection_limit:{selection_limit!r}")
    if not invalid_limit and counts["Paper"] > effective_paper_max:
        violations.append(
            f"paper_max_exceeded:{counts['Paper']}>{effective_paper_max}"
        )
    if not invalid_limit and total_count > selection_limit:
        violations.append(f"selection_limit_exceeded:{total_count}>{selection_limit}")
    if total_count > total_max:
        violations.append(f"total_max_exceeded:{total_count}>{total_max}")

    return {
        "valid": not violations,
        "categories": list(VALID_CATEGORIES),
        "counts": counts,
        "total_count": total_count,
        "selection_limit": selection_limit,
        "quota_contract": quota_contract,
        "news_policy_count": news_policy_count,
        "effective_paper_max": effective_paper_max,
        "violations": violations,
    }


def validate_candidate_categories(
    candidates: Sequence[object],
    *,
    category_field: str = "category",
    selection_limit: int = TOTAL_MAX,
    quota_contract: str = "three-track-v3",
) -> dict[str, object]:
    """Validate candidate objects with a frozen total selection limit."""
    return validate_categories(
        candidates,
        category_field=category_field,
        selection_limit=selection_limit,
        quota_contract=quota_contract,
    )


def _items_from_payload(
    payload: object, *, legacy_flat: bool = False
) -> list[object]:
    if isinstance(payload, dict):
        if "groups" in payload:
            groups = payload["groups"]
            if not isinstance(groups, Mapping):
                raise ValueError("input JSON groups must be an object")
            actual_groups = set(groups)
            if actual_groups != VALID_CATEGORY_SET:
                missing = sorted(VALID_CATEGORY_SET - actual_groups)
                unexpected = sorted(actual_groups - VALID_CATEGORY_SET, key=repr)
                raise ValueError(
                    "input JSON groups must contain exactly Paper, News, and Policy; "
                    f"missing={missing!r}, unexpected={unexpected!r}"
                )
            items = []
            for category in VALID_CATEGORIES:
                group = groups[category]
                if not isinstance(group, Mapping):
                    raise ValueError(f"input JSON group {category} must be an object")
                candidates = group.get("candidates")
                if not isinstance(candidates, list):
                    raise ValueError(
                        f"input JSON group {category} must contain a candidates list"
                    )
                items.extend(candidates)
            return items
    if not legacy_flat:
        raise ValueError(
            "input JSON must be a grouped candidates object; use --legacy-flat "
            "to validate a legacy flat input"
        )
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("selected_candidates", "candidates", "records", "categories"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ValueError(
        "legacy flat input must be a list or an object containing "
        "selected_candidates, candidates, records, or categories"
    )


def _load_items(path: Path, *, legacy_flat: bool = False) -> list[object]:
    return _items_from_payload(
        json.loads(path.read_text(encoding="utf-8")), legacy_flat=legacy_flat
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--category-field", default="category")
    parser.add_argument("--selection-limit", type=int)
    parser.add_argument("--legacy-flat", action="store_true")
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    selection_limit = args.selection_limit
    if selection_limit is None:
        selection_limit = (
            payload.get("selection_limit", TOTAL_MAX)
            if isinstance(payload, dict)
            else TOTAL_MAX
        )
    quota_contract = (
        payload.get(
            "quota_contract",
            "two-track-v2" if payload.get("schema_version") == 2 else "three-track-v3",
        )
        if isinstance(payload, dict)
        else "three-track-v3"
    )
    try:
        items = _items_from_payload(payload, legacy_flat=args.legacy_flat)
    except ValueError as error:
        parser.error(str(error))
    report = validate_candidate_categories(
        items,
        category_field=args.category_field,
        selection_limit=selection_limit,
        quota_contract=quota_contract,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
