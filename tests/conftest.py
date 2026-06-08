import os
import pathlib
import tempfile

# keep the test run quiet and isolated: no real toasts, no touching the repo's db/feed files
_tmp = pathlib.Path(tempfile.gettempdir())
os.environ["ABEN_NOTIFY"] = "0"
os.environ.setdefault("ABEN_SIGNAL_FEED", str(_tmp / "abenlux-test-feed.jsonl"))
os.environ.setdefault("ABEN_DB", str(_tmp / "abenlux-test.db"))
os.environ.setdefault("ABEN_MATCH_DB", str(_tmp / "abenlux-test-matches.db"))
os.environ.setdefault("ABEN_WT_MEMORY", str(_tmp / "abenlux-test-wt.json"))
os.environ.setdefault("ABEN_CONTACT_DB", str(_tmp / "abenlux-test-contacts.db"))
