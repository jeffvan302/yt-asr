import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yt_subtitle_extract.gui import (
    ASRReviewApp,
    VideoProject,
    build_manual_segment,
    create_project_from_url,
    effective_playback_samplerate,
    playback_status_text,
    resolve_path,
)


class ManualSentenceHelperTests(unittest.TestCase):
    def make_project(self) -> VideoProject:
        return VideoProject(
            version=1,
            video_id="alpha",
            title="Alpha Title",
            channel="Alpha Channel",
            webpage_url="https://example.com/watch?v=alpha",
            duration=12.0,
            caption_language="af",
            caption_file="captions/alpha.af.json3",
            audio_file="downloads/alpha.wav",
            working_audio_file="working_audio/alpha.wav",
            created_at=1.0,
            updated_at=1.0,
            segments=[],
        )

    def test_build_manual_segment_for_empty_project_starts_at_zero(self):
        project = self.make_project()

        segment = build_manual_segment(project)

        self.assertEqual(segment.text, "<Sentence>")
        self.assertEqual(segment.start_s, 0.0)
        self.assertEqual(segment.end_s, 2.0)

    def test_build_manual_segment_after_existing_segment_starts_at_end(self):
        project = self.make_project()
        project.segments = [build_manual_segment(project)]

        segment = build_manual_segment(project, after_index=0)

        self.assertEqual(segment.start_s, 2.0)
        self.assertEqual(segment.end_s, 4.0)

    def test_add_sentence_inserts_and_selects_new_segment(self):
        app = object.__new__(ASRReviewApp)
        app.current_project = self.make_project()
        app.current_segment_index = None
        app._ensure_project_editable = lambda _action="": True
        app._persist_project = lambda show_feedback=False: None
        app._populate_segment_tree = lambda: None
        selected: list[int] = []
        app._select_segment_by_index = lambda index: selected.append(index)
        statuses: list[str] = []
        app._set_status = lambda message: statuses.append(message)

        app._add_sentence()

        self.assertEqual(len(app.current_project.segments), 1)
        self.assertEqual(app.current_project.segments[0].text, "<Sentence>")
        self.assertEqual(selected, [0])
        self.assertTrue(statuses)


class CreateProjectWithoutCaptionsTests(unittest.TestCase):
    def test_create_project_from_url_creates_empty_project_when_no_captions_exist(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            audio_path = root / "downloads" / "alpha.wav"
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(b"RIFF0000WAVEfmt ")

            info = {
                "id": "alpha",
                "title": "Alpha Title",
                "channel": "Alpha Channel",
                "webpage_url": "https://www.youtube.com/watch?v=alpha",
                "duration": 12.0,
            }

            with mock.patch("yt_subtitle_extract.gui.yt_info", return_value=info):
                with mock.patch("yt_subtitle_extract.gui.download_audio", return_value=audio_path):
                    with mock.patch(
                        "yt_subtitle_extract.gui.select_caption_track",
                        side_effect=RuntimeError("No automatic caption track found"),
                    ):
                        project = create_project_from_url(
                            root,
                            "https://www.youtube.com/watch?v=alpha",
                            "af",
                        )

            caption_path = resolve_path(project.caption_file, root)
            self.assertEqual(project.video_id, "alpha")
            self.assertEqual(project.segments, [])
            self.assertTrue(caption_path.exists())
            payload = json.loads(caption_path.read_text(encoding="utf-8"))
            self.assertEqual(payload, {"events": []})


class PlaybackSpeedHelperTests(unittest.TestCase):
    def test_effective_playback_samplerate_scales_by_speed(self):
        self.assertEqual(effective_playback_samplerate(16000, 0.5), 8000)
        self.assertEqual(effective_playback_samplerate(16000, 1.25), 20000)

    def test_playback_status_text_includes_speed_and_loop(self):
        self.assertEqual(
            playback_status_text("Playing...", 0.75, loop=True),
            "Playing... @ 0.75x [Loop]",
        )
        self.assertEqual(
            playback_status_text("Paused", 1.0),
            "Paused @ 1.00x",
        )


if __name__ == "__main__":
    unittest.main()
