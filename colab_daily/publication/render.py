"""Pure final-storage renderer. Ordered schema3 keys are reconstructed explicitly."""
import json
from .validation import CATEGORIES, asset_key, markdown, public_url, require, text, image_info
from ..storage.files import read_bytes


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def render(store, cycle_id):
    frozen = store.export_publication(cycle_id)
    require(frozen["validation"].get("validator") == "publication-interface-v1", "publication was not persisted by the final integration validator")
    p = frozen["publication"]
    day = p["display_date"]
    files, groups, assets = {}, {}, []
    for category in CATEGORIES:
        candidates = []
        rows = [r for r in p["records"] if r["category"] == category]
        for row in rows:
            cid = row["candidate_id"]
            # Stable short slug; business identity is retained in frontmatter/manifest, not decoded into paths.
            from ..storage import digest
            path = f"docs/daily/{day}/{category.lower()}/{row['group_rank']:02d}-{digest(cid)[:16]}.md"
            preview = None
            if row["preview_image"]:
                logical = row["preview_image"]
                blob = store.root / frozen["assets"][logical]["path"]
                # Content-addressed blobs intentionally have no extension. Validate through the image decoder upstream;
                # storage export already verified their bytes. Extension here is the immutable validated logical name.
                extension = logical.rsplit(".", 1)[1]
                public_path = f"docs/public/daily/{day}/assets/{asset_key(cid)}/preview.{extension}"
                files[public_path] = read_bytes(blob)
                preview = "/" + public_path.removeprefix("docs/public/")
                assets.append({"candidate_id": cid, "path": public_path, "bytes": len(files[public_path])})
            fm = {"schemaVersion": 3, "candidateId": cid, "date": day, "category": category,
                  "groupRank": row["group_rank"], "title": row["title"], "authors": row["authors"],
                  "summary": row["summary"], "keywords": row["keywords"], "sources": row["sources"], "previewImage": preview}
            # JSON scalars/arrays are a safe YAML subset. No caller-provided YAML/frontmatter is interpolated.
            header = "---\n" + "\n".join(k + ": " + json.dumps(v, ensure_ascii=False) for k, v in fm.items()) + "\n---\n\n"
            content = markdown(row["content"], category)
            files[path] = (header + content.strip() + "\n").encode()
            candidates.append({"candidate_id": cid, "category": category, "group_rank": row["group_rank"],
                               "path": path, "bytes": len(files[path]), "preview_image": preview})
        groups[category] = {"candidates": candidates}
    counts = {c: len(groups[c]["candidates"]) for c in CATEGORIES}
    news_capacity = 10 - counts["Policy"]
    proof = {"selection_limit": 20, "paper_count": counts["Paper"], "news_count": counts["News"], "policy_count": counts["Policy"],
             "selected_total": len(p["records"]), "news_policy_total": counts["News"] + counts["Policy"], "paper_capacity": 10,
             "policy_capacity": 3, "news_final_capacity": news_capacity, "news_fallback_used": max(0, counts["News"] - 5)}
    manifest = {"schema_version": 3, "quota_contract": "three-track-v3", "quota_revision": "policy3", "cycle_id": p["cycle_id"],
                "display_date": day, "selection_limit": 20, "groups": groups, "assets": assets, "detached_legacy_pages": [],
                "generated_at": p["generated_at"], "public_fields": "rank-display-v1", "quota_proof": proof,
                "compatibility_checks": {"detached_legacy_page_count": 0, "detached_legacy_asset_count": 0}}
    files[f"docs/daily/{day}/.managed-manifest.json"] = json_bytes(manifest)
    # Public build input deliberately excludes private SQLite freeze receipts.
    files["docs/public/release-input.json"] = json_bytes({"schema_version": 2, "display_date": day})
    return files
