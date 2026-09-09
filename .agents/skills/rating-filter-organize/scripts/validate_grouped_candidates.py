#!/usr/bin/env python3
"""Validate frozen category-grouped rating candidates deterministically."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from validate_category_quota import (  # noqa: E402
    TOTAL_MAX,
    VALID_CATEGORIES,
    VALID_CATEGORY_SET,
    validate_candidate_categories,
)


GROUP_CONTRACTS = {
    "two-track-v2": {
        "Paper": {
            "score_scale": "paper-v2",
            "rating_track": "paper",
            "score_min": 0,
            "score_max": 100,
        },
        "News": {
            "score_scale": "news-policy-v2",
            "rating_track": "news_policy",
            "score_min": 0,
            "score_max": 100,
        },
        "Policy": {
            "score_scale": "news-policy-v2",
            "rating_track": "news_policy",
            "score_min": 0,
            "score_max": 100,
        },
    },
    "three-track-v3": {
        "Paper": {
            "score_scale": "paper-v2",
            "rating_track": "paper",
            "score_min": 0,
            "score_max": 100,
        },
        "News": {
            "score_scale": "news-v3",
            "rating_track": "news",
            "score_min": 0,
            "score_max": 100,
        },
        "Policy": {
            "score_scale": "policy-v3",
            "rating_track": "policy",
            "score_min": 0,
            "score_max": 100,
        },
    },
}
GROUP_FIELDS = frozenset({"candidates"})
FORBIDDEN_RANK_SCORE_FIELDS = frozenset(
    {
        "rank",
        "score",
        "global_rank",
        "final_rank",
        "combined_rank",
        "globalRank",
        "finalRank",
        "combinedRank",
        "global_score",
        "final_score",
        "combined_score",
        "globalScore",
        "finalScore",
        "combinedScore",
    }
)


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _selection_limit(payload: object, override: object | None) -> object:
    if override is not None:
        return override
    if isinstance(payload, Mapping):
        return payload.get("selection_limit")
    return None


def _forbidden_field_violations(value: object, path: str = "$.") -> list[str]:
    violations = []
    if isinstance(value, Mapping):
        for key in sorted(value, key=repr):
            field_path = f"{path}{key}" if path == "$." else f"{path}.{key}"
            if key in FORBIDDEN_RANK_SCORE_FIELDS:
                violations.append(f"forbidden_rank_score_field:{field_path}")
            violations.extend(_forbidden_field_violations(value[key], field_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            violations.extend(_forbidden_field_violations(item, f"{path}[{index}]"))
    return violations


def validate_grouped_candidates(
    payload: object, *, selection_limit: object | None = None
) -> dict[str, object]:
    """Return structural and quota validation for one grouped artifact.

    Scores are checked against their declared scale independently. They are
    never compared across groups because the scales are independent.
    """
    violations: list[str] = []
    candidates: list[object] = []
    group_reports: dict[str, dict[str, object]] = {}
    seen_candidate_ids: dict[str, str] = {}

    groups = payload.get("groups") if isinstance(payload, Mapping) else None
    if not isinstance(payload, Mapping):
        violations.append("artifact_must_be_object")
        schema_version = None
        quota_contract = "three-track-v3"
    else:
        schema_version = payload.get("schema_version")
        if schema_version not in (2, 3):
            violations.append("schema_version_must_equal_2_or_3")
        if schema_version == 2:
            quota_contract = payload.get("quota_contract", "two-track-v2")
            if quota_contract != "two-track-v2":
                violations.append("v2_quota_contract_must_be_two-track-v2")
        else:
            quota_contract = payload.get("quota_contract", "three-track-v3")
            if quota_contract != "three-track-v3":
                violations.append("v3_quota_contract_must_be_three-track-v3")
        if "selection_limit" not in payload:
            violations.append("selection_limit_is_required")
        violations.extend(_forbidden_field_violations(payload))
    contracts = GROUP_CONTRACTS[quota_contract if quota_contract in GROUP_CONTRACTS else "three-track-v3"]
    if not isinstance(groups, Mapping):
        violations.append("groups_must_be_object")
        groups = {}

    actual_groups = set(groups)
    if actual_groups != VALID_CATEGORY_SET:
        missing = sorted(VALID_CATEGORY_SET - actual_groups)
        unexpected = sorted(actual_groups - VALID_CATEGORY_SET, key=repr)
        if missing:
            violations.append("missing_groups:" + ",".join(missing))
        if unexpected:
            violations.append(
                "unexpected_groups:" + ",".join(repr(value) for value in unexpected)
            )
    if isinstance(groups, Mapping) and tuple(groups) != tuple(VALID_CATEGORIES):
        violations.append("groups_must_use_fixed_Paper_News_Policy_order")

    for category in VALID_CATEGORIES:
        group = groups.get(category)
        group_candidates: list[object] = []
        if not isinstance(group, Mapping):
            violations.append(f"{category}.group_must_be_object")
        else:
            actual_fields = set(group)
            if actual_fields != GROUP_FIELDS:
                missing = sorted(GROUP_FIELDS - actual_fields)
                unexpected = sorted(actual_fields - GROUP_FIELDS, key=repr)
                if missing:
                    violations.append(
                        f"{category}.missing_group_fields:" + ",".join(missing)
                    )
                if unexpected:
                    violations.append(
                        f"{category}.unexpected_group_fields:"
                        + ",".join(repr(value) for value in unexpected)
                    )
            value = group.get("candidates")
            if not isinstance(value, list):
                violations.append(f"{category}.candidates_must_be_list")
            else:
                group_candidates = value

        candidates.extend(group_candidates)
        valid_ranks: list[int] = []
        paper_emphasis: list[tuple[int, str]] = []
        contract = contracts[category]

        for index, candidate in enumerate(group_candidates):
            location = f"{category}.candidates[{index}]"
            if not isinstance(candidate, Mapping):
                violations.append(f"{location}.must_be_object")
                continue

            candidate_id = candidate.get("candidate_id")
            valid_candidate_id = isinstance(candidate_id, str) and bool(
                candidate_id.strip()
            )
            if not valid_candidate_id:
                violations.append(f"{location}.candidate_id_must_be_nonempty")
            elif candidate_id in seen_candidate_ids:
                violations.append(
                    f"duplicate_candidate_id:{candidate_id!r}:"
                    f"{seen_candidate_ids[candidate_id]}:{location}"
                )
            else:
                seen_candidate_ids[candidate_id] = location

            title = candidate.get("title")
            if not isinstance(title, str) or not title.strip():
                violations.append(f"{location}.title_must_be_nonempty")

            if candidate.get("category") != category:
                violations.append(
                    f"{location}.category_mismatch:"
                    f"{candidate.get('category')!r}!={category!r}"
                )

            group_rank = candidate.get("group_rank")
            valid_rank = _is_positive_integer(group_rank)
            if not valid_rank:
                violations.append(f"{location}.group_rank_must_be_positive_integer")
            else:
                valid_ranks.append(group_rank)
                if category == "Paper" and group_rank <= 3 and valid_candidate_id:
                    paper_emphasis.append((group_rank, candidate_id))

            group_score = candidate.get("group_score")
            if not _is_finite_number(group_score):
                violations.append(f"{location}.group_score_must_be_finite_number")
            elif not contract["score_min"] <= group_score <= contract["score_max"]:
                violations.append(
                    f"{location}.group_score_out_of_range:"
                    f"{group_score!r}_not_in_"
                    f"{contract['score_min']}..{contract['score_max']}"
                )
            if candidate.get("score_scale") != contract["score_scale"]:
                violations.append(
                    f"{location}.score_scale_mismatch:"
                    f"{candidate.get('score_scale')!r}!={contract['score_scale']!r}"
                )
            if candidate.get("rating_track") != contract["rating_track"]:
                violations.append(
                    f"{location}.rating_track_mismatch:"
                    f"{candidate.get('rating_track')!r}!={contract['rating_track']!r}"
                )

            if "emphasis" in candidate:
                emphasis = candidate["emphasis"]
                if not isinstance(emphasis, bool):
                    violations.append(f"{location}.emphasis_must_be_boolean")
                else:
                    expected = category == "Paper" and valid_rank and group_rank <= 3
                    if emphasis != expected:
                        violations.append(
                            f"{location}.emphasis_mismatch:{emphasis!r}!={expected!r}"
                        )

        if len(set(valid_ranks)) != len(valid_ranks):
            violations.append(f"{category}.group_ranks_must_be_unique")
        if len(valid_ranks) == len(group_candidates):
            if sorted(valid_ranks) != list(range(1, len(group_candidates) + 1)):
                violations.append(f"{category}.group_ranks_must_be_contiguous_from_1")

        group_reports[category] = {
            "count": len(group_candidates),
            "emphasis_candidate_ids": [
                candidate_id for _, candidate_id in sorted(paper_emphasis)
            ],
        }

    limit = _selection_limit(payload, selection_limit)
    quota_validation = validate_candidate_categories(
        candidates,
        selection_limit=limit,
        quota_contract=quota_contract,
    )
    return {
        "valid": not violations and quota_validation["valid"],
        "schema_version": schema_version,
        "quota_contract": quota_contract,
        "selection_limit": limit,
        "candidate_count": len(candidates),
        "groups": group_reports,
        "quota_validation": quota_validation,
        "violations": violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--selection-limit", type=int)
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    report = validate_grouped_candidates(
        payload,
        selection_limit=args.selection_limit,
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
