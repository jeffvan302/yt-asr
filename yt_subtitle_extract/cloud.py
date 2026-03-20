"""
cloud.py
--------
S3-compatible cloud storage integration for yt-subtitle-extract.

Provides:
  - B2Config  provider-aware cloud config (legacy name kept for compatibility)
  - B2CloudStore  S3-compatible object storage wrapper (legacy name kept for compatibility)
  - CloudPanel  Toplevel window for all cloud operations

Bucket layout
~~~~~~~~~~~~~
  {folder_prefix}titles/{video_id}.asr     # .asr package for each title
  {folder_prefix}locks/{video_id}.lock     # JSON lock file while checked out

Optional dependencies (install via  pip install ".[cloud]"):
  boto3>=1.34         S3-compatible object storage client
  cryptography>=42    Fernet encryption for .b2cfg export/import
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import queue
import socket
import threading
import time
import uuid
import zipfile
from dataclasses import MISSING, asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

# ---------------------------------------------------------------------------
# Optional heavy dependencies
# ---------------------------------------------------------------------------

try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

try:
    import boto3  # type: ignore[import]
    from botocore.config import Config as BotocoreConfig  # type: ignore[import]
    from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import]
    _HAS_S3 = True
except ImportError:
    _HAS_S3 = False
    boto3 = None  # type: ignore[assignment]
    BotocoreConfig = None  # type: ignore[assignment]
    BotoCoreError = Exception  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[assignment]

_HAS_B2 = _HAS_S3


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

B2_CONFIG_FILE   = ".b2_config.json"
B2_STATE_FILE    = ".cloud_state.json"
B2_CFG_EXTENSION = ".b2cfg"
_PBKDF2_ITERS    = 480_000
_NO_SUCH_KEY_CODES = {"NoSuchKey", "404", "NotFound"}

PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "backblaze_b2": {
        "label": "Backblaze B2",
        "endpoint_url": "",
        "region_name": "",
        "addressing_style": "virtual",
        "endpoint_hint": "Set Region (for example us-west-004) or enter the Backblaze S3 host or full URL.",
    },
    "aws_s3": {
        "label": "Amazon S3",
        "endpoint_url": "",
        "region_name": "us-east-1",
        "addressing_style": "virtual",
        "endpoint_hint": "AWS uses the standard S3 endpoint when Endpoint URL is blank.",
    },
    "cloudflare_r2": {
        "label": "Cloudflare R2",
        "endpoint_url": "https://<accountid>.r2.cloudflarestorage.com",
        "region_name": "auto",
        "addressing_style": "path",
        "endpoint_hint": "Replace <accountid> with your R2 account endpoint.",
    },
    "minio": {
        "label": "MinIO",
        "endpoint_url": "http://localhost:9000",
        "region_name": "us-east-1",
        "addressing_style": "path",
        "endpoint_hint": "Use your MinIO server URL and bucket.",
    },
    "generic_s3": {
        "label": "Generic S3 Compatible",
        "endpoint_url": "",
        "region_name": "",
        "addressing_style": "auto",
        "endpoint_hint": "Enter the provider's S3-compatible host or full endpoint URL if required.",
    },
}
PROVIDER_LABEL_TO_KEY = {
    preset["label"]: provider_key
    for provider_key, preset in PROVIDER_PRESETS.items()
}
ADDRESSING_STYLE_CHOICES = ["auto", "virtual", "path"]


# ---------------------------------------------------------------------------
# Cloud config dataclass (legacy B2Config name kept for compatibility)
# ---------------------------------------------------------------------------

@dataclass
class B2Config:
    provider:        str = "backblaze_b2"
    key_id:          str = ""
    application_key: str = ""
    bucket_name:     str = ""
    endpoint_url:    str = ""
    region_name:     str = ""
    addressing_style: str = "virtual"
    folder_prefix:   str = ""   # e.g. "team-project/" or ""
    display_name:    str = ""   # shown in lock files
    user_id:         str = ""   # stable identity used for ownership
    role:            str = "user"   # trusted-team role: user | admin
    allow_admin_role: str = "1"   # imported user configs lock this to false

    def is_valid(self) -> bool:
        if not (self.key_id and self.application_key and self.bucket_name):
            return False
        if self.normalized_provider() == "aws_s3":
            return True
        return bool(self.effective_endpoint_url())

    def has_identity(self) -> bool:
        return bool(self.user_id.strip())

    def allows_admin_role(self) -> bool:
        value = self.allow_admin_role.strip().lower()
        return value not in {"", "0", "false", "no", "off"}

    def uses_managed_user_config(self) -> bool:
        return not self.allows_admin_role()

    def normalized_role(self) -> str:
        if self.role.strip().lower() == "admin" and self.allows_admin_role():
            return "admin"
        return "user"

    def normalized_provider(self) -> str:
        value = self.provider.strip()
        if value in PROVIDER_PRESETS:
            return value
        return "backblaze_b2"

    def normalized_addressing_style(self) -> str:
        value = self.addressing_style.strip().lower()
        if value in ADDRESSING_STYLE_CHOICES:
            return value
        preset = PROVIDER_PRESETS[self.normalized_provider()]
        return preset.get("addressing_style", "auto")

    def normalized_endpoint_url(self) -> str:
        endpoint = self.endpoint_url.strip().rstrip("/")
        if not endpoint:
            return ""
        if "://" in endpoint:
            return endpoint
        lower = endpoint.lower()
        if lower.startswith(("localhost", "127.", "[::1]")):
            return f"http://{endpoint}"
        return f"https://{endpoint}"

    def effective_endpoint_url(self) -> str:
        endpoint = self.normalized_endpoint_url()
        if endpoint:
            return endpoint
        if self.normalized_provider() == "backblaze_b2" and self.region_name.strip():
            return f"https://s3.{self.region_name.strip()}.backblazeb2.com"
        return ""

    @property
    def titles_prefix(self) -> str:
        p = self.folder_prefix
        return f"{p}titles/" if p else "titles/"

    @property
    def locks_prefix(self) -> str:
        p = self.folder_prefix
        return f"{p}locks/" if p else "locks/"

    @property
    def audit_prefix(self) -> str:
        p = self.folder_prefix
        return f"{p}audit/" if p else "audit/"


def provider_label_for_key(provider_key: str) -> str:
    preset = PROVIDER_PRESETS.get(provider_key, PROVIDER_PRESETS["backblaze_b2"])
    return preset["label"]


def provider_key_for_label(label: str) -> str:
    return PROVIDER_LABEL_TO_KEY.get(label, "backblaze_b2")


def _config_field_defaults() -> dict[str, str]:
    defaults: dict[str, str] = {}
    for field_name, field_info in B2Config.__dataclass_fields__.items():
        if field_info.default is not MISSING:
            defaults[field_name] = str(field_info.default)
        else:
            defaults[field_name] = ""
    return defaults


def actor_name_for_config(config: B2Config) -> str:
    return config.display_name.strip() or socket.gethostname()


def config_is_admin(config: B2Config) -> bool:
    return config.normalized_role() == "admin"


def lock_owner_label(lock: dict[str, Any] | None) -> str:
    if not isinstance(lock, dict):
        return "unknown user"
    user = str(lock.get("user") or "").strip()
    user_id = str(lock.get("user_id") or "").strip()
    if user and user_id:
        return f"{user} ({user_id})"
    return user or user_id or "unknown user"


def lock_belongs_to(lock: dict[str, Any] | None, config: B2Config) -> bool:
    if not isinstance(lock, dict):
        return False
    lock_user_id = str(lock.get("user_id") or "").strip()
    config_user_id = config.user_id.strip()
    if lock_user_id and config_user_id:
        return lock_user_id == config_user_id
    lock_user = str(lock.get("user") or "").strip()
    return bool(lock_user and lock_user == actor_name_for_config(config))


# ---------------------------------------------------------------------------
# Config persistence (local workspace file)
# ---------------------------------------------------------------------------

def load_b2_config(workspace: Path) -> B2Config:
    path = workspace / B2_CONFIG_FILE
    if not path.exists():
        return B2Config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        defaults = _config_field_defaults()
        values = {k: str(data.get(k, defaults[k])) for k in defaults}
        return B2Config(**values)
    except Exception:
        return B2Config()


def save_b2_config(workspace: Path, config: B2Config) -> None:
    path = workspace / B2_CONFIG_FILE
    path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Local cloud-state persistence
# ---------------------------------------------------------------------------

def load_cloud_state(workspace: Path) -> dict[str, dict[str, Any]]:
    path = workspace / B2_STATE_FILE
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    raw_titles = payload.get("titles")
    if not isinstance(raw_titles, dict):
        return {}

    state: dict[str, dict[str, Any]] = {}
    for video_id, raw in raw_titles.items():
        if not isinstance(video_id, str) or not isinstance(raw, dict):
            continue
        state[video_id] = {
            "cloud_state": str(raw.get("cloud_state") or "").strip(),
            "lock_user": str(raw.get("lock_user") or "").strip(),
            "lock_user_id": str(raw.get("lock_user_id") or "").strip(),
            "lock_role": str(raw.get("lock_role") or "").strip(),
            "title": str(raw.get("title") or "").strip(),
            "channel": str(raw.get("channel") or "").strip(),
            "uploaded_at": float(raw.get("uploaded_at") or 0.0),
            "last_synced_updated_at": float(raw.get("last_synced_updated_at") or 0.0),
        }
    return state


def save_cloud_state(workspace: Path, state: dict[str, dict[str, Any]]) -> None:
    path = workspace / B2_STATE_FILE
    payload = {
        "version": 1,
        "titles": {
            video_id: {
                "cloud_state": str(entry.get("cloud_state") or ""),
                "lock_user": str(entry.get("lock_user") or ""),
                "lock_user_id": str(entry.get("lock_user_id") or ""),
                "lock_role": str(entry.get("lock_role") or ""),
                "title": str(entry.get("title") or ""),
                "channel": str(entry.get("channel") or ""),
                "uploaded_at": float(entry.get("uploaded_at") or 0.0),
                "last_synced_updated_at": float(entry.get("last_synced_updated_at") or 0.0),
            }
            for video_id, entry in sorted(state.items())
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def update_cloud_state_entry(
    workspace: Path,
    video_id: str,
    **updates: Any,
) -> dict[str, dict[str, Any]]:
    state = load_cloud_state(workspace)
    entry = dict(state.get(video_id, {}))
    entry.update(updates)
    state[video_id] = entry
    save_cloud_state(workspace, state)
    return state


def remove_cloud_state_entry(workspace: Path, video_id: str) -> dict[str, dict[str, Any]]:
    state = load_cloud_state(workspace)
    state.pop(video_id, None)
    save_cloud_state(workspace, state)
    return state


def refresh_cloud_state_from_listing(
    workspace: Path,
    titles: list[dict[str, Any]],
    locks: dict[str, dict[str, Any]],
    config: B2Config,
) -> dict[str, dict[str, Any]]:
    current = load_cloud_state(workspace)
    refreshed: dict[str, dict[str, Any]] = {}

    for title_info in titles:
        video_id = str(title_info.get("video_id") or "").strip()
        if not video_id:
            continue
        previous = current.get(video_id, {})
        lock = locks.get(video_id)
        lock_user = str((lock or {}).get("user") or "").strip()
        lock_user_id = str((lock or {}).get("user_id") or "").strip()
        lock_role = str((lock or {}).get("role") or "").strip()
        cloud_state = "checked_in"
        if lock:
            cloud_state = "checked_out_self" if lock_belongs_to(lock, config) else "checked_out_other"
        refreshed[video_id] = {
            "cloud_state": cloud_state,
            "lock_user": lock_user,
            "lock_user_id": lock_user_id,
            "lock_role": lock_role,
            "title": str(title_info.get("title") or previous.get("title") or "").strip(),
            "channel": str(title_info.get("channel") or previous.get("channel") or "").strip(),
            "uploaded_at": float(title_info.get("uploaded_at") or previous.get("uploaded_at") or 0.0),
            "last_synced_updated_at": float(previous.get("last_synced_updated_at") or 0.0),
        }

    save_cloud_state(workspace, refreshed)
    return refreshed


def filter_uploadable_summaries(
    local_summaries: list[dict[str, Any]],
    cloud_titles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cloud_ids = {
        str(item.get("video_id") or "").strip()
        for item in cloud_titles
        if str(item.get("video_id") or "").strip()
    }
    blocked_states = {"checked_in", "checked_out_self", "checked_out_other"}
    filtered: list[dict[str, Any]] = []
    for summary in local_summaries:
        video_id = str(summary.get("video_id") or "").strip()
        if not video_id:
            continue
        if video_id in cloud_ids:
            continue
        cloud_state = str(summary.get("cloud_state") or "").strip()
        if cloud_state in blocked_states:
            continue
        filtered.append(summary)
    return filtered


# ---------------------------------------------------------------------------
# Encrypted config export / import (.b2cfg files)
# ---------------------------------------------------------------------------

def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _normalized_shared_role(value: str) -> str:
    return "admin" if str(value or "").strip().lower() == "admin" else "user"


def _shared_config_for_export(config: B2Config, export_role: str = "user") -> B2Config:
    normalized_role = _normalized_shared_role(export_role)
    return B2Config(
        provider=config.normalized_provider(),
        key_id=config.key_id,
        application_key=config.application_key,
        bucket_name=config.bucket_name,
        endpoint_url=config.endpoint_url,
        region_name=config.region_name,
        addressing_style=config.normalized_addressing_style(),
        folder_prefix=config.folder_prefix,
        display_name="",
        user_id="",
        role=normalized_role,
        allow_admin_role="1" if normalized_role == "admin" else "0",
    )


def _sanitized_imported_config(
    data: dict[str, Any],
    *,
    display_name: str = "",
    user_id: str = "",
) -> B2Config:
    defaults = _config_field_defaults()
    imported = B2Config(**{k: str(data.get(k, defaults[k])) for k in defaults})
    allow_admin_role = "1" if imported.allows_admin_role() else "0"
    normalized_role = "admin" if imported.normalized_role() == "admin" else "user"
    return B2Config(
        provider=imported.normalized_provider(),
        key_id=imported.key_id,
        application_key=imported.application_key,
        bucket_name=imported.bucket_name,
        endpoint_url=imported.endpoint_url,
        region_name=imported.region_name,
        addressing_style=imported.normalized_addressing_style(),
        folder_prefix=imported.folder_prefix,
        display_name=display_name.strip(),
        user_id=user_id.strip(),
        role=normalized_role,
        allow_admin_role=allow_admin_role,
    )


def export_b2_config(
    config: B2Config,
    dest: Path,
    password: str,
    *,
    export_role: str = "user",
) -> None:
    """Encrypt *config* with *password* and write to *dest* (.b2cfg)."""
    if not _HAS_CRYPTO:
        raise RuntimeError(
            "The 'cryptography' package is required.\n"
            "Run:  pip install \".[cloud]\""
        )
    salt      = os.urandom(16)
    key       = _derive_key(password, salt)
    plaintext = json.dumps(
        asdict(_shared_config_for_export(config, export_role))
    ).encode("utf-8")
    encrypted = Fernet(key).encrypt(plaintext)
    payload   = {
        "v":    1,
        "salt": base64.b64encode(salt).decode(),
        "data": encrypted.decode(),
    }
    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def import_b2_config(
    src: Path,
    password: str,
    *,
    display_name: str = "",
    user_id: str = "",
) -> B2Config:
    """Decrypt a .b2cfg file; raises ValueError on wrong password."""
    if not _HAS_CRYPTO:
        raise RuntimeError(
            "The 'cryptography' package is required.\n"
            "Run:  pip install \".[cloud]\""
        )
    payload = json.loads(src.read_text(encoding="utf-8"))
    salt    = base64.b64decode(payload["salt"])
    key     = _derive_key(password, salt)
    try:
        plaintext = Fernet(key).decrypt(payload["data"].encode())
    except InvalidToken:
        raise ValueError("Incorrect password or corrupted file.")
    data   = json.loads(plaintext)
    return _sanitized_imported_config(
        data,
        display_name=display_name,
        user_id=user_id,
    )


# ---------------------------------------------------------------------------
# Cloud store - S3-compatible object storage wrapper
# ---------------------------------------------------------------------------

class B2CloudStore:
    """All object-storage operations the cloud panel needs."""

    def __init__(self, config: B2Config) -> None:
        if not _HAS_S3:
            raise RuntimeError(
                "The 'boto3' package is required.\n"
                "Run:  pip install \".[cloud]\""
            )
        self.config = config

        client_kwargs: dict[str, Any] = {
            "service_name": "s3",
            "aws_access_key_id": config.key_id,
            "aws_secret_access_key": config.application_key,
        }
        if config.region_name.strip():
            client_kwargs["region_name"] = config.region_name.strip()

        endpoint_url = config.effective_endpoint_url()
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url

        addressing_style = config.normalized_addressing_style()
        if addressing_style != "auto":
            client_kwargs["config"] = BotocoreConfig(
                s3={"addressing_style": addressing_style}
            )

        self.client = boto3.client(**client_kwargs)

    # ------------------------------------------------------------------ helpers

    def _download_bytes(self, file_name: str) -> bytes:
        response = self.client.get_object(
            Bucket=self.config.bucket_name,
            Key=file_name,
        )
        return response["Body"].read()

    def _upload_bytes(
        self,
        data: bytes,
        file_name: str,
        content_type: str = "application/octet-stream",
        file_info: dict[str, str] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "Bucket": self.config.bucket_name,
            "Key": file_name,
            "Body": data,
            "ContentType": content_type,
        }
        if file_info:
            kwargs["Metadata"] = file_info
        self.client.put_object(**kwargs)

    def _iter_objects(self, prefix: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        continuation_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self.config.bucket_name,
                "Prefix": prefix,
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = self.client.list_objects_v2(**kwargs)
            for item in response.get("Contents") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("Key") or "")
                if not key.startswith(prefix):
                    continue
                relative = key[len(prefix):]
                if not relative or "/" in relative:
                    continue
                results.append(item)
            if not response.get("IsTruncated"):
                break
            continuation_token = str(response.get("NextContinuationToken") or "")
            if not continuation_token:
                break
        return results

    def _head_object(self, file_name: str) -> dict[str, Any]:
        return self.client.head_object(
            Bucket=self.config.bucket_name,
            Key=file_name,
        )

    def _delete_file(self, file_name: str) -> None:
        """Delete all known versions of *file_name*, falling back to a plain delete."""
        deleted_version = False
        try:
            key_marker: str | None = None
            version_marker: str | None = None
            while True:
                kwargs: dict[str, Any] = {
                    "Bucket": self.config.bucket_name,
                    "Prefix": file_name,
                }
                if key_marker:
                    kwargs["KeyMarker"] = key_marker
                if version_marker:
                    kwargs["VersionIdMarker"] = version_marker
                response = self.client.list_object_versions(**kwargs)

                for section_name in ("Versions", "DeleteMarkers"):
                    for item in response.get(section_name) or []:
                        if not isinstance(item, dict) or item.get("Key") != file_name:
                            continue
                        delete_kwargs: dict[str, Any] = {
                            "Bucket": self.config.bucket_name,
                            "Key": file_name,
                        }
                        version_id = item.get("VersionId")
                        if version_id not in (None, "null"):
                            delete_kwargs["VersionId"] = version_id
                        self.client.delete_object(**delete_kwargs)
                        deleted_version = True

                if not response.get("IsTruncated"):
                    break
                key_marker = str(response.get("NextKeyMarker") or "")
                version_marker = str(response.get("NextVersionIdMarker") or "")
                if not key_marker and not version_marker:
                    break
        except Exception:
            deleted_version = False

        if deleted_version:
            return

        try:
            self.client.delete_object(Bucket=self.config.bucket_name, Key=file_name)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "")
            if code not in _NO_SUCH_KEY_CODES:
                raise

    def _read_asr_listing_metadata(self, file_name: str, video_id: str) -> dict[str, str]:
        """Recover title metadata from the uploaded .asr package itself."""
        try:
            asr_bytes = self._download_bytes(file_name)
        except Exception:
            return {"title": "", "channel": ""}

        try:
            with zipfile.ZipFile(io.BytesIO(asr_bytes), "r") as zf:
                manifest_entry = self._find_manifest_entry(zf, video_id)
                if manifest_entry:
                    return {
                        "title": str(manifest_entry.get("title") or "").strip(),
                        "channel": str(manifest_entry.get("channel") or "").strip(),
                    }

                project_entry = self._find_project_entry(zf, video_id)
                if project_entry:
                    return {
                        "title": str(project_entry.get("title") or "").strip(),
                        "channel": str(project_entry.get("channel") or "").strip(),
                    }
        except Exception:
            return {"title": "", "channel": ""}

        return {"title": "", "channel": ""}

    @staticmethod
    def _find_manifest_entry(zf: zipfile.ZipFile, video_id: str) -> dict[str, Any] | None:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except Exception:
            return None

        for key in ("projects", "titles"):
            entries = manifest.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                entry_id = entry.get("video_id") or entry.get("id")
                if entry_id == video_id:
                    return entry
            if len(entries) == 1 and isinstance(entries[0], dict):
                return entries[0]
        return None

    @staticmethod
    def _find_project_entry(zf: zipfile.ZipFile, video_id: str) -> dict[str, Any] | None:
        project_names = [name for name in zf.namelist() if name.endswith("/project.json")]
        fallback_entry: dict[str, Any] | None = None

        for name in project_names:
            try:
                entry = json.loads(zf.read(name).decode("utf-8"))
            except Exception:
                continue
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("video_id") or entry.get("id")
            if entry_id == video_id:
                return entry
            if fallback_entry is None:
                fallback_entry = entry

        return fallback_entry

    # ------------------------------------------------------------------ public API

    def list_titles(self) -> list[dict[str, Any]]:
        """Return metadata dicts for every .asr title on the bucket."""
        results: list[dict[str, Any]] = []
        for item in self._iter_objects(self.config.titles_prefix):
            file_name = str(item.get("Key") or "")
            if not file_name.endswith(".asr"):
                continue
            video_id = file_name[len(self.config.titles_prefix):-4]
            metadata: dict[str, str] = {}
            try:
                head = self._head_object(file_name)
                metadata = {
                    str(k).lower(): str(v)
                    for k, v in (head.get("Metadata") or {}).items()
                }
            except Exception:
                metadata = {}

            title = str(metadata.get("title") or "").strip()
            channel = str(metadata.get("channel") or "").strip()
            if not title:
                fallback = self._read_asr_listing_metadata(file_name, video_id)
                title = title or fallback.get("title", "")
                channel = channel or fallback.get("channel", "")

            uploaded_at = 0.0
            last_modified = item.get("LastModified")
            if hasattr(last_modified, "timestamp"):
                uploaded_at = float(last_modified.timestamp())

            results.append(
                {
                    "video_id": video_id,
                    "file_name": file_name,
                    "size": int(item.get("Size") or 0),
                    "uploaded_at": uploaded_at,
                    "title": title,
                    "channel": channel,
                }
            )
        return results

    def list_locks(self) -> dict[str, dict[str, Any]]:
        """Return {video_id: lock_info} for every active lock."""
        locks: dict[str, dict[str, Any]] = {}
        try:
            for item in self._iter_objects(self.config.locks_prefix):
                file_name = str(item.get("Key") or "")
                if not file_name.endswith(".lock"):
                    continue
                video_id = file_name[len(self.config.locks_prefix):-5]
                try:
                    locks[video_id] = json.loads(self._download_bytes(file_name))
                except Exception:
                    pass
        except Exception:
            pass
        return locks

    def create_lock(self, video_id: str) -> None:
        lock = {
            "user": actor_name_for_config(self.config),
            "user_id": self.config.user_id.strip(),
            "role": self.config.normalized_role(),
            "checkout_token": uuid.uuid4().hex,
            "hostname": socket.gethostname(),
            "locked_at": time.time(),
            "locked_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._upload_bytes(
            json.dumps(lock).encode("utf-8"),
            f"{self.config.locks_prefix}{video_id}.lock",
            content_type="application/json",
        )

    def release_lock(self, video_id: str) -> None:
        self._delete_file(f"{self.config.locks_prefix}{video_id}.lock")

    def get_lock(self, video_id: str) -> dict[str, Any] | None:
        try:
            data = self._download_bytes(f"{self.config.locks_prefix}{video_id}.lock")
            return json.loads(data)
        except Exception:
            return None

    def upload_asr(
        self,
        video_id: str,
        asr_bytes: bytes,
        title: str = "",
        channel: str = "",
    ) -> None:
        info: dict[str, str] = {}
        if title:
            info["title"] = title
        if channel:
            info["channel"] = channel
        self._upload_bytes(
            asr_bytes,
            f"{self.config.titles_prefix}{video_id}.asr",
            file_info=info or None,
        )

    def download_asr(self, video_id: str) -> bytes:
        return self._download_bytes(f"{self.config.titles_prefix}{video_id}.asr")

    def delete_asr(self, video_id: str) -> None:
        self._delete_file(f"{self.config.titles_prefix}{video_id}.asr")
        self.release_lock(video_id)

    def write_audit(
        self,
        event_type: str,
        video_id: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "event_type": event_type,
            "video_id": video_id,
            "actor_user_id": self.config.user_id.strip(),
            "actor_name": actor_name_for_config(self.config),
            "actor_role": self.config.normalized_role(),
            "timestamp": time.time(),
            "timestamp_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "hostname": socket.gethostname(),
        }
        if details:
            payload["details"] = details
        audit_name = (
            f"{self.config.audit_prefix}"
            f"{int(payload['timestamp'] * 1000)}_{video_id}_{event_type}_{uuid.uuid4().hex[:8]}.json"
        )
        self._upload_bytes(
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            audit_name,
            content_type="application/json",
        )

    def write_audit_safe(
        self,
        event_type: str,
        video_id: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.write_audit(event_type, video_id, details=details)
        except Exception:
            logger.exception("Failed to write audit entry for %s (%s)", video_id, event_type)


# ---------------------------------------------------------------------------
# Password dialog helper
# ---------------------------------------------------------------------------

class _PasswordDialog(tk.Toplevel):
    """Simple modal dialog that asks for a password (input hidden)."""

    def __init__(self, parent: tk.Misc, prompt: str) -> None:
        super().__init__(parent)
        self.title("Password")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: str | None = None

        ttk.Label(self, text=prompt, padding=(12, 12, 12, 6)).pack()
        self._var = tk.StringVar()
        entry = ttk.Entry(self, textvariable=self._var, show="*", width=30)
        entry.pack(padx=12, pady=(0, 8))
        entry.bind("<Return>", lambda _e: self._ok())
        entry.focus_set()

        btns = ttk.Frame(self, padding=(12, 0, 12, 12))
        btns.pack()
        ttk.Button(btns, text="OK",     command=self._ok,     width=10).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=self._cancel, width=10).pack(side="left")

        self.update_idletasks()
        px = parent.winfo_rootx() + parent.winfo_width()  // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry(f"+{px - w // 2}+{py - h // 2}")
        self.wait_window()

    def _ok(self) -> None:
        self.result = self._var.get()
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


def _ask_password(parent: tk.Misc, prompt: str) -> str | None:
    dlg = _PasswordDialog(parent, prompt)
    return dlg.result


class _ExportConfigDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, *, allow_admin_export: bool) -> None:
        super().__init__(parent)
        self.title("Export Config")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: dict[str, str] | None = None

        self._allow_admin_export = allow_admin_export
        self._export_role_var = tk.StringVar(value="user")
        self._password_var = tk.StringVar()
        self._confirm_var = tk.StringVar()

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(
            body,
            text="Choose what kind of config file to export.",
            wraplength=360,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        if allow_admin_export:
            ttk.Label(body, text="Export as:").grid(row=1, column=0, sticky="w", padx=(0, 8))
            role_row = ttk.Frame(body)
            role_row.grid(row=1, column=1, sticky="w")
            ttk.Radiobutton(
                role_row,
                text="User (Recommended)",
                variable=self._export_role_var,
                value="user",
            ).pack(side="left", padx=(0, 10))
            ttk.Radiobutton(
                role_row,
                text="Admin",
                variable=self._export_role_var,
                value="admin",
            ).pack(side="left")
        else:
            ttk.Label(body, text="Export as:").grid(row=1, column=0, sticky="w", padx=(0, 8))
            ttk.Label(body, text="User").grid(row=1, column=1, sticky="w")

        ttk.Label(body, text="Password:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 4))
        password_entry = ttk.Entry(body, textvariable=self._password_var, show="*", width=32)
        password_entry.grid(row=2, column=1, sticky="ew", pady=(8, 4))

        ttk.Label(body, text="Confirm:").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        confirm_entry = ttk.Entry(body, textvariable=self._confirm_var, show="*", width=32)
        confirm_entry.grid(row=3, column=1, sticky="ew", pady=(0, 4))

        btns = ttk.Frame(body)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self._cancel, width=10).pack(side="right")
        ttk.Button(btns, text="OK", command=self._ok, width=10).pack(side="right", padx=(0, 6))

        confirm_entry.bind("<Return>", lambda _e: self._ok())
        password_entry.focus_set()

        self.update_idletasks()
        px = parent.winfo_rootx() + parent.winfo_width() // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry(f"+{px - w // 2}+{py - h // 2}")
        self.wait_window()

    def _ok(self) -> None:
        password = self._password_var.get()
        confirm = self._confirm_var.get()
        if not password:
            messagebox.showerror("Missing password", "Enter a password for the exported config.", parent=self)
            return
        if password != confirm:
            messagebox.showerror("Mismatch", "Passwords did not match.", parent=self)
            return
        self.result = {
            "password": password,
            "export_role": _normalized_shared_role(
                self._export_role_var.get() if self._allow_admin_export else "user"
            ),
        }
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class _ImportConfigDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        initial_name: str = "",
        initial_user_id: str = "",
    ) -> None:
        super().__init__(parent)
        self.title("Import Config")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: dict[str, str] | None = None

        self._password_var = tk.StringVar()
        self._name_var = tk.StringVar(value=initial_name)
        self._user_id_var = tk.StringVar(value=initial_user_id)

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(
            body,
            text="Enter the file password and the local identity that should use this config.",
            wraplength=380,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(body, text="File password:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        password_entry = ttk.Entry(body, textvariable=self._password_var, show="*", width=32)
        password_entry.grid(row=1, column=1, sticky="ew", pady=(0, 4))

        ttk.Label(body, text="Your name:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        name_entry = ttk.Entry(body, textvariable=self._name_var, width=32)
        name_entry.grid(row=2, column=1, sticky="ew", pady=(0, 4))

        ttk.Label(body, text="User ID:").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        user_id_entry = ttk.Entry(body, textvariable=self._user_id_var, width=32)
        user_id_entry.grid(row=3, column=1, sticky="ew", pady=(0, 4))

        btns = ttk.Frame(body)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self._cancel, width=10).pack(side="right")
        ttk.Button(btns, text="OK", command=self._ok, width=10).pack(side="right", padx=(0, 6))

        user_id_entry.bind("<Return>", lambda _e: self._ok())
        password_entry.focus_set()

        self.update_idletasks()
        px = parent.winfo_rootx() + parent.winfo_width() // 2
        py = parent.winfo_rooty() + parent.winfo_height() // 2
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry(f"+{px - w // 2}+{py - h // 2}")
        self.wait_window()

    def _ok(self) -> None:
        password = self._password_var.get()
        name = self._name_var.get().strip()
        user_id = self._user_id_var.get().strip()
        if not password:
            messagebox.showerror("Missing password", "Enter the password for this config file.", parent=self)
            return
        if not name:
            messagebox.showerror("Missing name", "Enter your name for cloud editing.", parent=self)
            return
        if not user_id:
            messagebox.showerror("Missing user ID", "Enter a stable User ID for cloud editing.", parent=self)
            return
        self.result = {
            "password": password,
            "display_name": name,
            "user_id": user_id,
        }
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


# ---------------------------------------------------------------------------
# CloudPanel — the main Toplevel window
# ---------------------------------------------------------------------------

class CloudPanel(tk.Toplevel):
    """
    Non-modal cloud management window.

    Parameters
    ----------
    workspace       :  Path to the local workspace folder.
    pack_fn         :  pack_asr(workspace, projects, dest_path) -> int
    unpack_fn       :  unpack_asr(asr_path, workspace, video_ids) -> list[str]
    list_contents_fn:  list_asr_contents(asr_path) -> list[dict]
    get_project_fn  :  (video_id: str) -> VideoProject
    discover_fn     :  () -> list[dict]   (local workspace video summaries)
    prepare_local_fn:  () -> None         flushes local edits before cloud upload/sync
    on_cloud_change :  () -> None         called after cloud state changes locally
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        workspace: Path,
        pack_fn: Callable,
        unpack_fn: Callable,
        list_contents_fn: Callable,
        get_project_fn: Callable,
        discover_fn: Callable,
        prepare_local_fn: Callable,
        on_cloud_change: Callable,
    ) -> None:
        super().__init__(parent)
        self.title("Cloud Storage (S3 Compatible)")
        self.minsize(760, 620)
        self.resizable(True, True)

        self.workspace        = workspace
        self._pack_fn         = pack_fn
        self._unpack_fn       = unpack_fn
        self._list_contents   = list_contents_fn
        self._get_project     = get_project_fn
        self._discover        = discover_fn
        self._prepare_local   = prepare_local_fn
        self._on_cloud_change = on_cloud_change

        self._config          = load_b2_config(workspace)
        self._allow_admin_role = "1" if self._config.allows_admin_role() else "0"
        self._store:  B2CloudStore | None = None
        self._worker: threading.Thread | None = None
        self._queue:  queue.Queue[tuple[str, Any]] = queue.Queue()

        self._build_ui()
        self._load_fields()
        self.after(150, self._poll_queue)

        # Centre over parent
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width()  // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        w, h = 820, 660
        self.geometry(f"{w}x{h}+{pw - w // 2}+{ph - h // 2}")
        self.after(250, self._auto_refresh_on_open)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ---- Account settings section ----
        settings_lf = ttk.LabelFrame(self, text="Account Settings", padding=10)
        settings_lf.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        settings_lf.columnconfigure(1, weight=1)

        self._fields: dict[str, ttk.Entry] = {}
        self._shared_settings_widgets: list[tk.Widget] = []

        self._provider_label = ttk.Label(settings_lf, text="Provider:", anchor="e")
        self._provider_label.grid(row=0, column=0, sticky="e", padx=(0, 8), pady=2)
        self._provider_var = tk.StringVar(value=provider_label_for_key("backblaze_b2"))
        self._provider_combo = ttk.Combobox(
            settings_lf,
            textvariable=self._provider_var,
            values=[preset["label"] for preset in PROVIDER_PRESETS.values()],
            state="readonly",
            width=28,
        )
        self._provider_combo.grid(row=0, column=1, sticky="w", pady=2)
        self._provider_combo.bind("<<ComboboxSelected>>", self._on_provider_selected)
        self._shared_settings_widgets.extend([self._provider_label, self._provider_combo])

        self._key_id_label = ttk.Label(settings_lf, text="Access Key ID:", anchor="e")
        self._key_id_label.grid(row=1, column=0, sticky="e", padx=(0, 8), pady=2)
        self._key_id_var = tk.StringVar()
        self._fields["key_id"] = ttk.Entry(settings_lf, textvariable=self._key_id_var, width=42)
        self._fields["key_id"].grid(row=1, column=1, sticky="ew", pady=2)
        self._shared_settings_widgets.extend([self._key_id_label, self._fields["key_id"]])

        self._application_key_label = ttk.Label(settings_lf, text="Secret Access Key:", anchor="e")
        self._application_key_label.grid(row=2, column=0, sticky="e", padx=(0, 8), pady=2)
        self._application_key_var = tk.StringVar()
        self._fields["application_key"] = ttk.Entry(
            settings_lf,
            textvariable=self._application_key_var,
            width=42,
            show="*",
        )
        self._fields["application_key"].grid(row=2, column=1, sticky="ew", pady=2)

        self._show_key_var = tk.BooleanVar(value=False)
        self._show_key_btn = ttk.Checkbutton(
            settings_lf,
            text="Show key",
            variable=self._show_key_var,
            command=self._toggle_key_visibility,
        )
        self._show_key_btn.grid(row=2, column=2, padx=(6, 0))
        self._shared_settings_widgets.extend(
            [self._application_key_label, self._fields["application_key"], self._show_key_btn]
        )

        self._bucket_label = ttk.Label(settings_lf, text="Bucket:", anchor="e")
        self._bucket_label.grid(row=3, column=0, sticky="e", padx=(0, 8), pady=2)
        self._bucket_var = tk.StringVar()
        self._fields["bucket"] = ttk.Entry(settings_lf, textvariable=self._bucket_var, width=42)
        self._fields["bucket"].grid(row=3, column=1, sticky="ew", pady=2)
        self._shared_settings_widgets.extend([self._bucket_label, self._fields["bucket"]])

        self._region_label = ttk.Label(settings_lf, text="Region:", anchor="e")
        self._region_label.grid(row=4, column=0, sticky="e", padx=(0, 8), pady=2)
        self._region_name_var = tk.StringVar()
        self._fields["region_name"] = ttk.Entry(settings_lf, textvariable=self._region_name_var, width=42)
        self._fields["region_name"].grid(row=4, column=1, sticky="ew", pady=2)
        self._shared_settings_widgets.extend([self._region_label, self._fields["region_name"]])

        self._endpoint_label = ttk.Label(settings_lf, text="Endpoint URL:", anchor="e")
        self._endpoint_label.grid(row=5, column=0, sticky="e", padx=(0, 8), pady=2)
        self._endpoint_url_var = tk.StringVar()
        self._fields["endpoint_url"] = ttk.Entry(settings_lf, textvariable=self._endpoint_url_var, width=42)
        self._fields["endpoint_url"].grid(row=5, column=1, sticky="ew", pady=2)
        self._shared_settings_widgets.extend([self._endpoint_label, self._fields["endpoint_url"]])

        self._addressing_label = ttk.Label(settings_lf, text="Addressing:", anchor="e")
        self._addressing_label.grid(row=6, column=0, sticky="e", padx=(0, 8), pady=2)
        self._addressing_style_var = tk.StringVar(value="auto")
        self._addressing_combo = ttk.Combobox(
            settings_lf,
            textvariable=self._addressing_style_var,
            values=ADDRESSING_STYLE_CHOICES,
            state="readonly",
            width=12,
        )
        self._addressing_combo.grid(row=6, column=1, sticky="w", pady=2)
        self._shared_settings_widgets.extend([self._addressing_label, self._addressing_combo])

        self._provider_hint_var = tk.StringVar(value="")
        self._provider_hint_label = ttk.Label(
            settings_lf,
            textvariable=self._provider_hint_var,
            foreground="#6b7280",
            wraplength=300,
            justify="left",
        )
        self._provider_hint_label.grid(row=6, column=2, rowspan=2, padx=(6, 0), sticky="w")
        self._shared_settings_widgets.append(self._provider_hint_label)

        self._folder_prefix_label = ttk.Label(settings_lf, text="Folder prefix:", anchor="e")
        self._folder_prefix_label.grid(row=7, column=0, sticky="e", padx=(0, 8), pady=2)
        self._folder_prefix_var = tk.StringVar()
        self._fields["folder_prefix"] = ttk.Entry(settings_lf, textvariable=self._folder_prefix_var, width=42)
        self._fields["folder_prefix"].grid(row=7, column=1, sticky="ew", pady=2)
        self._shared_settings_widgets.extend([self._folder_prefix_label, self._fields["folder_prefix"]])

        ttk.Label(settings_lf, text="Your name:", anchor="e").grid(
            row=8, column=0, sticky="e", padx=(0, 8), pady=2
        )
        self._your_name_var = tk.StringVar()
        self._fields["your_name"] = ttk.Entry(settings_lf, textvariable=self._your_name_var, width=42)
        self._fields["your_name"].grid(row=8, column=1, sticky="ew", pady=2)

        ttk.Label(settings_lf, text="User ID:", anchor="e").grid(
            row=9, column=0, sticky="e", padx=(0, 8), pady=2
        )
        self._user_id_var = tk.StringVar()
        self._fields["user_id"] = ttk.Entry(settings_lf, textvariable=self._user_id_var, width=42)
        self._fields["user_id"].grid(row=9, column=1, sticky="ew", pady=2)

        self._role_label = ttk.Label(settings_lf, text="Role:", anchor="e")
        self._role_label.grid(row=10, column=0, sticky="e", padx=(0, 8), pady=2)
        self._role_var = tk.StringVar(value="user")
        self._role_combo = ttk.Combobox(
            settings_lf,
            textvariable=self._role_var,
            values=["user", "admin"],
            state="readonly",
            width=12,
        )
        self._role_combo.grid(row=10, column=1, sticky="w", pady=2)
        self._role_hint_var = tk.StringVar(value="Trusted team only")
        self._role_hint_label = ttk.Label(settings_lf, textvariable=self._role_hint_var, foreground="#6b7280")
        self._role_hint_label.grid(row=10, column=2, padx=(6, 0), sticky="w")
        self._shared_settings_widgets.extend([self._role_label, self._role_combo, self._role_hint_label])

        self._managed_config_note_var = tk.StringVar(value="")
        self._managed_config_note = ttk.Label(
            settings_lf,
            textvariable=self._managed_config_note_var,
            foreground="#6b7280",
            wraplength=520,
            justify="left",
        )
        self._managed_config_note.grid(row=11, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self._managed_config_note.grid_remove()

        # button row
        btn_row = ttk.Frame(settings_lf)
        btn_row.grid(row=12, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(btn_row, text="Test Connection", command=self._test_connection).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Save Settings",   command=self._save_settings).pack(side="left", padx=(0, 16))
        ttk.Separator(btn_row, orient="vertical").pack(side="left", fill="y", padx=(0, 12))
        ttk.Button(btn_row, text="Export Config…",  command=self._export_config).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="Import Config…",  command=self._import_config).pack(side="left")

        # ---- Cloud titles section ----
        titles_lf = ttk.LabelFrame(self, text="Cloud Titles", padding=10)
        titles_lf.grid(row=1, column=0, sticky="nsew", padx=10, pady=(4, 4))
        titles_lf.columnconfigure(0, weight=1)
        titles_lf.rowconfigure(0, weight=1)

        # treeview
        tree_frame = ttk.Frame(titles_lf)
        tree_frame.grid(row=0, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        self._tree = ttk.Treeview(
            tree_frame,
            columns=("size", "uploaded", "status"),
            show="tree headings",
            selectmode="extended",
        )
        self._tree.heading("#0",       text="Title / Video ID")
        self._tree.heading("size",     text="Size")
        self._tree.heading("uploaded", text="Uploaded")
        self._tree.heading("status",   text="Status")
        self._tree.column("#0",       width=220, stretch=True)
        self._tree.column("size",     width=80,  stretch=False, anchor="e")
        self._tree.column("uploaded", width=140, stretch=False)
        self._tree.column("status",   width=180, stretch=True)
        self._tree.tag_configure("available",   foreground="#111827")
        self._tree.tag_configure("locked_self", foreground="#1d4ed8", background="#eff6ff")
        self._tree.tag_configure("locked_other",foreground="#b45309", background="#fffbeb")
        self._tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self._tree.xview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        # action buttons
        action_row = ttk.Frame(titles_lf)
        action_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(action_row, text="↻ Refresh",            command=self._refresh_titles).pack(side="left", padx=(0, 6))
        ttk.Separator(action_row, orient="vertical").pack(side="left", fill="y", padx=(0, 8))
        ttk.Button(action_row, text="Check Out",            command=self._checkout).pack(side="left", padx=(0, 6))
        ttk.Button(action_row, text="Check In",             command=self._checkin).pack(side="left", padx=(0, 6))
        ttk.Button(action_row, text="Sync Checked Out",     command=self._sync_checked_out).pack(side="left", padx=(0, 6))
        ttk.Separator(action_row, orient="vertical").pack(side="left", fill="y", padx=(0, 8))
        ttk.Button(action_row, text="Upload from Workspace",command=self._upload_new).pack(side="left", padx=(0, 6))
        ttk.Button(action_row, text="Delete from Cloud",    command=self._delete_cloud).pack(side="left")
        ttk.Separator(action_row, orient="vertical").pack(side="left", fill="y", padx=(8, 8))
        self._admin_force_checkin_btn = ttk.Button(
            action_row,
            text="Admin Force Check In",
            command=self._admin_force_checkin,
        )
        self._admin_force_checkin_btn.pack(side="left", padx=(0, 6))
        self._admin_take_over_btn = ttk.Button(
            action_row,
            text="Admin Take Over",
            command=self._admin_take_over,
        )
        self._admin_take_over_btn.pack(side="left", padx=(0, 6))

        # status bar
        self._status_var = tk.StringVar(value="Enter your cloud credentials above and click Save Settings.")
        ttk.Label(
            self,
            textvariable=self._status_var,
            anchor="w",
            relief="flat",
            background="#f1f5f9",
            foreground="#374151",
            padding=(10, 4),
        ).grid(row=2, column=0, sticky="ew")

    def _auto_refresh_on_open(self) -> None:
        if not self.winfo_exists():
            return
        if self._config.is_valid():
            self._refresh_titles()

    # ------------------------------------------------------------------ field helpers

    def _apply_provider_hint(self, provider_key: str) -> None:
        preset = PROVIDER_PRESETS.get(provider_key, PROVIDER_PRESETS["backblaze_b2"])
        self._provider_hint_var.set(preset.get("endpoint_hint", ""))

    def _apply_provider_preset(self, provider_key: str, *, force: bool = False) -> None:
        preset = PROVIDER_PRESETS.get(provider_key, PROVIDER_PRESETS["backblaze_b2"])
        self._apply_provider_hint(provider_key)

        current_endpoint = self._endpoint_url_var.get().strip()
        current_region = self._region_name_var.get().strip()

        if force or not current_endpoint:
            self._endpoint_url_var.set(preset.get("endpoint_url", ""))
        if force or not current_region:
            self._region_name_var.set(preset.get("region_name", ""))
        self._addressing_style_var.set(preset.get("addressing_style", "auto"))

    def _on_provider_selected(self, _event: Any = None) -> None:
        self._apply_provider_preset(provider_key_for_label(self._provider_var.get()))

    def _load_fields(self) -> None:
        c = self._config
        self._allow_admin_role = "1" if c.allows_admin_role() else "0"
        self._provider_var.set(provider_label_for_key(c.normalized_provider()))
        self._key_id_var.set(c.key_id)
        self._application_key_var.set(c.application_key)
        self._bucket_var.set(c.bucket_name)
        self._region_name_var.set(c.region_name)
        self._endpoint_url_var.set(c.endpoint_url)
        self._addressing_style_var.set(c.normalized_addressing_style())
        self._folder_prefix_var.set(c.folder_prefix)
        self._your_name_var.set(c.display_name)
        self._user_id_var.set(c.user_id)
        self._role_var.set(c.normalized_role())
        self._apply_provider_hint(c.normalized_provider())
        self._apply_shared_settings_visibility()
        self._apply_role_permissions()
        self._update_admin_controls()
        if c.uses_managed_user_config():
            self._set_status("Managed user config loaded. Shared cloud settings are hidden on this machine.")

    def _read_fields(self) -> B2Config:
        config = B2Config(
            provider        = provider_key_for_label(self._provider_var.get()),
            key_id          = self._key_id_var.get().strip(),
            application_key = self._application_key_var.get().strip(),
            bucket_name     = self._bucket_var.get().strip(),
            endpoint_url    = self._endpoint_url_var.get().strip(),
            region_name     = self._region_name_var.get().strip(),
            addressing_style = self._addressing_style_var.get().strip() or "auto",
            folder_prefix   = self._folder_prefix_var.get().strip(),
            display_name    = self._your_name_var.get().strip(),
            user_id         = self._user_id_var.get().strip(),
            role            = self._role_var.get().strip() or "user",
            allow_admin_role = self._allow_admin_role,
        )
        config.role = config.normalized_role()
        return config

    def _toggle_key_visibility(self) -> None:
        show = "" if self._show_key_var.get() else "*"
        self._fields["application_key"].config(show=show)

    def _set_status(self, msg: str) -> None:
        self._status_var.set(msg)

    def _apply_shared_settings_visibility(self) -> None:
        managed = self._config.uses_managed_user_config()
        for widget in self._shared_settings_widgets:
            if managed:
                widget.grid_remove()
            else:
                widget.grid()
        if managed:
            self._managed_config_note_var.set(
                "This imported user config is managed by an admin. Shared cloud connection settings are hidden on this machine."
            )
            self._managed_config_note.grid()
        else:
            self._managed_config_note_var.set("")
            self._managed_config_note.grid_remove()

    def _apply_role_permissions(self) -> None:
        if self._allow_admin_role.strip().lower() in {"", "0", "false", "no", "off"}:
            self._role_var.set("user")
            self._role_combo.configure(state="disabled")
            self._role_hint_var.set("Imported user config: admin role locked")
            return
        self._role_combo.configure(state="readonly")
        self._role_hint_var.set("Trusted team only")

    def _update_admin_controls(self) -> None:
        state = ["!disabled"] if config_is_admin(self._read_fields()) else ["disabled"]
        self._admin_force_checkin_btn.state(state)
        self._admin_take_over_btn.state(state)

    def _require_identity(self) -> bool:
        config = self._read_fields()
        if config.has_identity():
            return True
        messagebox.showwarning(
            "Missing identity",
            "Set a stable User ID in Cloud Settings before using cloud editing actions.",
            parent=self,
        )
        return False

    def _require_admin(self) -> bool:
        if not self._require_identity():
            return False
        config = self._read_fields()
        if config_is_admin(config):
            return True
        messagebox.showwarning(
            "Admin only",
            "This action is only available to admins.",
            parent=self,
        )
        return False

    def _notify_cloud_change(self) -> None:
        if self._on_cloud_change is not None:
            self._on_cloud_change()

    def _record_uploaded_state(
        self,
        video_id: str,
        project: Any,
        *,
        checked_in: bool,
    ) -> None:
        update_cloud_state_entry(
            self.workspace,
            video_id,
            cloud_state="checked_in" if checked_in else "checked_out_self",
            lock_user="" if checked_in else actor_name_for_config(self._config),
            lock_user_id="" if checked_in else self._config.user_id.strip(),
            lock_role="" if checked_in else self._config.normalized_role(),
            title=str(getattr(project, "title", "") or ""),
            channel=str(getattr(project, "channel", "") or ""),
            last_synced_updated_at=float(getattr(project, "updated_at", 0.0) or 0.0),
            uploaded_at=time.time(),
        )

    # ------------------------------------------------------------------ settings

    def _save_settings(self) -> None:
        self._config = self._read_fields()
        save_b2_config(self.workspace, self._config)
        self._store = None          # force reconnect on next action
        self._apply_shared_settings_visibility()
        self._update_admin_controls()
        self._set_status("Settings saved.")

    def _export_config(self) -> None:
        if not _HAS_CRYPTO:
            messagebox.showerror(
                "Missing dependency",
                "Install the 'cryptography' package to use this feature:\n"
                "  pip install \".[cloud]\"",
                parent=self,
            )
            return
        config = self._read_fields()
        if not config.is_valid():
            messagebox.showwarning(
                "Incomplete",
                "Fill in the cloud access key, secret key, bucket, and any required endpoint or region before exporting.",
                parent=self,
            )
            return
        export_options = _ExportConfigDialog(
            self,
            allow_admin_export=config_is_admin(config),
        ).result
        if not export_options:
            return
        dest = filedialog.asksaveasfilename(
            parent=self,
            title="Export cloud config",
            defaultextension=".b2cfg",
            filetypes=[("Cloud config", "*.b2cfg"), ("All files", "*.*")],
        )
        if not dest:
            return
        try:
            export_b2_config(
                config,
                Path(dest),
                export_options["password"],
                export_role=export_options.get("export_role") or "user",
            )
            self._set_status(f"Config exported to {dest}")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)

    def _import_config(self) -> None:
        if not _HAS_CRYPTO:
            messagebox.showerror(
                "Missing dependency",
                "Install the 'cryptography' package:\n  pip install \".[cloud]\"",
                parent=self,
            )
            return
        src = filedialog.askopenfilename(
            parent=self,
            title="Import cloud config",
            filetypes=[("Cloud config", "*.b2cfg"), ("All files", "*.*")],
        )
        if not src:
            return
        import_options = _ImportConfigDialog(
            self,
            initial_name=self._your_name_var.get().strip(),
            initial_user_id=self._user_id_var.get().strip(),
        ).result
        if not import_options:
            return
        try:
            self._config = import_b2_config(
                Path(src),
                import_options["password"],
                display_name=import_options["display_name"],
                user_id=import_options["user_id"],
            )
            self._load_fields()
            self._save_settings()
            self._set_status("Config imported and saved.")
        except ValueError as exc:
            messagebox.showerror("Import failed", str(exc), parent=self)
        except Exception as exc:
            messagebox.showerror("Import failed", f"Could not read config:\n{exc}", parent=self)

    # ------------------------------------------------------------------ cloud connection

    def _get_store(self) -> B2CloudStore | None:
        if self._store is not None:
            return self._store
        config = self._config
        if not config.is_valid():
            messagebox.showwarning(
                "Not connected",
                "Enter your cloud credentials and click Save Settings first.",
                parent=self,
            )
            return None
        try:
            self._store = B2CloudStore(config)
            return self._store
        except Exception as exc:
            messagebox.showerror("Connection failed", str(exc), parent=self)
            return None

    def _test_connection(self) -> None:
        self._save_settings()
        self._set_status("Testing connection…")

        def worker() -> dict[str, Any]:
            store = B2CloudStore(self._read_fields())
            titles = store.list_titles()
            return {"count": len(titles)}

        def on_done(result: dict[str, Any]) -> None:
            self._set_status(
                f"Connected successfully. {result['count']} title(s) found on bucket."
            )

        self._run_worker("Testing…", worker, on_done)

    # ------------------------------------------------------------------ titles list

    def _refresh_titles(self) -> None:
        store = self._get_store()
        if not store:
            return
        self._set_status("Refreshing…")

        def worker() -> dict[str, Any]:
            titles = store.list_titles()
            locks  = store.list_locks()
            return {"titles": titles, "locks": locks}

        def on_done(result: dict[str, Any]) -> None:
            refresh_cloud_state_from_listing(self.workspace, result["titles"], result["locks"], self._config)
            self._populate_tree(result["titles"], result["locks"])
            self._notify_cloud_change()
            self._set_status(f"Found {len(result['titles'])} title(s) on cloud.")

        self._run_worker("Refreshing titles…", worker, on_done)

    def _populate_tree(self, titles: list[dict], locks: dict[str, dict]) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)

        for t in sorted(titles, key=lambda x: (x.get("title") or x["video_id"]).lower()):
            vid      = t["video_id"]
            title    = t.get("title", "")
            channel  = t.get("channel", "")
            size_mb  = t["size"] / (1024 * 1024)
            uploaded = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["uploaded_at"]))

            # Build display label: prefer title, fall back to video_id
            if title:
                display = title
                if channel:
                    display = f"{title}  [{channel}]"
            else:
                display = vid

            if vid in locks:
                lock     = locks[vid]
                locker   = lock_owner_label(lock)
                locked_s = lock.get("locked_at", 0)
                age      = _format_age(time.time() - locked_s)
                if lock_belongs_to(lock, self._config):
                    status = f"🔒 Checked out by you  ({age})"
                    tag    = "locked_self"
                else:
                    status = f"🔒 {locker}  ({age})"
                    tag    = "locked_other"
            else:
                status = "✓  Checked in"
                tag    = "available"

            self._tree.insert(
                "", "end", iid=vid,
                text=display,
                values=(f"{size_mb:.1f} MB", uploaded, status),
                tags=(tag,),
            )

    def _selected_video_ids(self) -> list[str]:
        return list(self._tree.selection())

    # ------------------------------------------------------------------ check out

    def _checkout(self) -> None:
        ids = self._selected_video_ids()
        if not ids:
            messagebox.showinfo("Nothing selected", "Select one or more titles to check out.", parent=self)
            return
        if not self._require_identity():
            return
        store = self._get_store()
        if not store:
            return

        # Check for locks held by others
        blocked: list[tuple[str, str]] = []
        allowed_ids: list[str] = []
        for vid in ids:
            lock = store.get_lock(vid)
            if lock and not lock_belongs_to(lock, self._config):
                blocked.append((vid, lock_owner_label(lock)))
            else:
                allowed_ids.append(vid)

        if blocked:
            blocked_lines = [f"  - {vid}  (locked by {owner})" for vid, owner in blocked]
            msg = "The following titles are checked out by someone else:\n\n" + "\n".join(blocked_lines)
            msg += "\n\nUse Admin Take Over if you need to override those locks."
            if not allowed_ids:
                messagebox.showwarning("Titles locked", msg, parent=self)
                return
            msg += f"\n\nContinue checking out the remaining {len(allowed_ids)} available title(s)?"
            if not messagebox.askyesno("Some titles unavailable", msg, parent=self):
                return
            ids = allowed_ids

        self._set_status(f"Checking out {len(ids)} title(s)…")

        def worker() -> dict[str, Any]:
            imported: list[str] = []
            for vid in ids:
                previous_lock = store.get_lock(vid)
                self._queue.put(("status", f"Downloading {vid}…"))
                asr_bytes = store.download_asr(vid)

                # Write temp .asr file then unpack
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".asr", delete=False) as tf:
                    tf.write(asr_bytes)
                    tmp_path = Path(tf.name)
                try:
                    result = self._unpack_fn(tmp_path, self.workspace, [vid])
                    imported.extend(result)
                finally:
                    tmp_path.unlink(missing_ok=True)

                self._queue.put(("status", f"Locking {vid}…"))
                store.create_lock(vid)
                store.write_audit_safe(
                    "check_out",
                    vid,
                    details={"previous_lock": previous_lock},
                )

            return {"imported": imported}

        def on_done(result: dict[str, Any]) -> None:
            for vid in result["imported"]:
                try:
                    project = self._get_project(vid)
                except Exception:
                    project = None
                update_cloud_state_entry(
                    self.workspace,
                    vid,
                    cloud_state="checked_out_self",
                    lock_user=actor_name_for_config(self._config),
                    lock_user_id=self._config.user_id.strip(),
                    lock_role=self._config.normalized_role(),
                    title=str(getattr(project, "title", "") or ""),
                    channel=str(getattr(project, "channel", "") or ""),
                    last_synced_updated_at=float(getattr(project, "updated_at", 0.0) or 0.0),
                )
            self._notify_cloud_change()
            self._refresh_titles()
            self._set_status(f"Checked out {len(result['imported'])} title(s).")
            messagebox.showinfo(
                "Check Out Complete",
                f"Checked out {len(result['imported'])} title(s) into workspace.",
                parent=self,
            )

        self._run_worker("Checking out…", worker, on_done)

    # ------------------------------------------------------------------ check in

    def _checkin(self) -> None:
        ids = self._selected_video_ids()
        if not ids:
            messagebox.showinfo("Nothing selected", "Select one or more titles to check in.", parent=self)
            return
        if not self._require_identity():
            return
        self._prepare_local()
        store = self._get_store()
        if not store:
            return

        self._set_status(f"Checking in {len(ids)} title(s)…")

        def worker() -> dict[str, Any]:
            uploaded: list[dict[str, Any]] = []
            import tempfile
            for vid in ids:
                self._queue.put(("status", f"Packing {vid}…"))
                lock = store.get_lock(vid)
                if lock and not lock_belongs_to(lock, self._config):
                    self._queue.put(("status", f"Skipping {vid}: checked out by {lock_owner_label(lock)}"))
                    continue
                try:
                    project = self._get_project(vid)
                except Exception as exc:
                    self._queue.put(("status", f"Skipping {vid}: {exc}"))
                    continue

                with tempfile.NamedTemporaryFile(suffix=".asr", delete=False) as tf:
                    tmp_path = Path(tf.name)
                try:
                    count = self._pack_fn(self.workspace, [project], tmp_path)
                    if count == 0:
                        continue
                    self._queue.put(("status", f"Uploading {vid}…"))
                    asr_bytes = tmp_path.read_bytes()
                    store.upload_asr(
                        vid, asr_bytes,
                        title=getattr(project, "title", ""),
                        channel=getattr(project, "channel", ""),
                    )
                    if not lock or lock_belongs_to(lock, self._config):
                        store.release_lock(vid)
                    store.write_audit_safe(
                        "check_in",
                        vid,
                        details={"released_lock": lock},
                    )
                    uploaded.append({
                        "video_id": vid,
                        "title": getattr(project, "title", ""),
                        "channel": getattr(project, "channel", ""),
                        "updated_at": float(getattr(project, "updated_at", 0.0) or 0.0),
                    })
                finally:
                    tmp_path.unlink(missing_ok=True)

            return {"uploaded": uploaded}

        def on_done(result: dict[str, Any]) -> None:
            for item in result["uploaded"]:
                update_cloud_state_entry(
                    self.workspace,
                    item["video_id"],
                    cloud_state="checked_in",
                    lock_user="",
                    lock_user_id="",
                    lock_role="",
                    title=str(item.get("title") or ""),
                    channel=str(item.get("channel") or ""),
                    last_synced_updated_at=float(item.get("updated_at") or 0.0),
                    uploaded_at=time.time(),
                )
            self._notify_cloud_change()
            self._refresh_titles()
            self._set_status(f"Checked in {len(result['uploaded'])} title(s).")
            messagebox.showinfo(
                "Check In Complete",
                f"Checked in {len(result['uploaded'])} title(s) to cloud.",
                parent=self,
            )

        self._run_worker("Checking in…", worker, on_done)

    # ------------------------------------------------------------------ upload new

    def _upload_new(self) -> None:
        """Upload local-only titles that do not already exist on cloud."""
        if not self._require_identity():
            return
        self._prepare_local()
        local = self._discover()
        if not local:
            messagebox.showinfo("No local titles", "No titles found in the current workspace.", parent=self)
            return
        store = self._get_store()
        if not store:
            return
        uploadable = filter_uploadable_summaries(local, store.list_titles())
        if not uploadable:
            messagebox.showinfo(
                "Nothing to upload",
                (
                    "Upload from Workspace only lists titles that are local-only and not yet on cloud.\n\n"
                    "Use Check Out / Check In for titles that already exist on both the computer and server."
                ),
                parent=self,
            )
            return

        from yt_subtitle_extract.gui import TitleSelectDialog  # local import to avoid circular
        dlg = TitleSelectDialog(self, "Select local-only titles to upload", uploadable, default_all=False)
        selected_ids = dlg.result
        if not selected_ids:
            return

        upload_choice = messagebox.askyesnocancel(
            "Upload Options",
            "Check in these uploaded title(s) immediately?\n\n"
            "Yes: upload and check in\n"
            "No: upload and keep checked out by you",
            parent=self,
        )
        if upload_choice is None:
            return
        check_in_after_upload = bool(upload_choice)

        selected_set = set(selected_ids)
        self._set_status(f"Uploading {len(selected_ids)} title(s)…")

        def worker() -> dict[str, Any]:
            uploaded: list[dict[str, Any]] = []
            skipped_existing: list[str] = []
            skipped_locked: list[str] = []
            existing_ids = {
                str(item.get("video_id") or "").strip()
                for item in store.list_titles()
            }
            import tempfile
            for summary in uploadable:
                vid = summary["video_id"]
                if vid not in selected_set:
                    continue
                if vid in existing_ids:
                    skipped_existing.append(vid)
                    continue
                existing_lock = store.get_lock(vid)
                if existing_lock and not lock_belongs_to(existing_lock, self._config):
                    skipped_locked.append(f"{vid} ({lock_owner_label(existing_lock)})")
                    continue
                self._queue.put(("status", f"Packing {vid}…"))
                try:
                    project = self._get_project(vid)
                except Exception as exc:
                    self._queue.put(("status", f"Skipping {vid}: {exc}"))
                    continue

                with tempfile.NamedTemporaryFile(suffix=".asr", delete=False) as tf:
                    tmp_path = Path(tf.name)
                try:
                    count = self._pack_fn(self.workspace, [project], tmp_path)
                    if count == 0:
                        continue
                    self._queue.put(("status", f"Uploading {vid}…"))
                    store.upload_asr(
                        vid, tmp_path.read_bytes(),
                        title=getattr(project, "title", ""),
                        channel=getattr(project, "channel", ""),
                    )
                    lock = store.get_lock(vid)
                    if check_in_after_upload:
                        if not lock or lock_belongs_to(lock, self._config):
                            try:
                                store.release_lock(vid)
                            except Exception:
                                pass
                    else:
                        store.create_lock(vid)
                    store.write_audit_safe(
                        "upload",
                        vid,
                        details={"checked_in": check_in_after_upload},
                    )
                    uploaded.append({
                        "video_id": vid,
                        "title": getattr(project, "title", ""),
                        "channel": getattr(project, "channel", ""),
                        "updated_at": float(getattr(project, "updated_at", 0.0) or 0.0),
                    })
                finally:
                    tmp_path.unlink(missing_ok=True)

            return {
                "uploaded": uploaded,
                "skipped_existing": skipped_existing,
                "skipped_locked": skipped_locked,
            }

        def on_done(result: dict[str, Any]) -> None:
            for item in result["uploaded"]:
                update_cloud_state_entry(
                    self.workspace,
                    item["video_id"],
                    cloud_state="checked_in" if check_in_after_upload else "checked_out_self",
                    lock_user="" if check_in_after_upload else actor_name_for_config(self._config),
                    lock_user_id="" if check_in_after_upload else self._config.user_id.strip(),
                    lock_role="" if check_in_after_upload else self._config.normalized_role(),
                    title=str(item.get("title") or ""),
                    channel=str(item.get("channel") or ""),
                    last_synced_updated_at=float(item.get("updated_at") or 0.0),
                    uploaded_at=time.time(),
                )
            self._notify_cloud_change()
            self._refresh_titles()
            skipped_existing = result.get("skipped_existing") or []
            skipped_locked = result.get("skipped_locked") or []
            if check_in_after_upload:
                self._set_status(f"Uploaded and checked in {len(result['uploaded'])} title(s).")
            else:
                self._set_status(f"Uploaded {len(result['uploaded'])} title(s) and kept them checked out.")
            if skipped_existing or skipped_locked:
                lines: list[str] = []
                if skipped_existing:
                    lines.append(
                        "Already on cloud: " + ", ".join(sorted(skipped_existing))
                    )
                if skipped_locked:
                    lines.append(
                        "Locked by another user: " + ", ".join(sorted(skipped_locked))
                    )
                messagebox.showinfo(
                    "Upload completed with skips",
                    "\n\n".join(lines),
                    parent=self,
                )

        self._run_worker("Uploading…", worker, on_done)

    # ------------------------------------------------------------------ sync checked out

    def _sync_checked_out(self) -> None:
        if not self._require_identity():
            return
        self._prepare_local()
        store = self._get_store()
        if not store:
            return

        selected_ids = self._selected_video_ids()
        self._set_status("Syncing checked-out titles…")

        def worker() -> dict[str, Any]:
            locks = store.list_locks()
            state = load_cloud_state(self.workspace)
            target_ids = selected_ids or [
                vid
                for vid, lock in locks.items()
                if lock_belongs_to(lock, self._config)
            ]

            uploaded: list[dict[str, Any]] = []
            unchanged: list[str] = []
            skipped: list[str] = []
            import tempfile

            for vid in target_ids:
                lock = locks.get(vid)
                if not lock or not lock_belongs_to(lock, self._config):
                    skipped.append(vid)
                    continue

                try:
                    project = self._get_project(vid)
                except Exception as exc:
                    self._queue.put(("status", f"Skipping {vid}: {exc}"))
                    skipped.append(vid)
                    continue

                project_updated_at = float(getattr(project, "updated_at", 0.0) or 0.0)
                last_synced_updated_at = float(
                    state.get(vid, {}).get("last_synced_updated_at") or 0.0
                )
                if project_updated_at <= last_synced_updated_at + 1e-6:
                    unchanged.append(vid)
                    continue

                self._queue.put(("status", f"Syncing {vid}…"))
                with tempfile.NamedTemporaryFile(suffix=".asr", delete=False) as tf:
                    tmp_path = Path(tf.name)
                try:
                    count = self._pack_fn(self.workspace, [project], tmp_path)
                    if count == 0:
                        skipped.append(vid)
                        continue
                    store.upload_asr(
                        vid,
                        tmp_path.read_bytes(),
                        title=getattr(project, "title", ""),
                        channel=getattr(project, "channel", ""),
                    )
                    store.write_audit_safe(
                        "sync",
                        vid,
                        details={"updated_at": project_updated_at},
                    )
                    uploaded.append({
                        "video_id": vid,
                        "title": getattr(project, "title", ""),
                        "channel": getattr(project, "channel", ""),
                        "updated_at": project_updated_at,
                    })
                finally:
                    tmp_path.unlink(missing_ok=True)

            return {"uploaded": uploaded, "unchanged": unchanged, "skipped": skipped}

        def on_done(result: dict[str, Any]) -> None:
            for item in result["uploaded"]:
                update_cloud_state_entry(
                    self.workspace,
                    item["video_id"],
                    cloud_state="checked_out_self",
                    lock_user=actor_name_for_config(self._config),
                    lock_user_id=self._config.user_id.strip(),
                    lock_role=self._config.normalized_role(),
                    title=str(item.get("title") or ""),
                    channel=str(item.get("channel") or ""),
                    last_synced_updated_at=float(item.get("updated_at") or 0.0),
                    uploaded_at=time.time(),
                )
            self._notify_cloud_change()
            self._refresh_titles()
            if not result["uploaded"] and not result["unchanged"]:
                self._set_status("No titles checked out by you needed syncing.")
            else:
                self._set_status(
                    f"Synced {len(result['uploaded'])} title(s), "
                    f"{len(result['unchanged'])} unchanged."
                )

    # ------------------------------------------------------------------ admin actions

    def _admin_force_checkin(self) -> None:
        ids = self._selected_video_ids()
        if not ids:
            messagebox.showinfo("Nothing selected", "Select one or more titles first.", parent=self)
            return
        if not self._require_admin():
            return
        if not messagebox.askyesno(
            "Admin Force Check In",
            f"Release the active lock for {len(ids)} title(s) and make the current cloud copy available?\n\n"
            "This does not recover unsynced local edits from the previous user.",
            parent=self,
        ):
            return
        store = self._get_store()
        if not store:
            return

        def worker() -> dict[str, Any]:
            released: list[str] = []
            skipped: list[str] = []
            for vid in ids:
                lock = store.get_lock(vid)
                if not lock:
                    skipped.append(vid)
                    continue
                self._queue.put(("status", f"Force checking in {vid}…"))
                store.release_lock(vid)
                store.write_audit_safe(
                    "admin_force_checkin",
                    vid,
                    details={"previous_lock": lock},
                )
                released.append(vid)
            return {"released": released, "skipped": skipped}

        def on_done(result: dict[str, Any]) -> None:
            for vid in result["released"]:
                update_cloud_state_entry(
                    self.workspace,
                    vid,
                    cloud_state="checked_in",
                    lock_user="",
                    lock_user_id="",
                    lock_role="",
                )
            self._notify_cloud_change()
            self._refresh_titles()
            self._set_status(
                f"Admin force checked in {len(result['released'])} title(s)."
            )

        self._run_worker("Admin force check in…", worker, on_done)

    def _admin_take_over(self) -> None:
        ids = self._selected_video_ids()
        if not ids:
            messagebox.showinfo("Nothing selected", "Select one or more titles first.", parent=self)
            return
        if not self._require_admin():
            return
        if not messagebox.askyesno(
            "Admin Take Over",
            f"Download and take over {len(ids)} title(s)?\n\n"
            "This will overwrite the current lock with your admin identity.",
            parent=self,
        ):
            return
        store = self._get_store()
        if not store:
            return

        def worker() -> dict[str, Any]:
            imported: list[dict[str, Any]] = []
            import tempfile
            for vid in ids:
                previous_lock = store.get_lock(vid)
                self._queue.put(("status", f"Taking over {vid}…"))
                asr_bytes = store.download_asr(vid)
                with tempfile.NamedTemporaryFile(suffix=".asr", delete=False) as tf:
                    tf.write(asr_bytes)
                    tmp_path = Path(tf.name)
                try:
                    result = self._unpack_fn(tmp_path, self.workspace, [vid])
                    if not result:
                        continue
                finally:
                    tmp_path.unlink(missing_ok=True)

                store.create_lock(vid)
                store.write_audit_safe(
                    "admin_take_over",
                    vid,
                    details={"previous_lock": previous_lock},
                )
                imported.append({"video_id": vid})

            return {"imported": imported}

        def on_done(result: dict[str, Any]) -> None:
            for item in result["imported"]:
                vid = item["video_id"]
                try:
                    project = self._get_project(vid)
                except Exception:
                    project = None
                update_cloud_state_entry(
                    self.workspace,
                    vid,
                    cloud_state="checked_out_self",
                    lock_user=actor_name_for_config(self._config),
                    lock_user_id=self._config.user_id.strip(),
                    lock_role=self._config.normalized_role(),
                    title=str(getattr(project, "title", "") or ""),
                    channel=str(getattr(project, "channel", "") or ""),
                    last_synced_updated_at=float(getattr(project, "updated_at", 0.0) or 0.0),
                )
            self._notify_cloud_change()
            self._refresh_titles()
            self._set_status(f"Admin took over {len(result['imported'])} title(s).")

        self._run_worker("Admin take over…", worker, on_done)

    # ------------------------------------------------------------------ delete from cloud

    def _delete_cloud(self) -> None:
        ids = self._selected_video_ids()
        if not ids:
            messagebox.showinfo("Nothing selected", "Select one or more titles to delete.", parent=self)
            return
        if not self._require_identity():
            return
        if not messagebox.askyesno(
            "Confirm delete",
            f"Permanently delete {len(ids)} title(s) from the cloud?\n\nThis cannot be undone.",
            parent=self,
        ):
            return
        store = self._get_store()
        if not store:
            return

        def worker() -> dict[str, Any]:
            deleted: list[str] = []
            skipped_locked: list[str] = []
            for vid in ids:
                lock = store.get_lock(vid)
                if lock and not lock_belongs_to(lock, self._config) and not config_is_admin(self._config):
                    skipped_locked.append(f"{vid} ({lock_owner_label(lock)})")
                    continue
                self._queue.put(("status", f"Deleting {vid}…"))
                store.delete_asr(vid)
                store.write_audit_safe(
                    "delete",
                    vid,
                    details={"previous_lock": lock},
                )
                deleted.append(vid)
            return {"deleted": deleted, "skipped_locked": skipped_locked}

        def on_done(result: dict[str, Any]) -> None:
            for vid in result["deleted"]:
                remove_cloud_state_entry(self.workspace, vid)
            self._notify_cloud_change()
            self._refresh_titles()
            self._set_status(f"Deleted {len(result['deleted'])} title(s) from cloud.")
            skipped_locked = result.get("skipped_locked") or []
            if skipped_locked:
                messagebox.showinfo(
                    "Delete skipped",
                    "These titles are checked out by another user and were not deleted:\n\n"
                    + "\n".join(skipped_locked),
                    parent=self,
                )

        self._run_worker("Deleting…", worker, on_done)

    # ------------------------------------------------------------------ background worker

    def _run_worker(
        self,
        start_msg: str,
        worker: Callable[[], Any],
        on_done: Callable[[Any], None],
    ) -> None:
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("Busy", "Another operation is in progress.", parent=self)
            return
        self._set_status(start_msg)
        self._done_cb = on_done

        def _run() -> None:
            try:
                result = worker()
            except Exception as exc:
                self._queue.put(("error", str(exc)))
            else:
                self._queue.put(("done", result))

        self._worker = threading.Thread(target=_run, daemon=True)
        self._worker.start()

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "status":
                    self._set_status(str(payload))
                elif kind == "error":
                    self._worker = None
                    self._set_status(f"Error: {payload}")
                    messagebox.showerror("Operation failed", str(payload), parent=self)
                elif kind == "done":
                    self._worker = None
                    cb = getattr(self, "_done_cb", None)
                    self._done_cb = None
                    if cb:
                        cb(payload)
        except queue.Empty:
            pass
        finally:
            try:
                if self.winfo_exists():
                    self.after(150, self._poll_queue)
            except tk.TclError:
                pass


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"
