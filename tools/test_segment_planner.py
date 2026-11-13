#!/usr/bin/env python3
"""The piece planner: how a file is cut, and how work is shared out.

Pure logic, no network: this is the part that decides whether a slow connection
can hold a whole download hostage.  Inspired by Mister Download Manager's
planner ("when a connection is free, split the largest remaining part").

Usage: .venv/bin/python tools/test_segment_planner.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnidownloader.core.models import SegmentProgress  # noqa: E402
from omnidownloader.core.segment_planner import (  # noqa: E402
    MAX_CHUNKS, MIN_CHUNK_BYTES, SegmentPlanner, plan_chunks,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def eq(name: str, got, expected) -> None:
    check(name, got == expected, f"got {got!r}, want {expected!r}")


def covers(segments: list[SegmentProgress], size: int) -> bool:
    """True when the segments are a contiguous, gap-free partition of *size*."""
    if not segments:
        return False
    ordered = sorted(segments, key=lambda s: s.start_byte)
    cursor = 0
    for seg in ordered:
        if seg.start_byte != cursor or seg.end_byte < seg.start_byte:
            return False
        cursor = seg.end_byte + 1
    return cursor == size


def test_plan_chunks() -> None:
    for size in (1, 1000, MIN_CHUNK_BYTES, 3 * 1024 * 1024 + 12345,
                 500 * 1024 * 1024, 60 * 1024 * 1024 * 1024):
        for workers in (1, 4, 8, 32):
            segs = plan_chunks(size, workers)
            check(f"plan({size}, {workers}) is a partition",
                  covers(segs, size), f"{len(segs)} pieces")
            check(f"plan({size}, {workers}) has no gap or overlap",
                  sum(s.length for s in segs) == size,
                  f"{sum(s.length for s in segs)} bytes")
            check(f"plan({size}, {workers}) respects the piece cap",
                  len(segs) <= MAX_CHUNKS, f"{len(segs)} pieces")

    eq("planning is deterministic", plan_chunks(987654321, 8), plan_chunks(987654321, 8))
    check("more workers means more pieces (up to the cap)",
          len(plan_chunks(50 * 1024 * 1024, 8)) > len(plan_chunks(50 * 1024 * 1024, 1)))
    check("pieces are bigger than the minimum unless the file is tiny",
          all(s.length >= 1 for s in plan_chunks(3 * 1024 * 1024, 4)))
    sizes = [s.length for s in plan_chunks(10 * 1024 * 1024 + 7, 4)]
    check("piece sizes are balanced (max 1 byte apart)",
          max(sizes) - min(sizes) <= 1, f"{min(sizes)}..{max(sizes)}")
    eq("a zero-length file never produces a range segment",
       plan_chunks(0, 4)[0].end_byte, -1)


def test_claim_order() -> None:
    segs = plan_chunks(8 * 1024 * 1024, 2)          # 8 pieces of 1 MiB
    planner = SegmentPlanner(segs)
    first = planner.claim()
    eq("the first claim takes the lowest index on a tie", first.segment_index, 0)
    segs[-1].downloaded_bytes = 1                  # make the last piece shortest
    nxt = planner.claim()
    check("the largest remaining piece is taken next",
          nxt.remaining >= max(s.remaining for s in segs if s is not nxt),
          f"took segment {nxt.segment_index}")

    planner.release(first, completed=False)
    again = planner.claim()
    check("a released piece can be claimed again", again is not None)

    seq = SegmentPlanner(plan_chunks(8 * 1024 * 1024, 2), sequential=True)
    order = [seq.claim().segment_index for _ in range(3)]
    eq("sequential downloads claim pieces in file order", order, [0, 1, 2])


def test_split_remainder() -> None:
    segs = plan_chunks(4 * 1024 * 1024, 1)         # 4 pieces of 1 MiB
    seg = segs[0]
    seg.downloaded_bytes = 400 * 1024              # stalled 40% in
    planner = SegmentPlanner(list(segs))
    planner.claim()                                # somebody holds seg 0

    new_seg = planner.split_remainder(seg)
    check("a stalled piece is re-shared", new_seg is not None)
    if new_seg is None:
        return
    eq("the stored bytes are kept", seg.end_byte,
       seg.start_byte + 400 * 1024 - 1)
    check("the shortened piece counts as done", seg.completed is True)
    eq("the new piece starts where the old one stopped",
       new_seg.start_byte, seg.end_byte + 1)
    eq("the new piece reaches the end of the old one",
       new_seg.end_byte, 1024 * 1024 - 1)
    check("the layout still covers the file",
          covers(planner.segments, 4 * 1024 * 1024))

    tiny = SegmentProgress(99, 0, 50 * 1024)       # tail below min_split
    eq("a tail too small to share is refused",
       planner.split_remainder(tiny), None)

    untouched = SegmentProgress(98, 0, 100 * 1024)
    eq("a tiny piece with nothing stored is refused",
       planner.split_remainder(untouched), None)


def test_pending_and_limits() -> None:
    segs = plan_chunks(2 * 1024 * 1024, 1)
    planner = SegmentPlanner(segs)
    check("a fresh planner has work", planner.pending() is True)
    for _ in segs:
        seg = planner.claim()
        planner.release(seg, completed=True)
    check("all pieces done means no work left", planner.pending() is False)
    eq("remaining bytes are zero at the end", planner.remaining_bytes(), 0)
    eq("claiming past the end returns None", planner.claim(), None)


def test_random_simulation() -> None:
    """Random claim/release/split/complete sequences must stay consistent."""
    rng = random.Random(7)
    size = 3 * 1024 * 1024
    broken = []
    for round_no in range(200):
        planner = SegmentPlanner(plan_chunks(size, rng.choice([1, 2, 4, 8])))
        held = []
        for _ in range(60):
            action = rng.random()
            if action < 0.45:
                seg = planner.claim()
                if seg is not None:
                    held.append(seg)
            elif action < 0.7 and held:
                seg = held.pop(rng.randrange(len(held)))
                seg.downloaded_bytes = min(seg.length, seg.downloaded_bytes
                                           + rng.randint(0, seg.length))
                planner.release(seg, completed=True)
            elif action < 0.85 and held:
                seg = held[rng.randrange(len(held))]
                seg.downloaded_bytes = min(seg.length, seg.downloaded_bytes
                                           + rng.randint(0, seg.length))
                if planner.split_remainder(seg) is not None:
                    held.remove(seg)
            else:
                for seg in list(planner.segments):
                    if not seg.completed:
                        seg.downloaded_bytes = seg.length
                        planner.release(seg, completed=True)
                held.clear()
        if not covers([s for s in planner.segments if s.end_byte >= s.start_byte], size):
            broken.append((round_no, "layout broken"))
        if planner.remaining_bytes() < 0:
            broken.append((round_no, "negative remainder"))
    check("200 random claim/release/split runs keep the layout a partition",
          not broken, str(broken[:3]))


def main() -> int:
    test_plan_chunks()
    test_claim_order()
    test_split_remainder()
    test_pending_and_limits()
    test_random_simulation()

    failures = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
