import tempfile
import unittest
from pathlib import Path

from yt_subtitle_extract.dataset import load_segments
from yt_subtitle_extract.gui import (
    delete_local_title_data,
    make_unique_local_video_id,
    subtitle_tracks_from_probe,
)


class LocalMediaImportHelperTests(unittest.TestCase):
    def test_subtitle_tracks_from_probe_marks_supported_and_unsupported_tracks(self):
        payload = {
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264"},
                {
                    "index": 2,
                    "codec_type": "subtitle",
                    "codec_name": "mov_text",
                    "tags": {"language": "en", "title": "English"},
                    "disposition": {"default": 1},
                },
                {
                    "index": 3,
                    "codec_type": "subtitle",
                    "codec_name": "hdmv_pgs_subtitle",
                    "tags": {"language": "af", "title": "Afrikaans"},
                    "disposition": {"default": 0},
                },
            ]
        }

        tracks = subtitle_tracks_from_probe(payload)

        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0]["language"], "en")
        self.assertTrue(tracks[0]["supported"])
        self.assertTrue(tracks[0]["default"])
        self.assertEqual(tracks[1]["language"], "af")
        self.assertFalse(tracks[1]["supported"])

    def test_make_unique_local_video_id_appends_suffix_when_needed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "projects").mkdir(parents=True, exist_ok=True)
            (root / "projects" / "Alpha_Title.json").write_text("{}", encoding="utf-8")

            video_id = make_unique_local_video_id(root, "Alpha Title")

            self.assertEqual(video_id, "Alpha_Title_2")

    def test_load_segments_supports_srt_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            srt_path = Path(tmp_dir) / "sample.srt"
            srt_path.write_text(
                "1\n"
                "00:00:01,000 --> 00:00:03,200\n"
                "Hello world\n\n"
                "2\n"
                "00:00:04,000 --> 00:00:05,000\n"
                "Second line\n",
                encoding="utf-8",
            )

            segments = load_segments(srt_path)

            self.assertEqual(len(segments), 2)
            self.assertEqual(segments[0].text, "Hello world")
            self.assertAlmostEqual(segments[0].start_s, 1.0)
            self.assertAlmostEqual(segments[0].end_s, 3.2)
            self.assertEqual(segments[1].text, "Second line")

    def test_delete_local_title_data_removes_workspace_files_only(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "projects").mkdir(parents=True, exist_ok=True)
            (root / "metadata").mkdir(parents=True, exist_ok=True)
            (root / "captions").mkdir(parents=True, exist_ok=True)
            (root / "working_audio").mkdir(parents=True, exist_ok=True)

            external_audio = root.parent / "outside.wav"
            external_audio.write_bytes(b"outside")

            caption_path = root / "captions" / "alpha.srt"
            caption_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            working_audio = root / "working_audio" / "alpha.wav"
            working_audio.write_bytes(b"wav")
            (root / "projects" / "alpha.json").write_text(
                '{"video_id":"alpha","caption_file":"captions/alpha.srt","audio_file":"working_audio/alpha.wav","working_audio_file":"working_audio/alpha.wav"}',
                encoding="utf-8",
            )
            (root / "metadata" / "alpha.json").write_text(
                f'{{"id":"alpha","caption_file":"captions/alpha.srt","audio_file":"{external_audio.as_posix()}"}}',
                encoding="utf-8",
            )

            removed = delete_local_title_data(root, "alpha")

            self.assertFalse((root / "projects" / "alpha.json").exists())
            self.assertFalse((root / "metadata" / "alpha.json").exists())
            self.assertFalse(caption_path.exists())
            self.assertFalse(working_audio.exists())
            self.assertTrue(external_audio.exists())
            self.assertGreaterEqual(len(removed), 4)


if __name__ == "__main__":
    unittest.main()
