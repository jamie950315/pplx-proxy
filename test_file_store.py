import concurrent.futures
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from file_store import FileStore


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name) / "files"
        self.store=FileStore(self.root, max_file_bytes=10, max_total_bytes=15)

    def test_create_get_list_delete_survive_restart(self):
        metadata=self.store.create(b"hello", "note.txt")
        self.assertEqual(metadata["object"], "file")
        self.assertEqual(metadata["bytes"], 5)
        self.assertEqual(metadata["purpose"], "user_data")
        restarted=FileStore(self.root)
        self.assertEqual(restarted.get(metadata["id"]), (metadata, b"hello"))
        self.assertEqual(restarted.list(), [metadata])
        self.assertTrue(restarted.delete(metadata["id"]))
        self.assertFalse(restarted.delete(metadata["id"]))
        with self.assertRaises(FileNotFoundError):
            restarted.get(metadata["id"])
        self.assertEqual(restarted.list(), [])

    def test_permissions_are_owner_only(self):
        metadata=self.store.create(b"secret", "note.txt", "assistants")
        entry=self.root / metadata["id"]
        for directory in (self.root, entry):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for file in (entry / "metadata.json", entry / "content", self.root / ".lock"):
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)

    def test_size_limit_and_persistent_total_quota(self):
        with self.assertRaises(OverflowError):
            self.store.create(b"x" * 11, "large.txt")
        first=self.store.create(b"x" * 10, "first.txt")
        restarted=FileStore(self.root, max_file_bytes=10, max_total_bytes=15)
        with self.assertRaises(OverflowError):
            restarted.create(b"y" * 6, "second.txt")
        restarted.create(b"y" * 5, "second.txt")
        restarted.delete(first["id"])
        restarted.create(b"z" * 10, "third.txt")
        self.assertEqual(sum(item["bytes"] for item in restarted.list()), 15)

    def test_concurrent_instances_share_quota(self):
        other=FileStore(self.root, max_file_bytes=10, max_total_bytes=15)
        def upload(store):
            try:
                store.create(b"x" * 10, "note.txt")
                return True
            except OverflowError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(upload, [self.store, other]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.store.list()), 1)

    def test_path_traversal_and_unsupported_inputs_rejected(self):
        for value in ("../secret", "/etc/passwd", "file-../secret", "file-x", None):
            with self.subTest(id=value):
                with self.assertRaises(ValueError):
                    self.store.get(value)
                with self.assertRaises(ValueError):
                    self.store.delete(value)
        for filename in ("../secret", "C:\\secret", "bad\x00name", "", "..", "x" * 256):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                self.store.create(b"x", filename)
        with self.assertRaises(ValueError):
            self.store.create(b"x", "file.txt", "fine-tune")
        with self.assertRaises(ValueError):
            self.store.create("not bytes", "file.txt")

    def test_symlinks_cannot_expose_other_files(self):
        metadata=self.store.create(b"hello", "note.txt")
        content=self.root / metadata["id"] / "content"
        content.unlink()
        content.symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.store.get(metadata["id"])

    def test_read_rejects_corrupt_store_without_fallback(self):
        metadata=self.store.create(b"hello", "note.txt")
        content=self.root / metadata["id"] / "content"
        content.write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "size"):
            self.store.get(metadata["id"])
        with self.assertRaises(ValueError):
            self.store.list()

    def test_oversized_metadata_read_is_bounded(self):
        metadata=self.store.create(b"hello", "note.txt")
        path=self.root / metadata["id"] / "metadata.json"
        path.write_text(" " * 4097)
        with self.assertRaisesRegex(ValueError, "too large"):
            self.store.get(metadata["id"])

    def test_lowered_read_limit_does_not_read_content(self):
        metadata=self.store.create(b"hello", "note.txt")
        restricted=FileStore(self.root, max_file_bytes=4)
        with self.assertRaises(OverflowError):
            restricted.get(metadata["id"])

    def test_failed_publish_leaves_no_partial_file(self):
        with patch.object(Path, "rename", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(OSError, "disk error"):
                self.store.create(b"hello", "note.txt")
        self.assertEqual(self.store.list(), [])
        self.assertEqual([path.name for path in self.root.iterdir()], [".lock"])


if __name__ == "__main__":
    unittest.main()
