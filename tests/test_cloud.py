import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from yt_subtitle_extract.cloud import (
    B2CloudStore,
    B2Config,
    _HAS_CRYPTO,
    export_b2_config,
    import_b2_config,
    load_b2_config,
    lock_belongs_to,
    save_b2_config,
)


def make_asr_bytes(
    video_id: str,
    *,
    title: str = "",
    channel: str = "",
    include_manifest: bool = True,
) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"{video_id}/project.json",
            json.dumps(
                {
                    "video_id": video_id,
                    "title": title,
                    "channel": channel,
                }
            ),
        )
        if include_manifest:
            zf.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "projects": [
                            {
                                "video_id": video_id,
                                "title": title,
                                "channel": channel,
                                "folder": video_id,
                            }
                        ],
                    }
                ),
            )
    return payload.getvalue()


class B2CloudStoreListTitlesTests(unittest.TestCase):
    def make_store(self, entry, download_bytes, head_metadata=None):
        store = object.__new__(B2CloudStore)
        store.config = B2Config(folder_prefix="team/")
        store._iter_objects = lambda _prefix: [entry]
        if head_metadata is None:
            store._head_object = lambda _file_name: {}
        else:
            store._head_object = lambda _file_name: {"Metadata": head_metadata}
        store._download_bytes = download_bytes
        return store

    def test_list_titles_falls_back_to_asr_manifest_when_file_info_missing(self):
        entry = {
            "Key": "team/titles/video-123.asr",
            "Size": 1024,
            "LastModified": datetime(2024, 3, 1, tzinfo=timezone.utc),
        }
        archive = make_asr_bytes(
            "video-123",
            title="Recovered Title",
            channel="Recovered Channel",
        )
        store = self.make_store(entry, lambda _file_name: archive)

        titles = store.list_titles()

        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0]["video_id"], "video-123")
        self.assertEqual(titles[0]["title"], "Recovered Title")
        self.assertEqual(titles[0]["channel"], "Recovered Channel")

    def test_list_titles_uses_file_info_without_downloading_archive(self):
        entry = {
            "Key": "team/titles/video-456.asr",
            "Size": 2048,
            "LastModified": datetime(2024, 3, 1, tzinfo=timezone.utc),
        }

        def fail_if_called(_file_name):
            raise AssertionError("archive download should not be needed")

        store = self.make_store(
            entry,
            fail_if_called,
            head_metadata={"title": "Stored Title", "channel": "Stored Channel"},
        )

        titles = store.list_titles()

        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0]["title"], "Stored Title")
        self.assertEqual(titles[0]["channel"], "Stored Channel")

    def test_list_titles_falls_back_to_project_json_when_manifest_missing(self):
        entry = {
            "Key": "team/titles/video-789.asr",
            "Size": 4096,
            "LastModified": datetime(2024, 3, 1, tzinfo=timezone.utc),
        }
        archive = make_asr_bytes(
            "video-789",
            title="Project Title",
            channel="Project Channel",
            include_manifest=False,
        )
        store = self.make_store(entry, lambda _file_name: archive)

        titles = store.list_titles()

        self.assertEqual(len(titles), 1)
        self.assertEqual(titles[0]["title"], "Project Title")
        self.assertEqual(titles[0]["channel"], "Project Channel")


class CloudIdentityTests(unittest.TestCase):
    def test_local_config_round_trip_preserves_user_id(self):
        config = B2Config(
            provider="cloudflare_r2",
            key_id="key-id",
            application_key="secret-key",
            bucket_name="bucket-name",
            endpoint_url="https://example.r2.cloudflarestorage.com",
            region_name="auto",
            addressing_style="path",
            folder_prefix="team/",
            display_name="Alice",
            user_id="alice-123",
            role="admin",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            save_b2_config(workspace, config)
            loaded = load_b2_config(workspace)

        self.assertEqual(loaded.display_name, "Alice")
        self.assertEqual(loaded.user_id, "alice-123")
        self.assertEqual(loaded.role, "admin")
        self.assertEqual(loaded.provider, "cloudflare_r2")
        self.assertEqual(loaded.endpoint_url, "https://example.r2.cloudflarestorage.com")
        self.assertEqual(loaded.region_name, "auto")
        self.assertEqual(loaded.addressing_style, "path")
        self.assertTrue(loaded.allows_admin_role())
        self.assertFalse(loaded.uses_managed_user_config())

    def test_lock_belongs_to_prefers_user_id_when_present(self):
        config = B2Config(display_name="Different Name", user_id="user-123")
        lock = {"user": "Alice", "user_id": "user-123"}

        self.assertTrue(lock_belongs_to(lock, config))

    def test_backblaze_config_derives_endpoint_from_region(self):
        config = B2Config(provider="backblaze_b2", region_name="us-west-004")

        self.assertEqual(
            config.effective_endpoint_url(),
            "https://s3.us-west-004.backblazeb2.com",
        )

    def test_endpoint_host_without_scheme_defaults_to_https(self):
        config = B2Config(
            provider="backblaze_b2",
            endpoint_url="s3.us-east-005.backblazeb2.com",
        )

        self.assertEqual(
            config.effective_endpoint_url(),
            "https://s3.us-east-005.backblazeb2.com",
        )

    def test_local_endpoint_host_without_scheme_defaults_to_http(self):
        config = B2Config(
            provider="minio",
            endpoint_url="localhost:9000",
        )

        self.assertEqual(
            config.effective_endpoint_url(),
            "http://localhost:9000",
        )

    @unittest.skipUnless(_HAS_CRYPTO, "cryptography is not installed")
    def test_user_export_import_uses_imported_identity_not_exporter_identity(self):
        config = B2Config(
            provider="backblaze_b2",
            key_id="key-id",
            application_key="secret-key",
            bucket_name="bucket-name",
            folder_prefix="team/",
            display_name="Alice",
            user_id="alice-123",
            role="admin",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "team.b2cfg"
            export_b2_config(config, dest, "top-secret")
            imported = import_b2_config(
                dest,
                "top-secret",
                display_name="Bob",
                user_id="bob-456",
            )

        self.assertEqual(imported.key_id, "key-id")
        self.assertEqual(imported.application_key, "secret-key")
        self.assertEqual(imported.bucket_name, "bucket-name")
        self.assertEqual(imported.folder_prefix, "team/")
        self.assertEqual(imported.provider, "backblaze_b2")
        self.assertEqual(imported.display_name, "Bob")
        self.assertEqual(imported.user_id, "bob-456")
        self.assertEqual(imported.role, "user")
        self.assertFalse(imported.allows_admin_role())
        self.assertTrue(imported.uses_managed_user_config())
        imported.role = "admin"
        self.assertEqual(imported.normalized_role(), "user")

    @unittest.skipUnless(_HAS_CRYPTO, "cryptography is not installed")
    def test_admin_export_import_preserves_admin_role_but_uses_imported_identity(self):
        config = B2Config(
            provider="minio",
            key_id="key-id",
            application_key="secret-key",
            bucket_name="bucket-name",
            endpoint_url="http://localhost:9000",
            region_name="us-east-1",
            addressing_style="path",
            folder_prefix="team/",
            display_name="Alice",
            user_id="alice-123",
            role="admin",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "team-admin.b2cfg"
            export_b2_config(config, dest, "top-secret", export_role="admin")
            imported = import_b2_config(
                dest,
                "top-secret",
                display_name="Admin Bob",
                user_id="admin-bob",
            )

        self.assertEqual(imported.display_name, "Admin Bob")
        self.assertEqual(imported.user_id, "admin-bob")
        self.assertEqual(imported.provider, "minio")
        self.assertEqual(imported.endpoint_url, "http://localhost:9000")
        self.assertEqual(imported.region_name, "us-east-1")
        self.assertEqual(imported.addressing_style, "path")
        self.assertTrue(imported.allows_admin_role())
        self.assertFalse(imported.uses_managed_user_config())
        self.assertEqual(imported.normalized_role(), "admin")
        self.assertEqual(imported.role, "admin")


if __name__ == "__main__":
    unittest.main()
