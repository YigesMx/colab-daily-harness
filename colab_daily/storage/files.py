"""Private local file primitives. Symlinks and parent traversal fail closed."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile


class StorageError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(value):
    path = Path(value).absolute()
    if ".." in path.parts:
        raise StorageError("parent traversal is forbidden")
    for component in [*reversed(path.parents), path]:
        if component.is_symlink():
            raise StorageError("symlink paths are forbidden")
    return path


def relative_name(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise StorageError("invalid relative asset name")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")):
        raise StorageError("invalid relative asset name")
    return value


def private_dir(value):
    path = safe_path(value)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def read_bytes(path):
    path = safe_path(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        return stream.read()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data, immutable=False):
    path = safe_path(path)
    private_dir(path.parent)
    if immutable and path.exists():
        if read_bytes(path) != data:
            raise StorageError("immutable file changed")
        return
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(path)
        if immutable:
            try:
                os.link(name, path)
            except FileExistsError:
                if read_bytes(path) != data:
                    raise StorageError("immutable file changed")
        else:
            os.replace(name, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise StorageError("duplicate JSON object key")
            result[key] = value
        return result
    return json.loads(read_bytes(path), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(StorageError("non-finite JSON number")))
