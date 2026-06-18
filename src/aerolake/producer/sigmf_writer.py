"""SigMF encoding for AeroLake.

Takes raw IQ samples plus metadata and produces the two byte streams that
form a SigMF capture: the binary samples (``<name>.sigmf-data``) and the
JSON metadata (``<name>.sigmf-meta``).

These bytes are ready to be uploaded as separate S3 objects via the
StorageClient. We do not write temporary files to disk — everything
happens in memory, which keeps the pipeline stateless and efficient.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypedDict

import numpy as np
import sigmf
from sigmf import SigMFFile


class EncodableSignal(Protocol):
    """Minimal interface the encoder needs from any acquisition source.

    Both :class:`aerolake.producer.synthetic.SyntheticSignal` and
    :class:`aerolake.producer.soapy_source.SdrCapture` satisfy this — the
    encoder reads only these four attributes, so it stays agnostic to whether
    the samples are synthetic or captured from real hardware.
    """

    @property
    def samples(self) -> np.ndarray: ...
    @property
    def sample_rate(self) -> float: ...
    @property
    def center_freq(self) -> float: ...
    @property
    def description(self) -> str: ...


class AnnotationFields(TypedDict, total=False):
    """Flattened annotation fields the encoder writes into ``annotations``.

    All optional. The encoder emits an annotation segment covering the whole
    capture only if at least one of these is present. ``label``/``comment`` and
    the ``freq_*_edge`` pair are core SigMF; the ``antenna:*`` keys belong in
    Annotations per the spec (not the Global antenna block), so they ride here.
    """

    label: str
    comment: str
    freq_lower_edge: float
    freq_upper_edge: float
    polarization: str
    azimuth_angle: float
    elevation_angle: float


class AntennaFields(TypedDict, total=False):
    """Flattened scalar fields of the SigMF ``antenna:`` extension (Global).

    All optional except that, by construction upstream, ``model`` is always
    present when an antenna block exists (the spec's required field). The
    encoder maps each key to ``antenna:<key>`` in the Global Object and
    declares the ``antenna`` extension in ``core:extensions``.
    """

    model: str
    type: str
    low_frequency: float
    high_frequency: float
    gain: float
    horizontal_beam_width: float
    vertical_beam_width: float
    cross_polar_discrimination: float
    voltage_standing_wave_ratio: float
    cable_loss: float
    steerable: bool
    mobile: bool
    hagl: float

# SigMF datatype string for np.complex64.
# Format: complex float 32-bit little-endian. See SigMF spec, "Datatypes".
SIGMF_DATATYPE_CF32_LE = "cf32_le"

# SigMF *specification* version we declare in core:version. We derive it from
# the installed sigmf library (its __specification__) rather than hard-coding a
# string, so what we write always matches the spec the tooling implements.
SIGMF_VERSION: str = getattr(sigmf, "__specification__", "1.2.6")


@dataclass(frozen=True)
class SigMFCapture:
    """A SigMF capture ready to be uploaded as two S3 objects.

    Attributes
    ----------
    data_bytes
        Raw binary IQ samples, contents of the ``<name>.sigmf-data`` object.
    meta_bytes
        JSON-encoded metadata (UTF-8), contents of the ``<name>.sigmf-meta``
        object.
    """

    data_bytes: bytes
    meta_bytes: bytes


def encode(
    signal: EncodableSignal,
    *,
    author: str = "AeroLake",
    recorder: str = "aerolake-producer-synthetic",
    hardware: str = "synthetic",
    signal_type: str | None = None,
    signal_type_detail: str | None = None,
    operator: str | None = None,
    location: str | None = None,
    mobile: bool | None = None,
    hardware_info: dict[str, str] | None = None,
    overflow_count: int | None = None,
    description: str | None = None,
    license: str | None = None,
    geolocation: dict[str, object] | None = None,
    annotation: AnnotationFields | None = None,
    segment_annotations: list[dict[str, object]] | None = None,
    antenna: AntennaFields | None = None,
) -> SigMFCapture:
    """Encode a SyntheticSignal into SigMF byte streams.

    Validates the metadata against the SigMF schema before returning, so
    any structural mistake fails fast here rather than silently producing
    a non-compliant capture on MinIO.

    Parameters
    ----------
    signal
        Output of one of the generators in :mod:`aerolake.producer.synthetic`.
    author
        Goes into ``global.core:author`` (free text).
    recorder
        Goes into ``global.core:recorder`` (identifies the software).
    hardware
        Goes into ``global.core:hw`` (identifies the device).

    Returns
    -------
    SigMFCapture
        Two byte streams ready to upload, with validated metadata.
    """
    # --- 1. Pack the samples to raw bytes ----------------------------------
    # numpy stores complex64 in C order as alternating float32 I and Q.
    # tobytes() gives us exactly the bytes SigMF expects for cf32_le.
    data_bytes = signal.samples.tobytes()

    # --- 2. Build the metadata dict per SigMF v1.0.0 spec ------------------
    # AeroLake namespace: lab-specific, discovery-oriented fields. SigMF leaves
    # domain fields open; we prefix ours with "aerolake:" so they coexist with
    # core: without clashing. Duration is always derived (never hand-entered).
    # Optional fields are omitted when absent, so "no location" is a real
    # absence rather than an empty string.
    sample_rate = float(signal.sample_rate)
    duration_s = len(signal.samples) / sample_rate if sample_rate > 0 else 0.0
    global_block: dict[str, object] = {
        "core:datatype": SIGMF_DATATYPE_CF32_LE,
        "core:sample_rate": sample_rate,
        "core:author": author,
        "core:description": description if description is not None else signal.description,
        "core:recorder": recorder,
        "core:hw": hardware,
        "core:version": SIGMF_VERSION,
        "aerolake:duration_s": duration_s,
        "aerolake:sample_count": len(signal.samples),
    }
    # A license URL is optional; include it only when the user supplied one.
    if license is not None:
        global_block["core:license"] = license
    if signal_type is not None:
        global_block["aerolake:signal_type"] = signal_type
    if signal_type_detail is not None:
        global_block["aerolake:signal_type_detail"] = signal_type_detail
    if operator is not None:
        global_block["aerolake:operator"] = operator
    if location is not None:
        global_block["aerolake:location"] = location
    if mobile is not None:
        global_block["aerolake:mobile"] = bool(mobile)
    # Full hardware provenance as reported by the SDR (manufacturer,
    # product, tuner, label, ...). Stored as a sub-object so it works for
    # any device without knowing its keys in advance. Absent for synthetic.
    if hardware_info:
        global_block["aerolake:hardware_info"] = hardware_info
    # Number of stream overflows during capture: 0 means a clean, gapless
    # recording; >0 means some samples were dropped (USB/host hiccups) and the
    # capture may contain discontinuities. None for synthetic (not applicable).
    if overflow_count is not None:
        global_block["aerolake:overflow_count"] = overflow_count

    # Antenna extension (Global scope): map each supplied scalar to
    # antenna:<key>. The model is the spec's required field and is always
    # present when an antenna block exists. Booleans are normalized.
    antenna_used = bool(antenna)
    if antenna:
        for key, value in antenna.items():
            if value is not None:
                global_block[f"antenna:{key}"] = value

    # The antenna extension is also "in use" if pointing fields ride in the
    # annotation (polarization/azimuth/elevation are antenna:* keys). Declare
    # it whenever any antenna:* key appears anywhere, or the file is
    # non-compliant (undeclared extension in use).
    antenna_in_annotation = annotation is not None and any(
        k in annotation for k in ("polarization", "azimuth_angle", "elevation_angle")
    )

    # Declare the aerolake namespace as a SigMF extension. The spec requires
    # any custom "<name>:" field to be declared here; without this, current
    # SigMF warns and future versions reject the file. optional=True: readers
    # that don't know the extension can still decode the IQ.
    extensions: list[dict[str, object]] = [
        {"name": "aerolake", "version": "1.0.0", "optional": True}
    ]
    # The antenna extension is canonical in SigMF; declare it too whenever any
    # antenna field is present (Global scalars or annotation pointing), so the
    # antenna:* keys are spec-compliant.
    if antenna_used or antenna_in_annotation:
        extensions.append({"name": "antenna", "version": "1.0.0", "optional": True})
    global_block["core:extensions"] = extensions

    # --- Captures segment -------------------------------------------------
    # frequency and datetime were already standard here. geolocation is the
    # spec's preferred scope for position (over the Global object), so it goes
    # in the capture segment when provided. SigMF requires the UTC offset to be
    # written as "Z", not "+00:00".
    capture_segment: dict[str, object] = {
        "core:sample_start": 0,
        "core:frequency": float(signal.center_freq),
        "core:datetime": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    if geolocation is not None:
        capture_segment["core:geolocation"] = geolocation

    # --- Annotations ------------------------------------------------------
    # A single annotation covering the whole capture is emitted only if the
    # user supplied any annotation field. sample_start/sample_count anchor it
    # to the full recording. The freq edges are a pair (both or neither,
    # enforced upstream). polarization/azimuth/elevation live here per spec.
    annotations: list[dict[str, object]] = []
    if annotation:
        ann_segment: dict[str, object] = {
            "core:sample_start": 0,
            "core:sample_count": len(signal.samples),
        }
        if "label" in annotation:
            ann_segment["core:label"] = annotation["label"]
        if "comment" in annotation:
            ann_segment["core:comment"] = annotation["comment"]
        if "freq_lower_edge" in annotation and "freq_upper_edge" in annotation:
            ann_segment["core:freq_lower_edge"] = annotation["freq_lower_edge"]
            ann_segment["core:freq_upper_edge"] = annotation["freq_upper_edge"]
        for ant_key in ("polarization", "azimuth_angle", "elevation_angle"):
            if ant_key in annotation:
                ann_segment[f"antenna:{ant_key}"] = annotation[ant_key]
        annotations.append(ann_segment)

    # Segment annotations (ADR-017): per-event spans built from GNU Radio cursor
    # times / stream tags (see aerolake.producer.annotations). They are already
    # rendered as SigMF annotation Objects, so they're appended as-is, after the
    # optional whole-capture annotation. SigMF allows overlapping annotations.
    if segment_annotations:
        annotations.extend(segment_annotations)

    metadata = {
        "global": global_block,
        "captures": [capture_segment],
        "annotations": annotations,
    }

    # --- 3. Validate against the SigMF schema ------------------------------
    # The library raises if the structure or required fields are wrong.
    # Catching mistakes here means we never write a non-compliant capture
    # to MinIO.
    sigmf_file = SigMFFile(metadata=metadata)
    sigmf_file.validate()

    # --- 4. Serialize metadata as human-readable JSON ----------------------
    # indent=2 + sort_keys=True makes the output diff-friendly and
    # readable when inspecting an object in the MinIO console.
    meta_bytes = json.dumps(
        metadata,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")

    return SigMFCapture(data_bytes=data_bytes, meta_bytes=meta_bytes)
