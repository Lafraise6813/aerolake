# ADR-017 — Segment annotations from GNU Radio cursor events / stream tags

- **Status:** Accepted
- **Date:** 2026-06-18
- **Author:** Théo Schmitt
- **Supersedes:** N/A (extends the single-annotation encoder)

## Context

Until now the encoder wrote at most **one** annotation, covering the whole
capture (`sample_start = 0`, `sample_count = len`). But a capture usually
contains *events* — an Iridium burst here, a signal appearing there — and SigMF
is built to record them: the `annotations` array holds any number of segments,
each a `core:sample_start` + `core:sample_count` span with an optional
`core:label` / `core:comment` and frequency band.

The supervisor (Abdu, 2026-06-18 call) asked to explore feeding those segments
from **GNU Radio**: an operator watching a QT time/spectrogram sink sees *when*
a signal appears (a cursor on the time axis), and GNU Radio **stream tags** mark
a precise sample offset where an event occurs. Both are "a moment in the
capture" — exactly what a SigMF annotation segment encodes.

## Decision

**Add `producer/annotations.py`: a pure bridge from time/sample marks to SigMF
annotation segments, and let `encode()` accept a list of them.**

- `AnnotationSegment` — a validated, sample-domain span (`sample_start` >= 0,
  `sample_count` > 0, frequency edges paired and ordered, à la the config
  model). `to_sigmf()` renders it as a SigMF annotation Object.
- `segment_from_time(start_s, duration_s, sample_rate, ...)` — the "operator
  read a cursor" path: seconds → samples (`round(t * sample_rate)`).
- `segments_from_tags(tags, start_key, end_key)` — the "stream tags marked the
  bursts" path: pair `burst_start` / `burst_end` `GnuRadioTag`s (offset + key +
  value) into segments, in offset order, with the start tag's value as label.
  An unbalanced stream (orphan end, unclosed start) raises.
- `encode(..., segment_annotations=[...])` appends these dicts to the
  `annotations` array (after the optional whole-capture one) and validates the
  whole metadata against the SigMF schema as before.

The module is **pure** (no I/O, no GNU Radio import), so it is fully unit-tested
without GNU Radio installed — `GnuRadioTag` is a 3-field stand-in for `gr.tag_t`
(offset/key/value), all this bridge needs.

## Rationale

- **Standard, not bespoke**: segments are written into SigMF's own
  `annotations` array, so any SigMF reader understands them.
- **Two real GNU Radio sources, one model**: a human reading cursors (seconds)
  and a flowgraph emitting stream tags (samples) converge on the same
  `AnnotationSegment`.
- **Testable & decoupled**: keeping the bridge GNU-Radio-free means it runs in
  CI; the encoder only sees plain dicts.

## Consequences

### Positive

- Captures can be sliced into labelled signal events, not just one blob.
- Foundation for an automatic detector later (energy/squelch → tags → segments).

### Negative / open

- This delivers the conversion + encoder support. Producing the tags from a live
  flowgraph (a GNU Radio block that emits `burst_start`/`burst_end`, or a UI to
  export cursor positions) is **future work** and out of scope here.
- `GnuRadioTag` is a minimal stand-in; reading real `gr.tag_t` objects would map
  onto it at the (untested-in-CI) GNU Radio boundary.
- Overlapping segments are allowed (SigMF permits it); de-duplication/merging is
  left to the producer of the marks.

## Alternatives considered

- **Keep a single whole-capture annotation**: cannot mark individual events;
  rejected.
- **Store events in a custom `aerolake:` field**: non-standard; the SigMF
  `annotations` array already models exactly this.
- **Import GNU Radio in the module**: would make the bridge untestable in CI and
  couple a pure mapping to a heavy system dependency; rejected for the 3-field
  stand-in.

## References

- Supervisor call (Abdu, 2026-06-18): GNU Radio cursors × SigMF annotations
- SigMF spec v1.2.6 — `annotations` (sample_start/sample_count, label, freq edges)
- `src/aerolake/producer/annotations.py`,
  `src/aerolake/producer/sigmf_writer.py` (`encode(segment_annotations=...)`),
  `tests/producer/test_annotations.py`
