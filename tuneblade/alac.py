"""Minimal ALAC "uncompressed" frame encoder.

AirPlay 1 receivers expect Apple Lossless. ALAC allows a frame to carry raw
PCM ("escape" frames), which every receiver decodes, so we don't need a real
compressor. Layout (MSB first):

    3 bits  element type   = 1 (CPE, stereo pair)
    4 bits  element tag    = 0
   12 bits  unused         = 0
    1 bit   has size       = 0 (frame length matches the SDP fmtp)
    2 bits  shifted bytes  = 0
    1 bit   not compressed = 1
    N * 32  interleaved big-endian 16-bit samples
    3 bits  end element    = 7
    pad to byte boundary
"""

import numpy as np

FRAMES_PER_PACKET = 352
_HEADER = (1 << 20) | 1  # 23-bit header described above


def encode(pcm: np.ndarray) -> bytes:
    """Encode an (N, 2) int16 array into one ALAC frame."""
    frames = pcm.shape[0]
    samples = pcm.astype(">i2", copy=False).tobytes()
    sample_bits = frames * 32
    total_bits = 23 + sample_bits + 3
    pad = (-total_bits) % 8
    value = (_HEADER << (sample_bits + 3)) | (int.from_bytes(samples, "big") << 3) | 7
    value <<= pad
    return value.to_bytes((total_bits + pad) // 8, "big")
