import json
import shutil
import tempfile
import unittest
from pathlib import Path

from yt_subtitle_extract.cloud import save_cloud_state
from yt_subtitle_extract.gui import ASRReviewApp, VideoProject, discover_videos


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class DiscoverVideosCloudStateTests(unittest.TestCase):
    def test_checked_out_titles_sort_first_and_checked_in_titles_are_read_only(self):
        root = Path(__file__).resolve().parent.parent / "tmp" / "test_gui_cloud_state_workspace"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        try:
            write_json(
                root / "projects" / "alpha.json",
                {
                    "video_id": "alpha",
                    "title": "Alpha Title",
                    "channel": "Alpha Channel",
                    "segments": [],
                    "caption_file": "captions/alpha.json3",
                    "audio_file": "downloads/alpha.wav",
                    "working_audio_file": "working_audio/alpha.wav",
                },
            )
            write_json(
                root / "projects" / "beta.json",
                {
                    "video_id": "beta",
                    "title": "Beta Title",
                    "channel": "Beta Channel",
                    "segments": [],
                    "caption_file": "captions/beta.json3",
                    "audio_file": "downloads/beta.wav",
                    "working_audio_file": "working_audio/beta.wav",
                },
            )
            write_json(
                root / "metadata" / "gamma.json",
                {
                    "id": "gamma",
                    "title": "Gamma Title",
                    "channel": "Gamma Channel",
                },
            )

            save_cloud_state(
                root,
                {
                    "alpha": {"cloud_state": "checked_out_self", "lock_user": "Me"},
                    "beta": {"cloud_state": "checked_in", "lock_user": ""},
                },
            )

            summaries = discover_videos(root)

            self.assertEqual([item["video_id"] for item in summaries], ["alpha", "beta", "gamma"])
            self.assertEqual(summaries[0]["state_label"], "Checked Out")
            self.assertFalse(summaries[0]["read_only"])
            self.assertEqual(summaries[1]["state_label"], "Checked In")
            self.assertTrue(summaries[1]["read_only"])
            self.assertEqual(summaries[2]["state_label"], "Downloaded")
            self.assertFalse(summaries[2]["read_only"])
        finally:
            shutil.rmtree(root, ignore_errors=True)


class AutoSyncQueueTests(unittest.TestCase):
    def make_project(self) -> VideoProject:
        return VideoProject(
            version=1,
            video_id="alpha",
            title="Alpha Title",
            channel="Alpha Channel",
            webpage_url="https://example.com/watch?v=alpha",
            duration=12.0,
            caption_language="af",
            caption_file="captions/alpha.json3",
            audio_file="downloads/alpha.wav",
            working_audio_file="working_audio/alpha.wav",
            created_at=1.0,
            updated_at=1.0,
            segments=[],
        )

    def test_persist_project_queues_auto_sync_for_checked_out_title(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            app = object.__new__(ASRReviewApp)
            app.workspace = root
            app.current_project = self.make_project()
            app.current_video_summary = {"cloud_state": "checked_out_self"}
            app.current_read_only = False
            app._set_status = lambda _message: None

            queued: list[tuple[Path, str, float]] = []
            app._queue_auto_sync = lambda workspace, video_id, updated_at: queued.append(
                (workspace, video_id, updated_at)
            )

            app._persist_project(show_feedback=False)

            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0][0], root)
            self.assertEqual(queued[0][1], "alpha")
            self.assertGreater(queued[0][2], 1.0)

    def test_persist_project_does_not_queue_auto_sync_for_checked_in_title(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            app = object.__new__(ASRReviewApp)
            app.workspace = root
            app.current_project = self.make_project()
            app.current_video_summary = {"cloud_state": "checked_in"}
            app.current_read_only = False
            app._set_status = lambda _message: None

            queued: list[tuple[Path, str, float]] = []
            app._queue_auto_sync = lambda workspace, video_id, updated_at: queued.append(
                (workspace, video_id, updated_at)
            )

            app._persist_project(show_feedback=False)

            self.assertEqual(queued, [])


if __name__ == "__main__":
    unittest.main()
