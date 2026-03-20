import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from yt_subtitle_extract import dataset


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class FetchCaptionFileTests(unittest.TestCase):
    def test_fetch_caption_file_downloads_directly_when_request_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "captions" / "alpha.af.json3"

            with mock.patch.object(
                dataset.urllib.request,
                "urlopen",
                return_value=_FakeResponse(b'{"events": []}'),
            ):
                result = dataset.fetch_caption_file("https://example.com/caption", dest)

            self.assertEqual(result, dest)
            self.assertEqual(dest.read_bytes(), b'{"events": []}')

    def test_fetch_caption_file_falls_back_to_yt_dlp_on_429(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "captions" / "alpha.af.json3"
            http_error = urllib.error.HTTPError(
                "https://example.com/caption",
                429,
                "Too Many Requests",
                hdrs={},
                fp=None,
            )

            def fake_fallback(
                video_url: str,
                fallback_dest: Path,
                *,
                caption_language: str,
                caption_format: str,
                cookies: str | None = None,
            ) -> Path:
                self.assertEqual(video_url, "https://www.youtube.com/watch?v=alpha")
                self.assertEqual(caption_language, "af")
                self.assertEqual(caption_format, "json3")
                self.assertIsNone(cookies)
                fallback_dest.write_text('{"events":[{"tStartMs":0}]}', encoding="utf-8")
                return fallback_dest

            with mock.patch.object(dataset.urllib.request, "urlopen", side_effect=http_error):
                with mock.patch.object(
                    dataset,
                    "_download_caption_file_with_yt_dlp",
                    side_effect=fake_fallback,
                ):
                    result = dataset.fetch_caption_file(
                        "https://example.com/caption",
                        dest,
                        video_url="https://www.youtube.com/watch?v=alpha",
                        caption_language="af",
                        caption_format="json3",
                    )

            self.assertEqual(result, dest)
            self.assertTrue(dest.exists())

    def test_fetch_caption_file_raises_friendly_error_for_429_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "captions" / "alpha.af.json3"
            http_error = urllib.error.HTTPError(
                "https://example.com/caption",
                429,
                "Too Many Requests",
                hdrs={},
                fp=None,
            )

            with mock.patch.object(dataset.urllib.request, "urlopen", side_effect=http_error):
                with self.assertRaises(RuntimeError) as ctx:
                    dataset.fetch_caption_file("https://example.com/caption", dest)

            self.assertIn("HTTP 429", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
