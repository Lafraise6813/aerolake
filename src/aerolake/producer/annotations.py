"""Turn time-domain signal marks into SigMF temporal annotations.

A GNU Radio QT time/spectrogram sink lets an operator *see* when a signal
appears — a cursor on the time axis — and GNU Radio **stream tags** mark a
precise sample offset where an event occurs. Either way the unit is the same:
*a moment (or span) inside the capture*. SigMF's ``annotations`` array records
exactly that — each entry delimits a segment with ``core:sample_start`` +
``core:sample_count`` and may carry a ``core:label`` / ``core:comment`` and a
frequency band (``core:freq_lower_edge`` / ``core:freq_upper_edge``).

This module is the bridge (ADR-017). It converts marks expressed **in seconds**
(read off a cursor) or **in samples** (from a stream tag) into conformant SigMF
annotation segments, so a capture can be sliced into labelled signal events
instead of carrying a single whole-capture annotation. It is pure logic — no
I/O, no GNU Radio import — so it is fully testable on its own; the encoder
(:func:`aerolake.producer.sigmf_writer.encode`) accepts the dicts it produces.

The flow::

    cursor seconds / stream-tag samples
        -> AnnotationSegment (validated, sample-domain)
        -> to_sigmf() / to_sigmf_annotations()
        -> encode(segment_annotations=...)
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class GnuRadioTag:
    """A minimal stand-in for a GNU Radio stream tag.

    A real ``gr.tag_t`` carries an absolute sample ``offset`` plus a ``key`` and
    ``value``. We only need those three to turn a stream of tags into segments,
    and modelling them here keeps this module free of any GNU Radio import.

    Attributes
    ----------
    offset
        Absolute sample index where the tag sits in the stream.
    key
        Tag key (e.g. ``"burst_start"`` / ``"burst_end"``).
    value
        Optional payload, used as the segment label when present.
    """

    offset: int
    key: str
    value: str = ""


@dataclass(frozen=True)
class AnnotationSegment:
    """One labelled signal span, in the sample domain, ready for SigMF.

    Validated at construction (frozen dataclass + ``__post_init__``): the start
    must be non-negative, the count strictly positive, and the frequency edges
    obey the SigMF pairing rule (both present or neither, lower <= upper). That
    way a malformed mark fails fast here, before it can reach the encoder.

    Attributes
    ----------
    sample_start, sample_count
        The segment as ``core:sample_start`` / ``core:sample_count``.
    label, comment
        Optional ``core:label`` / ``core:comment``.
    freq_lower_edge, freq_upper_edge
        Optional frequency band (Hz); both or neither.
    """

    sample_start: int
    sample_count: int
    label: str | None = None
    comment: str | None = None
    freq_lower_edge: float | None = None
    freq_upper_edge: float | None = None

    def __post_init__(self) -> None:
        if self.sample_start < 0:
            raise ValueError(f"sample_start must be >= 0, got {self.sample_start}")
        if self.sample_count <= 0:
            raise ValueError(f"sample_count must be > 0, got {self.sample_count}")
        has_lower = self.freq_lower_edge is not None
        has_upper = self.freq_upper_edge is not None
        if has_lower != has_upper:
            raise ValueError(
                "freq_lower_edge and freq_upper_edge must both be set or both "
                "omitted (SigMF requires the pair, not one alone)."
            )
        if (
            has_lower
            and has_upper
            and self.freq_lower_edge is not None  # narrow for mypy
            and self.freq_upper_edge is not None
            and self.freq_lower_edge > self.freq_upper_edge
        ):
            raise ValueError("freq_lower_edge must not exceed freq_upper_edge.")

    def to_sigmf(self) -> dict[str, object]:
        """Render this segment as a SigMF annotation Object."""
        seg: dict[str, object] = {
            "core:sample_start": self.sample_start,
            "core:sample_count": self.sample_count,
        }
        if self.label is not None:
            seg["core:label"] = self.label
        if self.comment is not None:
            seg["core:comment"] = self.comment
        if self.freq_lower_edge is not None and self.freq_upper_edge is not None:
            seg["core:freq_lower_edge"] = self.freq_lower_edge
            seg["core:freq_upper_edge"] = self.freq_upper_edge
        return seg


def segment_from_time(
    *,
    start_s: float,
    duration_s: float,
    sample_rate: float,
    label: str | None = None,
    comment: str | None = None,
    freq_lower_edge: float | None = None,
    freq_upper_edge: float | None = None,
) -> AnnotationSegment:
    """Build a segment from a cursor reading in **seconds**.

    Converts the time window to samples using the capture's ``sample_rate``::

        sample_start = round(start_s    * sample_rate)
        sample_count = round(duration_s * sample_rate)

    This is the "operator spotted a signal from t = 2.3 s for 0.5 s" path.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be > 0")
    return AnnotationSegment(
        sample_start=round(start_s * sample_rate),
        sample_count=round(duration_s * sample_rate),
        label=label,
        comment=comment,
        freq_lower_edge=freq_lower_edge,
        freq_upper_edge=freq_upper_edge,
    )


def segments_from_tags(
    tags: Iterable[GnuRadioTag],
    *,
    start_key: str = "burst_start",
    end_key: str = "burst_end",
) -> list[AnnotationSegment]:
    """Pair GNU Radio start/end stream tags into annotation segments.

    Each ``start_key`` tag opens a segment at its sample offset; the next
    ``end_key`` tag closes it, giving ``sample_count = end_offset -
    start_offset``. Tags are processed in offset order, and the label is taken
    from the start tag's ``value`` when it carries one. This is the
    "stream tags marked the bursts" path — the programmatic counterpart to a
    human reading cursors.

    Raises
    ------
    ValueError
        On an unbalanced stream: an end tag with no open start, or a start tag
        left unclosed at the end.
    """
    segments: list[AnnotationSegment] = []
    open_start: GnuRadioTag | None = None
    for tag in sorted(tags, key=lambda t: t.offset):
        if tag.key == start_key:
            if open_start is not None:
                raise ValueError(
                    f"nested {start_key!r} tag at offset {tag.offset} "
                    f"(previous one at {open_start.offset} is still open)"
                )
            open_start = tag
        elif tag.key == end_key:
            if open_start is None:
                raise ValueError(
                    f"{end_key!r} tag at offset {tag.offset} with no open "
                    f"{start_key!r}"
                )
            segments.append(
                AnnotationSegment(
                    sample_start=open_start.offset,
                    sample_count=tag.offset - open_start.offset,
                    label=open_start.value or None,
                )
            )
            open_start = None
    if open_start is not None:
        raise ValueError(
            f"unclosed {start_key!r} tag at offset {open_start.offset} "
            f"(no matching {end_key!r})"
        )
    return segments


def to_sigmf_annotations(segments: Sequence[AnnotationSegment]) -> list[dict[str, object]]:
    """Render a sequence of segments as the SigMF ``annotations`` array."""
    return [seg.to_sigmf() for seg in segments]
