"""A HackRF's samples on the way to the client (unsigned 8-bit IQ, as rtl_tcp sends): the DC spike taken
out, narrowed to a rate below the HackRF's 2 MS/s, and measured (the level the gain control reads).
Sweep blocks are trimmed to the samples hackrf_sweep's FFT uses."""

import math

import numpy as np


ZERO = 127.5                       # rtl_tcp's clients take 127.5 as zero (an RTL-SDR's ADC centre); the HackRF's
                                   # flipped samples sit on 128, half a step off: a DC spike of their own


def _iq(data: bytes) -> np.ndarray:
    u = np.frombuffer(data, np.uint8).astype(np.float32) - ZERO
    return u[0::2] + 1j * u[1::2]


def _cu8(x: np.ndarray, rng=None) -> bytes:
    """Back to unsigned 8-bit; with *rng*, dithered (half a step either way, at random) so rounding keeps the
    mean: the samples came in as whole numbers, and a fractional shift of whole numbers rounds them all the
    same way."""
    b = np.empty(2 * len(x), np.uint8)
    re, im = x.real + ZERO, x.imag + ZERO
    if rng is not None:
        d = rng.random((2, len(x)), np.float32) - 0.5
        re, im = re + d[0], im + d[1]
    b[0::2] = np.clip(np.rint(re), 0, 255)
    b[1::2] = np.clip(np.rint(im), 0, 255)
    return b.tobytes()


class DCBlock:
    """The HackRF's DC spike (its local oscillator leaking through, a constant offset on I and Q): the
    offset is followed slowly (about 0.1 s) and taken off, so only a few hertz at the centre go with it,
    leaving the samples centred where the clients take zero to be (127.5, not the HackRF's 128).
    The samples are whole numbers, so rounding back to 8 bits is dithered: without it every sample rounds
    the same way and up to half a step of offset is left (a spike 8 dB tall on a real HackRF)."""

    def __init__(self, seconds=0.1):
        self.seconds = seconds
        self.dc = None
        self.rng = np.random.default_rng()

    def __call__(self, data: bytes, rate: int) -> bytes:
        n = len(data) // 2 * 2
        if not n:
            return data
        x = _iq(data[:n])
        m = complex(x.mean())
        a = min(1.0, len(x) / max(rate * self.seconds, 1.0))
        self.dc = m if self.dc is None else self.dc + a * (m - self.dc)
        return _cu8(x - self.dc, self.rng) + data[n:]


class Decimator:
    """IQ at 1/k the rate: a windowed-sinc low-pass (to 0.8 of the new Nyquist), then every k-th sample,
    carrying the filter's history and phase from one block to the next."""

    def __init__(self, k: int):
        self.k = k
        n = 16 * k + 1
        t = np.arange(n) - (n - 1) / 2
        cutoff = 0.8 / k
        h = cutoff * np.sinc(cutoff * t) * np.hamming(n)
        self.taps = (h / h.sum()).astype(np.float32)[::-1].copy()
        self.hist = np.zeros(n - 1, np.complex64)
        self.phase = 0                   # where the next output falls in the next block
        self.odd = b""

    def __call__(self, data: bytes) -> bytes:
        data = self.odd + data
        n = len(data) // 2 * 2
        self.odd = data[n:]
        if not n:
            return b""
        x = np.concatenate([self.hist, _iq(data[:n]).astype(np.complex64)])
        taps = len(self.taps)
        count = len(x) - taps + 1                                # outputs possible at full rate
        idx = np.arange(self.phase, count, self.k)
        self.hist = x[len(x) - (taps - 1):]
        self.phase = (idx[-1] + self.k - count) if len(idx) else self.phase - count
        if not len(idx):
            return b""
        y = np.zeros(len(idx), np.complex64)
        for i, h in enumerate(self.taps):                       # tap by tap: no taps x outputs copy
            y += h * x[idx + i]
        return _cu8(y)


def narrowing(rate: int, minimum: int) -> int:
    """The decimation for a rate below the device's minimum: the smallest k with k x rate >= minimum."""
    return 1 if rate >= minimum else -(-minimum // max(int(rate), 1))


class Level:
    """The input level over the last second, from the samples the device sent (before any narrowing):
    average power in dB full scale (hackrf_transfer's sum(x^2) / (n x 127^2), +3 dB for I and Q), the
    peak, and how often a sample sat at the ADC's limit."""

    SAMPLE = 32768                       # bytes of each buffer looked at: plenty at any rate

    def __init__(self):
        self.reset()

    def reset(self):
        self._power = 0.0
        self._n = 0
        self._clipped = 0
        self._peak = 0
        self.last = None

    def __call__(self, data: bytes):
        b = np.frombuffer(data[: self.SAMPLE], np.uint8).astype(np.int16) - 128
        if not len(b):
            return
        self._power += float(np.dot(b.astype(np.float32), b.astype(np.float32)))
        self._n += len(b)
        self._clipped += int(np.count_nonzero((b <= -127) | (b >= 127)))
        self._peak = max(self._peak, int(np.abs(b).max()))

    def take(self) -> dict | None:
        """The second's figures, and start the next."""
        if not self._n:
            return self.last
        ratio = self._power / (self._n * 127 * 127)
        self.last = {"dbfs": round(10 * math.log10(max(ratio, 1e-12)) + 3.0, 1), "peak": self._peak,
                     "clipped": round(self._clipped / self._n, 6)}
        self._power, self._n, self._clipped, self._peak = 0.0, 0, 0, 0
        return self.last


SWEEP_BLOCK = 16384                      # hackrf_sweep's BYTES_PER_BLOCK


def trim_sweep(data: bytes, fft_size: int) -> bytes:
    """hackrf_sweep reads only the header (0x7F 0x7F, the frequency as u64) and the last fft_size samples
    of each 16384-byte block: send just those (10 + 2 x fft_size bytes a block)."""
    keep = 2 * fft_size
    out = []
    for i in range(0, len(data) - SWEEP_BLOCK + 1, SWEEP_BLOCK):
        block = data[i:i + SWEEP_BLOCK]
        if block[0] == 0x7F and block[1] == 0x7F:
            out.append(block[:10] + block[SWEEP_BLOCK - keep:])
    return b"".join(out)
