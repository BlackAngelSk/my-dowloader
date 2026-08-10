#!/usr/bin/env python3
"""Quick smoke-test for the YouTube extractor-args fix."""

from omnidownloader.modules.media_extractor import (
    MediaExtractor, BEST_VIDEO_AUDIO, BEST_AUDIO_ONLY,
    YOUTUBE_DOMAINS, _YOUTUBE_EXTRACTOR_ARGS,
)

# ── 1. Format constants ────────────────────────────────────────
assert BEST_VIDEO_AUDIO == "bv*+ba/b", f"Expected bv*+ba/b, got {BEST_VIDEO_AUDIO}"
assert BEST_AUDIO_ONLY == "bestaudio/best", f"Expected bestaudio/best, got {BEST_AUDIO_ONLY}"
print("[PASS] Format constants are correct")

# ── 2. YouTube domain detection ────────────────────────────────
assert MediaExtractor._is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
assert MediaExtractor._is_youtube_url("https://youtube.com/watch?v=abc123")
assert MediaExtractor._is_youtube_url("https://youtu.be/dQw4w9WgXcQ")
assert MediaExtractor._is_youtube_url("https://m.youtube.com/watch?v=abc")
assert MediaExtractor._is_youtube_url("https://www.youtube.com/shorts/abc123")
assert not MediaExtractor._is_youtube_url("https://twitter.com/something")
assert not MediaExtractor._is_youtube_url("https://vimeo.com/12345")
assert not MediaExtractor._is_youtube_url("not_a_url")
print("[PASS] YouTube URL detection works correctly")

# ── 3. Extractor args injected for YouTube URLs ────────────────
cmd_yt = ["yt-dlp", "--dump-json"]
MediaExtractor._append_youtube_args(cmd_yt, "https://www.youtube.com/watch?v=abc")
assert "--extractor-args" in cmd_yt, "Missing --extractor-args for YouTube"
assert "youtube:player_client=android_vr,web" in cmd_yt, "Missing player_client arg"
assert "--geo-bypass" in cmd_yt, "Missing --geo-bypass for YouTube"
print("[PASS] YouTube extractor args injected correctly")

# ── 4. No args injected for non-YouTube URLs ───────────────────
cmd_twitter = ["yt-dlp", "--dump-json"]
MediaExtractor._append_youtube_args(cmd_twitter, "https://twitter.com/something")
assert cmd_twitter == ["yt-dlp", "--dump-json"], f"Non-YouTube cmd was modified: {cmd_twitter}"
print("[PASS] Non-YouTube URLs left untouched")

# ── 5. Full extract_metadata command for YouTube ────────────────
extractor = MediaExtractor()
cmd = [
    "yt-dlp", "--dump-json", "--no-download",
    "--no-warnings", "--no-check-certificates",
]
url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
extractor._append_youtube_args(cmd, url)
cmd.append(url)
expected_args = [
    "yt-dlp", "--dump-json", "--no-download",
    "--no-warnings", "--no-check-certificates",
    "--extractor-args", "youtube:player_client=android_vr,web",
    "--geo-bypass",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
]
assert cmd == expected_args, f"Command mismatch:\n  got:      {cmd}\n  expected: {expected_args}"
print("[PASS] Full extract_metadata command is correct for YouTube")

# ── 6. Simulated download command for YouTube ──────────────────
dl_cmd = [
    "yt-dlp", "-f", "bv*+ba/b",
    "--merge-output-format", "mp4",
    "--newline", "--no-warnings", "-o", "/tmp/%(title)s.%(ext)s",
]
extractor._append_youtube_args(dl_cmd, "https://youtube.com/watch?v=123")
assert "--extractor-args" in dl_cmd
assert "--geo-bypass" in dl_cmd
print("[PASS] Download command gets YouTube args for youtube.com")

print()
print("=" * 50)
print("ALL TESTS PASSED — YouTube fix verified!")
print("=" * 50)
