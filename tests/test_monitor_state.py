import tempfile
import unittest
from pathlib import Path

from yt2bili.youtube.monitor import load_state, save_state


class MonitorStateTests(unittest.TestCase):
    def test_load_state_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "processed_videos.json"
            path.write_bytes(
                b"\xef\xbb\xbf"
                + '{"version": 1, "generated_at": "now", "videos": {}}'.encode("utf-8")
            )

            state = load_state(path)

        self.assertEqual({}, state["videos"])

    def test_save_state_writes_utf8_without_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "processed_videos.json"
            save_state(path, {"version": 1, "generated_at": "now", "videos": {}})

            data = path.read_bytes()

        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main()
