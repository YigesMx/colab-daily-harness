"""Small control-plane metadata only; never intermediate documents or binaries."""
from .files import StorageError, canonical, relative_name

KEYS = {"identity", "paths", "counts", "checksums", "status", "reason", "context_id", "session_id"}


def control_metadata(value):
    if not isinstance(value, dict) or set(value) - KEYS:
        raise StorageError("checkpoint/context permits only bounded control metadata")
    if len(canonical(value).encode()) > 16384:
        raise StorageError("control metadata exceeds 16 KiB")
    for key, item in value.items():
        if key == "paths":
            if not isinstance(item, list) or len(item) > 64 or any(not isinstance(p, str) or not p or len(p) > 1024 or "\n" in p for p in item):
                raise StorageError("invalid bounded metadata paths")
            for path in item:
                relative_name(path)
        elif key in {"identity", "counts", "checksums"}:
            if not isinstance(item, dict) or len(item) > 64:
                raise StorageError("invalid bounded metadata mapping")
            for name, field in item.items():
                if not isinstance(name, str) or len(name) > 128:
                    raise StorageError("invalid metadata key")
                if key == "counts":
                    valid = type(field) is int and field >= 0
                elif key == "checksums":
                    valid = isinstance(field, str) and len(field) == 64 and all(c in "0123456789abcdef" for c in field)
                else:
                    valid = field is None or type(field) in (int, bool) or (isinstance(field, str) and len(field) <= 256 and "\n" not in field)
                if not valid:
                    raise StorageError("invalid control metadata scalar")
        elif not isinstance(item, str) or len(item) > 300 or "\n" in item:
            raise StorageError("invalid bounded control metadata text")
    return value


EVENT_FIELDS = {"key", "source_id", "version", "announce_type", "announce_types", "announcement_at",
                "first_seen_at", "last_seen_at", "source_categories", "status", "cycle_id", "canonical_url"}


def identity_event(event):
    if set(event) - EVENT_FIELDS or len(canonical(event).encode()) > 4096:
        raise StorageError("new source events must contain bounded identity/cursor metadata only")
    for key, value in event.items():
        if key in {"announce_types", "source_categories"}:
            valid = isinstance(value, list) and len(value) <= 32 and all(isinstance(item, str) and len(item) <= 128 and "\n" not in item for item in value)
        else:
            valid = value is None or (isinstance(value, str) and len(value) <= 2048 and "\n" not in value)
        if not valid:
            raise StorageError("source identity fields cannot contain nested documents")
    return event


def cursor_metadata(cursor):
    if not isinstance(cursor, dict) or len(cursor) > 64 or len(canonical(cursor).encode()) > 16384:
        raise StorageError("source cursor must be bounded scalar metadata")
    for key, value in cursor.items():
        if not isinstance(key, str) or len(key) > 128 or key.lower() in {"body", "content", "summary", "files", "base64", "events"}:
            raise StorageError("source cursor cannot contain documents")
        if value is not None and type(value) not in (int, float, bool) and not (isinstance(value, str) and len(value) <= 256 and "\n" not in value):
            raise StorageError("source cursor values must be bounded scalars")
    return cursor
