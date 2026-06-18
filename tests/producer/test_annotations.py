"""Unit tests for aerolake.producer.annotations (ADR-017).

Pure conversion: cursor seconds / GNU Radio stream tags -> validated
AnnotationSegment -> SigMF annotation Objects. The last test wires the result
through the encoder to prove the segments land in a valid .sigmf-meta.
"""

from __future__ import annotations

import json

import pytest

from aerolake.producer.annotations import (
    AnnotationSegment,
    GnuRadioTag,
    segment_from_time,
    segments_from_tags,
    to_sigmf_annotations,
)
from aerolake.producer.sigmf_writer import encode
from aerolake.producer.synthetic import generate_tone

# --- AnnotationSegment validation ----------------------------------------

def test_segment_rejects_negative_start() -> None:
    with pytest.raises(ValueError, match="sample_start"):
        AnnotationSegment(sample_start=-1, sample_count=10)


def test_segment_rejects_nonpositive_count() -> None:
    with pytest.raises(ValueError, match="sample_count"):
        AnnotationSegment(sample_start=0, sample_count=0)


def test_segment_rejects_single_freq_edge() -> None:
    with pytest.raises(ValueError, match="both be set or both omitted"):
        AnnotationSegment(sample_start=0, sample_count=10, freq_lower_edge=1.0)


def test_segment_rejects_inverted_freq_edges() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        AnnotationSegment(
            sample_start=0,
            sample_count=10,
            freq_lower_edge=2.0,
            freq_upper_edge=1.0,
        )


# --- to_sigmf ------------------------------------------------------------

def test_to_sigmf_minimal_has_only_start_and_count() -> None:
    seg = AnnotationSegment(sample_start=100, sample_count=50)
    assert seg.to_sigmf() == {"core:sample_start": 100, "core:sample_count": 50}


def test_to_sigmf_includes_optional_fields() -> None:
    seg = AnnotationSegment(
        sample_start=0,
        sample_count=10,
        label="burst",
        comment="seen on the waterfall",
        freq_lower_edge=1_574_420_000.0,
        freq_upper_edge=1_576_420_000.0,
    )
    out = seg.to_sigmf()
    assert out["core:label"] == "burst"
    assert out["core:comment"] == "seen on the waterfall"
    assert out["core:freq_lower_edge"] == 1_574_420_000.0
    assert out["core:freq_upper_edge"] == 1_576_420_000.0


# --- segment_from_time ---------------------------------------------------

def test_segment_from_time_converts_seconds_to_samples() -> None:
    seg = segment_from_time(start_s=2.0, duration_s=0.5, sample_rate=1000.0, label="x")
    assert seg.sample_start == 2000
    assert seg.sample_count == 500
    assert seg.label == "x"


def test_segment_from_time_rejects_bad_sample_rate() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        segment_from_time(start_s=0.0, duration_s=1.0, sample_rate=0.0)


# --- segments_from_tags --------------------------------------------------

def test_segments_from_tags_pairs_start_and_end() -> None:
    tags = [
        GnuRadioTag(offset=100, key="burst_start", value="iridium"),
        GnuRadioTag(offset=350, key="burst_end"),
        GnuRadioTag(offset=800, key="burst_start"),
        GnuRadioTag(offset=900, key="burst_end"),
    ]
    segs = segments_from_tags(tags)
    assert [(s.sample_start, s.sample_count, s.label) for s in segs] == [
        (100, 250, "iridium"),
        (800, 100, None),
    ]


def test_segments_from_tags_sorts_by_offset() -> None:
    tags = [
        GnuRadioTag(offset=350, key="burst_end"),
        GnuRadioTag(offset=100, key="burst_start"),
    ]
    segs = segments_from_tags(tags)
    assert len(segs) == 1
    assert segs[0].sample_start == 100 and segs[0].sample_count == 250


def test_segments_from_tags_rejects_end_without_start() -> None:
    with pytest.raises(ValueError, match="no open"):
        segments_from_tags([GnuRadioTag(offset=10, key="burst_end")])


def test_segments_from_tags_rejects_unclosed_start() -> None:
    with pytest.raises(ValueError, match="unclosed"):
        segments_from_tags([GnuRadioTag(offset=10, key="burst_start")])


# --- Integration with the encoder ----------------------------------------

def test_encoder_writes_segment_annotations_and_validates() -> None:
    signal = generate_tone(
        duration_s=0.01, sample_rate=100_000, center_freq=1_575_420_000
    )
    segments = [
        segment_from_time(
            start_s=0.001,
            duration_s=0.002,
            sample_rate=signal.sample_rate,
            label="burst-1",
        ),
        segment_from_time(
            start_s=0.005, duration_s=0.001, sample_rate=signal.sample_rate
        ),
    ]
    capture = encode(
        signal,
        signal_type="iridium",
        segment_annotations=to_sigmf_annotations(segments),
    )
    meta = json.loads(capture.meta_bytes.decode("utf-8"))
    anns = meta["annotations"]
    # Two segment annotations made it in, in order, with the right spans.
    assert len(anns) == 2
    assert anns[0]["core:sample_start"] == 100  # 0.001 * 100_000
    assert anns[0]["core:sample_count"] == 200  # 0.002 * 100_000
    assert anns[0]["core:label"] == "burst-1"
    assert anns[1]["core:sample_start"] == 500
