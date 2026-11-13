"""Work-stealing segmentation for the multi-connection HTTP engine.

The first version cut a file into exactly one piece per connection and gave each
piece to one connection until it finished.  Two things went wrong with that:

* **The tail is single-threaded.**  The last piece is often the slowest, and
  every other connection sits idle while it finishes.
* **A stalled connection blocks the whole job.**  There is no way to hand that
  piece to somebody else without corrupting the region.

The fix (the same idea Mister Download Manager's planner uses) is to cut the
file into *more* pieces than there are connections and let each free connection
take the largest piece still going, so the tail is shared.  A piece that stops
making progress can then be dropped: the bytes already on disk stay, the rest is
re-split and picked up by another connection.

Everything here is pure bookkeeping — no network, no disk, no asyncio — so the
split logic is testable on its own (``tools/test_segment_planner.py``).
"""

from __future__ import annotations

from typing import Optional

from omnidownloader.core.models import SegmentProgress

#: Pieces per connection in the initial grid.  More pieces give a finer tail at
#: the cost of more requests; below ~4 the tail stops improving.
CHUNKS_PER_CONNECTION = 4

#: Never plan a piece smaller than this (a split remainder can be smaller).
MIN_CHUNK_BYTES = 256 * 1024

#: Hard cap on pieces: a stalled 8 GB download must not plan a million of them.
MAX_CHUNKS = 256

#: A remainder is only worth sharing out when both halves are at least this big.
MIN_SPLIT_BYTES = 128 * 1024


def plan_chunks(
    file_size: int,
    connections: int,
    chunks_per_connection: int = CHUNKS_PER_CONNECTION,
    min_chunk: int = MIN_CHUNK_BYTES,
    max_chunks: int = MAX_CHUNKS,
) -> list[SegmentProgress]:
    """Cut *file_size* into a contiguous, non-overlapping list of segments.

    The layout is deterministic for a given ``(file_size, connections)``: it is
    what the resume sidecar records, so the same file must always produce the
    same numbers or a resume would have to start over.

    Pieces are as equal as the size allows (the first ``file_size % count``
    pieces are one byte longer), which keeps the tail balanced.
    """
    if file_size <= 0:
        return [SegmentProgress(0, 0, -1)]
    connections = max(1, int(connections))
    target = max(1, connections * max(1, chunks_per_connection))

    chunk = max(min_chunk, file_size // target)
    count = max(1, -(-file_size // chunk))          # ceil
    if count > max_chunks:
        count = max_chunks

    base, extra = divmod(file_size, count)
    segments: list[SegmentProgress] = []
    start = 0
    for index in range(count):
        length = base + (1 if index < extra else 0)
        if length <= 0:
            continue
        segments.append(SegmentProgress(index, start, start + length - 1))
        start += length
    return segments


class SegmentPlanner:
    """Hands out the next piece of work to a free connection.

    One planner per download.  ``claim`` returns the largest unfinished piece
    that nobody is working on (or the lowest-numbered one for sequential
    downloads); ``release`` says it is done; ``split_remainder`` gives a stalled
    piece's undownloaded tail to the rest of the pool.
    """

    def __init__(
        self,
        segments: list[SegmentProgress],
        sequential: bool = False,
        min_split: int = MIN_SPLIT_BYTES,
    ) -> None:
        # Kept by reference on purpose: a re-split appends a piece, and the UI
        # (`job.segments`) and the resume sidecar read that same list.
        self.segments: list[SegmentProgress] = (
            segments if isinstance(segments, list) else list(segments))
        self.sequential = sequential
        self.min_split = max(1, int(min_split))
        self._busy: set[int] = set()
        self._next_index = max((s.segment_index for s in self.segments), default=-1) + 1

    # ── state ───────────────────────────────────────────────────
    def pending(self) -> bool:
        """True while some piece still has bytes left to fetch."""
        return any(not s.completed for s in self.segments)

    def active(self) -> list[SegmentProgress]:
        """Pieces currently owned by a connection."""
        return [s for s in self.segments if s.segment_index in self._busy]

    def remaining_bytes(self) -> int:
        return sum(s.remaining for s in self.segments if not s.completed)

    def total_bytes(self) -> int:
        return sum(s.length for s in self.segments if s.end_byte >= 0)

    # ── allocation ──────────────────────────────────────────────
    def claim(self) -> Optional[SegmentProgress]:
        """Reserve the best free piece for a connection, or ``None``.

        ``None`` with :meth:`pending` true means every remaining piece is
        already being downloaded — the caller should wait, not give up.
        """
        free = [s for s in self.segments
                if not s.completed and s.segment_index not in self._busy]
        if not free:
            return None
        if self.sequential:
            chosen = min(free, key=lambda s: s.segment_index)
        else:
            # Largest remaining first: the piece most worth working on, and the
            # one whose absence balances the others.
            chosen = max(free, key=lambda s: (s.remaining, -s.segment_index))
        self._busy.add(chosen.segment_index)
        return chosen

    def release(self, seg: SegmentProgress, completed: bool = True) -> None:
        """Give *seg* back; ``completed`` marks it finished for good."""
        self._busy.discard(seg.segment_index)
        if completed:
            seg.completed = True
            seg.speed_bps = 0.0

    def split_remainder(self, seg: SegmentProgress) -> Optional[SegmentProgress]:
        """Re-share a stalled piece's undownloaded tail with the pool.

        The bytes already on disk stay where they are: *seg* is shortened to end
        exactly at the last stored byte and marked complete, and a brand-new
        piece covers the rest.  The layout therefore stays a partition of the
        file, which is what makes resume state still valid.

        Returns the new piece, or ``None`` when the tail is too small to split.
        """
        if seg.end_byte < 0:
            return None
        stored = max(0, min(seg.downloaded_bytes, seg.length))
        tail_start = seg.start_byte + stored
        tail = seg.end_byte - tail_start + 1
        if tail <= 0:
            return None
        if stored and tail < self.min_split:
            # Only share out the tail when it is big enough to be worth a new
            # connection; a short tail is better downloaded by somebody who
            # already finished.
            return None
        if stored == 0 and tail < 2 * self.min_split:
            return None

        new_seg = SegmentProgress(self._next_index, tail_start, seg.end_byte)
        self._next_index += 1
        if stored:
            seg.end_byte = tail_start - 1
            self.release(seg, completed=True)
        else:
            # Nothing stored: the piece is simply replaced by the new one.
            self._busy.discard(seg.segment_index)
            seg.completed = True
            seg.end_byte = seg.start_byte - 1
        self.segments.append(new_seg)
        return new_seg
