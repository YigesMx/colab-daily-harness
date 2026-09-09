"""Objective final-publication checks; semantic judgments remain isolated-agent work."""
import importlib.util
import ipaddress
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

from markdown_it import MarkdownIt
from PIL import Image, ImageOps

from ..adapters import verify_artifacts, artifact_receipt
from ..lifecycle import Lifecycle, tree_receipt, verify_tree
from ..storage import StorageError, VALIDATION_CHECKS, digest
from ..storage.files import atomic_write, read_bytes, read_json, relative_name, sha256

CATEGORIES = ("Paper", "News", "Policy")
SECTIONS = {
    "Paper": ["研究问题与贡献", "方法与系统", "实验设置与数据", "结果、限制与结论", "来源链接"],
    "News": ["事件概述", "已确认事实与证据", "影响与后续观察", "来源链接"],
    "Policy": ["政策行动", "适用范围与约束力", "关键条款", "时间线", "影响与待观察事项", "来源链接"],
}
GROUP_FIELDS = {"candidate_id", "title", "category", "group_rank", "group_score", "score_scale", "rating_track"}
FINAL_FIELDS = GROUP_FIELDS | {"authors", "summary", "keywords", "sources", "content_path", "preview_image"}
IMAGE_STAGES = {"Paper": ["figure1", "tex", "pdf", "html", "project", "repository"],
                "News": ["official", "attachments"], "Policy": ["official", "attachments"]}


def require(condition, message):
    if not condition:
        raise StorageError(message)


def exact(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields), "invalid publication interface fields")


def text(value, maximum=1000):
    require(isinstance(value, str) and 0 < len(value.strip()) <= maximum and
            not any(ord(c) < 32 and c not in "\n\t" for c in value), "invalid final text")
    require(not re.search(r"working_tmp|crawl_tmp|file://|/home/|/Users/|Bearer\s|Authorization:|GRIST_API_KEY|javascript:|data:text/html|<script|\{\{", value, re.I),
            "final text contains private/runtime or executable markup")
    return value


def public_url(value):
    text(value, 2048)
    require(value == value.strip() and not re.search(r"[\s\\<>]", value), "unsafe public URL")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        require(parsed.scheme in ("http", "https") and not parsed.username and not parsed.password and
                parsed.port in (None, 80, 443), "public URL must be credential-free HTTP(S)")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            require(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) and "." in host and
                    not host.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".example", ".home.arpa")) and
                    not all(c in "0123456789.xabcdef" for c in host), "nonpublic hostname")
        else:
            require(address.is_global, "nonpublic address")
        require(not any(re.search(r"token|secret|password|api.?key|signature|credential", key, re.I)
                        for key, _ in parse_qsl(parsed.query)), "URL contains authentication parameters")
    except (ValueError, UnicodeError):
        raise StorageError("invalid public URL") from None
    return value


def candidate_id(value):
    require(isinstance(value, str) and len(value) <= 240 and
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})*", value), "unsafe candidate identity")
    return value


def asset_key(value):
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: "_" + m[1].lower(), candidate_id(value))


def image_info(path):
    data = read_bytes(path)
    require(0 < len(data) <= 20 * 1024 * 1024, "invalid display image size")
    try:
        with Image.open(BytesIO(data)) as image:
            fmt, size = image.format, image.size
            require(fmt in {"PNG", "JPEG", "WEBP", "GIF", "AVIF"} and min(size) >= 32 and
                    max(size) <= 16000 and size[0] * size[1] <= 40_000_000, "invalid display image format/dimensions")
            require(not getattr(image, "is_animated", False), "animated display images are not supported")
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()  # decode pixels, not just a magic-byte assertion
    except (OSError, ValueError, Image.DecompressionBombError):
        raise StorageError("display image cannot be decoded") from None
    ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif", "AVIF": "avif"}[fmt]
    require(Path(path).suffix.lower().lstrip(".") in ({"jpg", "jpeg"} if fmt == "JPEG" else {ext}), "image extension/type mismatch")
    return {"extension": ext, "width": size[0], "height": size[1], "bytes": len(data), "sha256": sha256(data)}


def sanitized_display_image(path, target):
    """Decode and deterministically re-encode final pixels without source metadata."""
    data = read_bytes(path)
    try:
        with Image.open(BytesIO(data)) as source:
            require(not getattr(source, "is_animated", False), "animated display images are not supported")
            image = ImageOps.exif_transpose(source)
            image.load()
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "transparency" in source.info else "RGB")
            output = BytesIO()
            image.save(output, format="PNG", optimize=False, compress_level=9)
            sanitized = output.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise StorageError("display image cannot be sanitized") from None
    atomic_write(target, sanitized)
    info = image_info(target)
    require(info["extension"] == "png", "sanitized display image must be PNG")
    return target, info


def markdown(content, category):
    text(content, 150000)
    require("<" not in content and "{{" not in content and not re.search(r"^\s*(---|:::|@import)\s*$", content, re.M),
            "raw HTML, Vue/frontmatter/container directives are forbidden in final Markdown")
    tokens = MarkdownIt("commonmark").parse(content)
    headings, bodies = [], []
    current = None
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            require(token.tag in {"h2", "h3", "h4"}, "final body must not supply page title or deep headings")
            if token.tag == "h2":
                headings.append(tokens[index + 1].content)
                bodies.append([])
                current = len(bodies) - 1
        elif current is not None and token.type in {"inline", "fence", "code_block"}:
            if index and tokens[index - 1].type == "heading_open":
                continue
            bodies[current].append(token.content)
        for child in token.children or []:
            require(child.type not in {"html_inline", "image"}, "body images/HTML must not bypass final asset mapping")
            if child.type == "link_open":
                public_url(child.attrGet("href"))
    require(headings == SECTIONS[category] and all("".join(b).strip() for b in bodies),
            "final category sections must be exact, ordered and nonempty")
    # Reference-style and autolinks are parsed above, not validated with a link regex.
    return content


def grouped(payload):
    require(isinstance(payload, dict) and payload.get("schema_version") == 3 and
            payload.get("quota_contract") == "three-track-v3" and payload.get("selection_limit") == 20,
            "new publication requires grouped schema3/three-track-v3/20")
    script = Path(__file__).parents[2] / ".agents/skills/rating-filter-organize/scripts/validate_grouped_candidates.py"
    spec = importlib.util.spec_from_file_location("publication_grouped_validator", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    require(module.validate_grouped_candidates(payload)["valid"], "existing grouped selection/quota validator rejected artifact")
    for category in CATEGORIES:
        rows = payload["groups"][category]["candidates"]
        require([r["group_rank"] for r in rows] == list(range(1, len(rows) + 1)), "group array order differs from rank")
    return [row for category in CATEGORIES for row in payload["groups"][category]["candidates"]]


def local_file(life, value, beneath=None):
    relative_name(value)
    path = life.workspace_path(life.working / value)
    require(beneath is None or beneath in path.parents, "artifact is outside its isolated generation")
    require(path.is_file() and 0 < path.stat().st_size <= 100 * 1024 * 1024, "required temporary artifact missing/empty/oversized")
    return path


def evidence(life, entries, root, category, dropped=False):
    require(isinstance(entries, list) and 1 <= len(entries) <= 24, "evidence materialization is required")
    urls, kinds = set(), set()
    for entry in entries:
        exact(entry, {"kind", "url", "path"})
        require(entry["kind"] in {"full_text", "official", "supplement", "attempt"}, "invalid evidence kind")
        public_url(entry["url"])
        local_file(life, entry["path"], root)
        urls.add(entry["url"])
        kinds.add(entry["kind"])
    if dropped:
        require(len(urls) >= 2, "drop requires multiple acquisition paths and retained attempt evidence")
    else:
        require(("full_text" if category == "Paper" else "official") in kinds, "required full-text/official evidence missing")


def image_audit(life, audit, root, category):
    exact(audit, {"stages", "nullReason", "selected"})
    require(isinstance(audit["stages"], list) and [s.get("stage") for s in audit["stages"]] == IMAGE_STAGES[category],
            "image acquisition stages are incomplete")
    for stage in audit["stages"]:
        exact(stage, {"stage", "status", "url", "path", "reason"})
        require(stage["status"] in {"found", "unavailable", "unsuitable"}, "image stage must be terminal")
        text(stage["reason"])
        public_url(stage["url"])
        local_file(life, stage["path"], root)  # retained actual response/extraction/attempt log, not a checkbox
    selected = audit["selected"]
    if selected is None:
        text(audit["nullReason"])
        return None, None
    exact(selected, {"path", "url", "kind", "caption"})
    require(selected["kind"] in {"figure", "diagram", "detail"} and audit["nullReason"] is None,
            "image provenance role invalid; generated/logo/avatar/advertisement/full-page images forbidden")
    public_url(selected["url"])
    text(selected["caption"])
    path = local_file(life, selected["path"], root)
    require(any(s["status"] == "found" and s["path"] == selected["path"] for s in audit["stages"]), "selected image lacks found-stage ownership")
    source_info = image_info(path)  # validate declared source type before normalization
    target = life.working / "publication-display" / (source_info["sha256"] + ".png")
    return sanitized_display_image(path, target)


def freeze(config, owner, assembly_path):
    """Only this integration entry point manufactures successful validation checks."""
    life = Lifecycle(config)
    with life.use(owner) as state:
        path = life.workspace_path(assembly_path)
        require(path.name == "publication_set.json", "canonical assembly publication_set required")
        publication_set = read_json(path)
        rows = grouped(publication_set)
        exact(publication_set, {"schema_version", "quota_contract", "cycle_id", "display_date", "selection_limit", "groups"})
        require(publication_set["cycle_id"] == state["cycle_id"] and publication_set["display_date"] == state["input"]["display_date"], "publication differs from frozen cycle/date")
        manifest = read_json(path.parent / "manifest.json")
        exact(manifest, {"interface_version", "cycle_id", "refine_run_id", "assembly_generation_id", "rating_run_id", "contexts", "taxonomy_path", "under_target_reason"})
        require(manifest["interface_version"] == 1 and manifest["cycle_id"] == state["cycle_id"], "assembly interface identity mismatch")
        for key in ("refine_run_id", "assembly_generation_id", "rating_run_id"):
            require(re.fullmatch(r"[A-Za-z0-9_-]{1,100}", manifest[key]), "unsafe run/generation identity")
        expected = life.working / "refine_candidates/runs" / manifest["refine_run_id"] / "assemblies" / manifest["assembly_generation_id"]
        require(path.parent == expected, "noncanonical assembly path")
        rating_dir = life.working / "rating_filter_organize/runs" / manifest["rating_run_id"]
        rating = life.store.run_state(manifest["rating_run_id"])
        require(rating["publish_id"] == state["publish_id"] and rating["kind"] == "rating-coordinator" and rating["owner"] == owner,
                "rating is not owned by this cycle")
        stage = rating["stages"].get("assemble")
        require(stage and stage["status"] == "complete", "rating must actually complete its validated coordinator adapter")
        verify_artifacts(life, stage["payload"])
        verify_tree(rating_dir / "shared", {"sha256": stage["payload"]["checksums"]["shared"],
                    "files": stage["payload"]["counts"]["shared_files"], "bytes": stage["payload"]["counts"]["shared_bytes"]})
        selection = read_json(rating_dir / "grouped_selection.json")
        selected = {r["candidate_id"]: r for r in grouped(selection)}
        require(selection["cycle_id"] == state["cycle_id"], "selection cycle mismatch")
        taxonomy = read_json(local_file(life, manifest["taxonomy_path"], expected))
        exact(taxonomy, {"interface_version", "terms", "assignments", "under_target_reason"})
        terms = taxonomy["terms"]
        require(taxonomy["interface_version"] == 1 and isinstance(terms, list) and 2 <= len(terms) <= 15 and
                len(set(terms)) == len(terms) and all(isinstance(t, str) and 1 <= len(t) <= 40 and re.search(r"[\u4e00-\u9fff]", t) for t in terms),
                "taxonomy must use unique canonical Chinese terms (maximum 15)")
        if len(terms) < 10:
            text(taxonomy["under_target_reason"])
            require(manifest["under_target_reason"] == taxonomy["under_target_reason"], "taxonomy shortage explanation mismatch")
        exact(manifest["contexts"], {"paper", "news", "policy"})
        contexts, successes, drops, assets, final = set(), {}, set(), {}, []
        for category in CATEGORIES:
            track = category.lower()
            ref = manifest["contexts"][track]
            exact(ref, {"run_id", "context_id", "generation_id", "manifest_path"})
            require(re.fullmatch(r"[A-Za-z0-9_-]{1,100}", ref["generation_id"]), "invalid generation identity")
            run = life.store.run_state(ref["run_id"])
            require(run["publish_id"] == state["publish_id"] and run["kind"] == "refine:" + track and
                    run["context"].get("context_id") == ref["context_id"] and run["owner"] == ref["context_id"], "refine context lacks SQLite runtime identity")
            require(ref["context_id"] not in contexts and ref["context_id"] not in stage["payload"]["identity"].values(), "refine contexts must be isolated from siblings/rating")
            contexts.add(ref["context_id"])
            terminal = run["stages"].get("refine")
            require(terminal and terminal["status"] == "complete", "refine runtime must record terminal completion")
            verify_artifacts(life, terminal["payload"])
            root = life.working / "refine_candidates/runs" / manifest["refine_run_id"] / "contexts" / track / "generations" / ref["generation_id"]
            verify_tree(root, {"sha256": terminal["payload"]["checksums"]["generation"],
                              "files": terminal["payload"]["counts"]["generation_files"], "bytes": terminal["payload"]["counts"]["generation_bytes"]})
            context_path = local_file(life, ref["manifest_path"], root)
            require(ref["manifest_path"] in terminal["payload"]["paths"], "terminal checkpoint must bind generation manifest")
            data = read_json(context_path)
            exact(data, {"interface_version", "cycle_id", "refine_run_id", "track", "generation_id", "context_id", "successful", "dropped"})
            require(data["interface_version"] == 1 and all(data[k] == v for k, v in {"cycle_id": state["cycle_id"], "refine_run_id": manifest["refine_run_id"], "track": track, "generation_id": ref["generation_id"], "context_id": ref["context_id"]}.items()), "context manifest identity mismatch")
            expected_ids = [r["candidate_id"] for r in selection["groups"][category]["candidates"]]
            seen = set()
            for item in data["successful"]:
                exact(item, {"candidate_id", "title", "authors", "summary", "sources", "content_path", "evidence", "image_audit"})
                cid = candidate_id(item["candidate_id"])
                require(cid in expected_ids and cid not in seen, "successful candidate ownership/duplication failure")
                seen.add(cid)
                evidence(life, item["evidence"], root, category)
                content = markdown(read_bytes(local_file(life, item["content_path"], root)).decode("utf-8"), category)
                image, info = image_audit(life, item["image_audit"], root, category)
                successes[cid] = (item, content, image, info)
            for item in data["dropped"]:
                exact(item, {"candidate_id", "reason_code", "reason", "evidence"})
                cid = item["candidate_id"]
                require(cid in expected_ids and cid not in seen and item["reason_code"] in {"identity_unconfirmed", "empty_content", "unreadable_content"}, "invalid drop/coverage/ownership")
                text(item["reason"])
                evidence(life, item["evidence"], root, category, dropped=True)
                seen.add(cid)
                drops.add(cid)
            require(seen == set(expected_ids), "refine must exhaust successful/dropped input, including empty tracks")
            require([r["candidate_id"] for r in publication_set["groups"][category]["candidates"]] == [cid for cid in expected_ids if cid not in drops], "assembly changed selected order or success coverage")
        require(set(successes) == {r["candidate_id"] for r in rows} and set(taxonomy["assignments"]) == set(successes), "assembly/taxonomy/success set mismatch")
        for row in rows:
            exact(row, FINAL_FIELDS)
            cid = row["candidate_id"]
            original = selected[cid]
            require(all(row[k] == original[k] for k in ("category", "group_score", "score_scale", "rating_track")), "refine may not reclassify or rescore")
            item, content, image, info = successes[cid]
            require(all(row[k] == item[k] for k in ("title", "authors", "summary", "sources", "content_path")), "assembly may only normalize keywords and compress category rank")
            text(row["title"], 500); text(row["summary"], 5000)
            require(isinstance(row["authors"], list) and len(row["authors"]) <= 100, "invalid authors")
            for author in row["authors"]: text(author, 200)
            require(isinstance(row["sources"], list) and 1 <= len(row["sources"]) <= 20, "sources required")
            for source in row["sources"]:
                exact(source, {"name", "url"}); text(source["name"], 200); public_url(source["url"])
            keywords = row["keywords"]
            require(isinstance(keywords, list) and 2 <= len(keywords) <= 5 and len(set(keywords)) == len(keywords) and
                    set(keywords) <= set(terms) and keywords == taxonomy["assignments"][cid], "invalid final taxonomy mapping")
            require(row["preview_image"] == (item["image_audit"]["selected"]["path"] if image else None), "final image differs from audited image")
            logical = f"images/{asset_key(cid)}/preview.{info['extension']}" if image else None
            if image:
                require(logical not in assets, "image path alias collision")
                assets[logical] = image
            urls = [s["url"] for s in row["sources"]]
            canonical_url = original.get("canonical_url") or urls[0]
            public_url(canonical_url)
            record = {k: row[k] for k in GROUP_FIELDS | {"authors", "summary", "keywords", "sources"}}
            record.update(content=content, preview_image=logical, canonical_url=canonical_url,
                          normalized_arxiv_id=original.get("normalized_arxiv_id"), source_identities=original.get("source_identities") or urls)
            final.append(record)
        require(final, "empty final publication cannot be released")
        publication = {"schema_version": 3, "quota_contract": "three-track-v3", "quota_revision": "policy3", "cycle_id": state["cycle_id"],
                       "display_date": state["input"]["display_date"], "generated_at": state["input"]["window_until"], "records": final}
        # Bounded evidence only: no context manifests, intermediate prose, attempt documents or rating bundles.
        validation = {"publication_sha256": digest(publication), "checks": {key: True for key in VALIDATION_CHECKS},
                      "validator": "publication-interface-v1", "identity": {"rating_run_id": manifest["rating_run_id"], "refine_run_id": manifest["refine_run_id"],
                      **{t + "_context_id": manifest["contexts"][t]["context_id"] for t in ("paper", "news", "policy")}},
                      "counts": {"selected": len(selected), "published": len(final), "dropped": len(drops), "taxonomy": len(terms)},
                      "checksums": {"assembly": sha256(read_bytes(path)), "manifest": sha256(read_bytes(path.parent / "manifest.json")), "taxonomy": digest(taxonomy)}}
        return life.store.freeze_publication(state["cycle_id"], publication, assets, validation)


def complete_refine(config, workspace_owner, run_id, manifest_path):
    """Bind actual completed local material to a previously claimed agent context.

    This does not certify semantic truth or infer that an agent ran from a string.
    The orchestration runtime must claim the authentic context before execution.
    """
    life = Lifecycle(config)
    with life.use(workspace_owner) as state:
        run = life.store.run_state(run_id)
        require(run["publish_id"] == state["publish_id"] and run["kind"].startswith("refine:") and
                run["owner"] == run["context"].get("context_id"), "refine runtime context identity mismatch")
        path = life.workspace_path(manifest_path)
        data = read_json(path)
        root = life.working / "refine_candidates/runs" / data["refine_run_id"] / "contexts" / data["track"] / "generations" / data["generation_id"]
        require(path == root / "generation_manifest.json" and data["cycle_id"] == state["cycle_id"] and
                run["kind"] == "refine:" + data["track"] and run["owner"] == data["context_id"], "noncanonical refine generation")
        receipt = artifact_receipt(life, [path], identity={"generation_id": data["generation_id"]})
        tree = tree_receipt(root)
        receipt["checksums"]["generation"] = tree["sha256"]
        receipt["counts"].update(generation_files=tree["files"], generation_bytes=tree["bytes"])
        old = run["stages"].get("refine")
        if old and old["status"] == "complete":
            require(old["payload"] == receipt, "completed refine material changed or missing")
            return old["revision"]
        revision = old["revision"] if old else 0
        return life.store.checkpoint_stage(run_id, run["owner"], "refine", "final:" + data["generation_id"], revision, "complete", receipt)
