#!/usr/bin/env python3
"""Regression tests: /second_channel_check must NEVER enter the render pipeline.

Covers the live incident where a second_channel_check payload was executed
by worker code that fell into the first-channel download/render path and
died with KeyError 'audio_file_id':

  - worker main() routes mode=second_channel_check to the pure check
  - the pure check works with a payload that has NO audio/media keys
    (a StrictPayload raises if ANY media key is touched)
  - the pure check reports PASS/FAIL with channel identity + outro status
  - the optional ffprobe probe never fails a green check
  - normal first-channel + Facebook video routing is unchanged
  - the bot dispatches the check payload with mode set and no media keys

Usage (needs requirements installed, no network performed):
    pip install -r requirements.txt
    python3 scripts/test_worker_check.py

Exit code 0 = all green, 1 = failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

# Hermetic env: no real secrets, dummy token (all senders are mocked anyway).
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TESTDUMMY"
for var in ("YOUTUBE_TOKEN_JSON", "YOUTUBE_SECOND_CLIENT_ID",
            "YOUTUBE_SECOND_CLIENT_SECRET", "YOUTUBE_SECOND_REFRESH_TOKEN",
            "GITHUB_EVENT_PATH", "AUTHORIZED_TELEGRAM_USER_ID"):
    os.environ.pop(var, None)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    icon = "✅" if cond else "❌"
    print(f"{icon} {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not cond:
        FAILURES.append(name)


FORBIDDEN_MEDIA_KEYS = {
    "audio_file_id", "audio_ext", "audio_path",
    "outro_file_id", "outro_ext",
    "thumbnail_file_id", "thumbnail_ext",
    "reference_file_id", "reference_ext",
    "facebook_url", "youtube_url", "video_path",
}


class StrictPayload(dict):
    """Dict that explodes if the check touches any media/render key."""

    def __getitem__(self, key):
        if key in FORBIDDEN_MEDIA_KEYS:
            raise AssertionError(f"pure check accessed forbidden key: {key}")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in FORBIDDEN_MEDIA_KEYS:
            raise AssertionError(f"pure check accessed forbidden key: {key}")
        return super().get(key, default)


def main() -> int:
    import github_worker as gw

    # ── T1: main() routes mode=second_channel_check to the pure check ──
    check_payload = {"job_id": "fb-check-20990101-000000",
                     "song_name": "Second channel check",
                     "chat_id": 111, "user_id": 111,
                     "mode": "second_channel_check",
                     "target_channel": "second"}
    calls: dict[str, list] = {"check": [], "fb": [], "sent": []}
    with patch.object(gw, "read_payload", return_value=dict(check_payload)), \
         patch.object(gw, "run_second_channel_check",
                      side_effect=lambda p: calls["check"].append(p)), \
         patch.object(gw, "run_facebook_job",
                      side_effect=AssertionError("FB video path must not run")), \
         patch.object(gw, "send_message",
                      side_effect=AssertionError("first-channel path must not run")):
        try:
            gw.main()
            routed_ok = True
            routed_err = ""
        except Exception as exc:  # noqa: BLE001
            routed_ok = False
            routed_err = repr(exc)
    check("mode=second_channel_check routes to pure check (no audio keys)",
          routed_ok and len(calls["check"]) == 1, routed_err)
    check("routed payload preserves mode + target",
          calls["check"] and calls["check"][0].get("mode") == "second_channel_check"
          and calls["check"][0].get("target_channel") == "second")

    # ── T2: Facebook video payloads still route to run_facebook_job ──
    fb_payload = {"job_id": "fb-1", "song_name": "FB fb-1", "chat_id": 111,
                  "user_id": 111, "target_channel": "second",
                  "source_type": "facebook_url", "facebook_url": "https://fb.com/x"}
    with patch.object(gw, "read_payload", return_value=dict(fb_payload)), \
         patch.object(gw, "run_second_channel_check",
                      side_effect=AssertionError("check must not run for video jobs")), \
         patch.object(gw, "run_facebook_job",
                      side_effect=lambda p: calls["fb"].append(p)), \
         patch.object(gw, "send_message",
                      side_effect=AssertionError("first-channel path must not run")):
        try:
            gw.main()
            fb_ok = True
            fb_err = ""
        except Exception as exc:  # noqa: BLE001
            fb_ok = False
            fb_err = repr(exc)
    check("facebook video job still routes to run_facebook_job",
          fb_ok and len(calls["fb"]) == 1, fb_err)

    # ── T3: normal first-channel payload path is unchanged ──
    normal_payload = {"job_id": "n-1", "song_name": "Song", "chat_id": 111,
                      "source_type": "telegram_audio", "audio_file_id": "f1"}
    fake_popen = lambda *a, **k: SimpleNamespace(read=lambda: "v9.9.9-test\n")
    with patch.object(gw, "read_payload", return_value=dict(normal_payload)), \
         patch("os.popen", fake_popen), \
         patch.object(gw, "run_second_channel_check",
                      side_effect=AssertionError("check must not run")), \
         patch.object(gw, "run_facebook_job",
                      side_effect=AssertionError("FB job must not run")), \
         patch.object(gw, "send_message",
                      side_effect=lambda c, t: calls["sent"].append(t)), \
         patch.object(gw, "validate_youtube_token_early",
                      return_value=(False, "x-token-bad")):
        try:
            gw.main()
            normal_err = "no exception (expected RuntimeError)"
            normal_ok = False
        except RuntimeError as exc:
            normal_ok = str(exc) == "x-token-bad"
            normal_err = str(exc)
        except Exception as exc:  # noqa: BLE001
            normal_ok = False
            normal_err = repr(exc)
    check("normal job still takes first-channel path (early token gate)",
          normal_ok, normal_err)

    # ── T4: pure check PASSES with zero media keys ──
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(b"\x00" * 1024)
        tmp_outro = Path(tmp.name)
    strict = StrictPayload(check_payload)
    with patch.object(gw, "validate_second_channel_early",
                      return_value=(True, "Second channel verified: 'X' (id: UC123) — upload ready.")), \
         patch("facebook_pipeline.resolve_outro_asset", return_value=tmp_outro), \
         patch.object(gw, "probe_outro_with_ffprobe", return_value=None), \
         patch.object(gw, "send_message",
                      side_effect=lambda c, t: calls["sent"].append(t)):
        try:
            gw.run_second_channel_check(strict)
            pass_ok, pass_err = True, ""
        except Exception as exc:  # noqa: BLE001
            pass_ok, pass_err = False, repr(exc)
    last_msg = calls["sent"][-1] if calls["sent"] else ""
    check("pure check passes without audio_file_id", pass_ok, pass_err)
    check("PASS message reports channel identity",
          "PASSED" in last_msg and "UC123" in last_msg, last_msg[:100])
    check("PASS message reports outro asset", "outro" in last_msg.lower(), last_msg[:100])
    check("strict payload untouched (no media key access)", pass_ok)
    tmp_outro.unlink(missing_ok=True)

    # ── T5: pure check FAILS cleanly on bad credentials ──
    calls["sent"].clear()
    with patch.object(gw, "validate_second_channel_early",
                      return_value=(False, "refresh token REVOKED")), \
         patch("facebook_pipeline.resolve_outro_asset",
               return_value=Path("/nonexistent/outro.mp4")), \
         patch.object(gw, "send_message",
                      side_effect=lambda c, t: calls["sent"].append(t)):
        try:
            gw.run_second_channel_check(StrictPayload(check_payload))
            fail_ok, fail_err = False, "no exception (expected RuntimeError)"
        except RuntimeError as exc:
            fail_ok = "REVOKED" in str(exc) or "failed" in str(exc).lower()
            fail_err = str(exc)[:100]
        except Exception as exc:  # noqa: BLE001
            fail_ok, fail_err = False, repr(exc)
    check("pure check fails cleanly on bad credentials", fail_ok, fail_err)
    check("FAIL message sent to Telegram",
          any("FAILED" in m for m in calls["sent"]))

    # ── T6: pure check FAILS on missing outro (auth green) ──
    calls["sent"].clear()
    with patch.object(gw, "validate_second_channel_early",
                      return_value=(True, "Second channel verified: 'X' (id: UC1) — upload ready.")), \
         patch("facebook_pipeline.resolve_outro_asset",
               return_value=Path("/nonexistent/outro.mp4")), \
         patch.object(gw, "send_message",
                      side_effect=lambda c, t: calls["sent"].append(t)):
        try:
            gw.run_second_channel_check(StrictPayload(check_payload))
            outro_ok, outro_err = False, "no exception (expected RuntimeError)"
        except RuntimeError as exc:
            outro_ok = "MISSING" in str(exc)
            outro_err = str(exc)[:100]
        except Exception as exc:  # noqa: BLE001
            outro_ok, outro_err = False, repr(exc)
    check("pure check fails on missing outro asset", outro_ok, outro_err)

    # ── T7: ffprobe probe is advisory-only and never raises ──
    import shutil
    import subprocess
    with patch.object(shutil, "which", return_value=None):
        w = gw.probe_outro_with_ffprobe(Path("/tmp/x.mp4"))
    check("probe warns (no fail) when ffprobe/ffmpeg missing",
          isinstance(w, str) and "not available" in w, str(w)[:80])
    fake_ok = SimpleNamespace(returncode=0, stderr="")
    with patch.object(shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"), \
         patch.object(subprocess, "run", return_value=fake_ok) as mrun:
        w = gw.probe_outro_with_ffprobe(Path("/tmp/x.mp4"))
    check("probe returns None when media reads fine", w is None, repr(w))
    check("probe uses ffprobe when present",
          "ffprobe" in (mrun.call_args[0][0][0] if mrun.call_args else ""))
    fake_bad = SimpleNamespace(returncode=1, stderr="moov atom not found")
    with patch.object(shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"), \
         patch.object(subprocess, "run", return_value=fake_bad):
        w = gw.probe_outro_with_ffprobe(Path("/tmp/x.mp4"))
    check("probe warns on unreadable media",
          isinstance(w, str) and "could not read" in w and "moov" in w, str(w)[:100])
    with patch.object(shutil, "which", side_effect=lambda n: None if n == "ffprobe" else "/usr/bin/ffmpeg"), \
         patch.object(subprocess, "run", return_value=fake_ok) as mrun2:
        w = gw.probe_outro_with_ffprobe(Path("/tmp/x.mp4"))
    check("probe falls back to ffmpeg decode check",
          w is None and "ffmpeg" in (mrun2.call_args[0][0][0] if mrun2.call_args else ""))
    with patch.object(shutil, "which", side_effect=OSError("boom")):
        try:
            w = gw.probe_outro_with_ffprobe(Path("/tmp/x.mp4"))
            raise_ok = isinstance(w, str)
        except Exception:  # noqa: BLE001
            raise_ok = False
    check("probe never raises", raise_ok)

    # ── T8: bot dispatches check payload with mode + no media keys ──
    import telegram_bot as tb
    dispatched: list[dict] = []
    replies: list[str] = []

    async def fake_reply(text, **kwargs):
        replies.append(text)

    msg = SimpleNamespace(text="/second_channel_check", reply_text=fake_reply)
    upd = SimpleNamespace(message=msg,
                          effective_user=SimpleNamespace(id=111),
                          effective_chat=SimpleNamespace(id=111))
    with patch.object(tb, "ENABLE_FACEBOOK_SECOND_CHANNEL", True), \
         patch.object(tb, "USE_GITHUB_WORKER", True), \
         patch.object(tb, "BASE_URL", "https://example.onrender.com"), \
         patch.object(tb, "dispatch_github_worker",
                      side_effect=lambda p: dispatched.append(p)):
        asyncio.run(tb.second_channel_check_cmd(upd, SimpleNamespace()))
    check("bot dispatches exactly one check payload", len(dispatched) == 1,
          str(len(dispatched)))
    if dispatched:
        p = dispatched[0]
        check("payload carries mode=second_channel_check",
              p.get("mode") == "second_channel_check", str(p.get("mode")))
        check("payload carries target_channel=second",
              p.get("target_channel") == "second", str(p.get("target_channel")))
        leaked = sorted(set(p) & FORBIDDEN_MEDIA_KEYS)
        check("payload has NO media keys (no audio_file_id)", not leaked, str(leaked))
        check("payload has job_id + chat routing",
              str(p.get("job_id", "")).startswith("fb-check-")
              and p.get("chat_id") == 111 and p.get("user_id") == 111,
              str({k: p.get(k) for k in ("job_id", "chat_id", "user_id")}))
    check("bot confirms dispatch to user",
          any("Dispatching second-channel check" in r for r in replies))

    print()
    if FAILURES:
        print(f"WORKER CHECK TESTS FAILED: {len(FAILURES)}: {FAILURES}")
        return 1
    print("WORKER CHECK TESTS PASSED — all green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
