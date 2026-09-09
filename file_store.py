"""Bounded local storage for OpenAI file uploads; no upstream side effects."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from uuid import uuid4


_FILE_ID=re.compile(r"file-[0-9a-f]{32}\Z")
_PURPOSES={"user_data", "assistants", "vision"}


class FileStore:
    def __init__(self, root, max_file_bytes=20*1024*1024, max_total_bytes=200*1024*1024):
        if not isinstance(max_file_bytes, int) or isinstance(max_file_bytes, bool) or max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be a positive integer")
        if not isinstance(max_total_bytes, int) or isinstance(max_total_bytes, bool) or max_total_bytes <= 0:
            raise ValueError("max_total_bytes must be a positive integer")
        self.root=Path(root)
        self.max_file_bytes=max_file_bytes
        self.max_total_bytes=max_total_bytes
        self._lock=threading.Lock()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("File storage root must be a real directory")
        self.root.chmod(0o700)

    @contextmanager
    def _locked(self):
        # flock also serializes separate FileStore instances and worker processes.
        with self._lock:
            fd=os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _path(self, file_id):
        if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
            raise ValueError("Invalid file ID")
        path=self.root / file_id
        if path.is_symlink():
            raise ValueError("File entry must not be a symlink")
        return path

    def _metadata(self, path):
        metadata_path=path / "metadata.json"
        content_path=path / "content"
        if metadata_path.is_symlink() or content_path.is_symlink():
            raise ValueError("File entry must not contain symlinks")
        with metadata_path.open("rb") as handle:
            raw=handle.read(4097)
        if len(raw) > 4096:
            raise ValueError("Stored file metadata is too large")
        metadata=json.loads(raw)
        if not isinstance(metadata, dict) or metadata.get("id") != path.name or metadata.get("object") != "file":
            raise ValueError("Stored file metadata is invalid")
        size=metadata.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("Stored file size is invalid")
        if not content_path.is_file() or content_path.stat().st_size != size:
            raise ValueError("Stored file content size does not match metadata")
        return metadata

    def _list(self):
        result=[]
        for path in self.root.iterdir():
            if path.name.startswith("file-"):
                result.append(self._metadata(self._path(path.name)))
        return sorted(result, key=lambda item: (item["created_at"], item["id"]), reverse=True)

    def create(self, data, filename, purpose="user_data"):
        if not isinstance(data, bytes):
            raise ValueError("File content must be bytes")
        if not data:
            raise ValueError("File content must not be empty")
        if len(data) > self.max_file_bytes:
            raise OverflowError("File exceeds the upload size limit")
        if not isinstance(filename, str) or not filename or filename in (".", ".."):
            raise ValueError("A filename is required")
        if len(filename.encode("utf-8")) > 255 or any(ord(ch) < 32 or ch in "/\\" for ch in filename):
            raise ValueError("Filename must be a plain name of at most 255 UTF-8 bytes")
        if not isinstance(purpose, str) or purpose not in _PURPOSES:
            raise ValueError("Supported file purposes: user_data, assistants, vision")
        with self._locked():
            total=sum(item["bytes"] for item in self._list())
            if total + len(data) > self.max_total_bytes:
                raise OverflowError("File storage quota exceeded")
            file_id="file-" + uuid4().hex
            metadata={"id": file_id, "object": "file", "bytes": len(data), "created_at": int(time.time()), "filename": filename, "purpose": purpose, "status": "processed", "status_details": None}
            # Publishing the directory exposes both metadata and bytes together.
            with tempfile.TemporaryDirectory(prefix=".upload-", dir=self.root) as temporary:
                directory=Path(temporary)
                for name, content in (("metadata.json", json.dumps(metadata).encode()), ("content", data)):
                    fd=os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                directory.rename(self.root / file_id)
            return metadata

    def get(self, file_id):
        path=self._path(file_id)
        with self._locked():
            metadata=self._metadata(path)
            if metadata["bytes"] > self.max_file_bytes:
                raise OverflowError("Stored file exceeds the current file size limit")
            with (path / "content").open("rb") as handle:
                data=handle.read(self.max_file_bytes + 1)
            if len(data) != metadata["bytes"]:
                raise ValueError("Stored file changed while being read")
            return metadata, data

    def list(self):
        with self._locked():
            return self._list()

    def delete(self, file_id):
        path=self._path(file_id)
        with self._locked():
            if not path.exists():
                return False
            shutil.rmtree(path)
            return True
