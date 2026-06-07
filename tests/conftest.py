import os
import pathlib
import tempfile

# keep the test run quiet and isolated: no real desktop toasts, no writing the real user feed
os.environ["ABEN_NOTIFY"] = "0"
os.environ.setdefault("ABEN_SIGNAL_FEED", str(pathlib.Path(tempfile.gettempdir()) / "abenlux-test-feed.jsonl"))
