"""Optional equivalent numeric kernels. No altered quality thresholds."""

import ctypes
from pathlib import Path

try:
    import numpy as np
except ImportError:
    np = None

_LIBRARY = None
path = Path(__file__).with_name("__pycache__") / "_fast_frechet_v3.so"
if np is not None and path.exists():
    _LIBRARY = ctypes.CDLL(str(path))
    ptr = ctypes.POINTER(ctypes.c_double)
    _LIBRARY.route_frechet.argtypes = [ptr, ctypes.c_int, ptr, ctypes.c_int, ptr, ptr]
    _LIBRARY.route_frechet.restype = ctypes.c_int
    _LIBRARY.route_corridor.argtypes = [ptr, ctypes.c_int, ptr, ctypes.c_int, ptr]
    _LIBRARY.route_corridor.restype = ctypes.c_int


def available():
    return _LIBRARY is not None


def _coordinates(points):
    values = np.asarray(points, dtype=np.float64)
    if values.shape == (0,):
        return np.empty((0, 2), dtype=np.float64)
    if values.ndim != 2 or values.shape[1] < 2:
        raise ValueError("invalid_geometry_shape")
    values = np.ascontiguousarray(values[:, :2])
    if not np.isfinite(values).all():
        raise ValueError("nonfinite_geometry")
    if np.any(np.abs(values[:, 0]) > 180) or np.any(np.abs(values[:, 1]) > 90):
        raise ValueError("invalid_coordinate_range")
    return values


def ordered(evidence, candidate):
    if _LIBRARY is None:
        return None
    a = _coordinates(evidence)
    b = _coordinates(candidate)
    output = np.zeros(len(a), dtype=np.float64)
    score = ctypes.c_double()
    ptr = ctypes.POINTER(ctypes.c_double)
    code = _LIBRARY.route_frechet(
        a.ctypes.data_as(ptr),
        len(a),
        b.ctypes.data_as(ptr),
        len(b),
        ctypes.byref(score),
        output.ctypes.data_as(ptr),
    )
    if code:
        raise ValueError("native_ordered_resource_limit")
    return score.value, output.tolist()


def corridor(points, route):
    if np is None:
        return None
    p = _coordinates(points)
    r = _coordinates(route)
    if len(r) < 2:
        raise ValueError("empty_corridor")
    if _LIBRARY is not None:
        a = np.ascontiguousarray(p, dtype=np.float64)
        b = np.ascontiguousarray(r, dtype=np.float64)
        output = np.zeros(len(a), dtype=np.float64)
        ptr = ctypes.POINTER(ctypes.c_double)
        code = _LIBRARY.route_corridor(
            a.ctypes.data_as(ptr),
            len(a),
            b.ctypes.data_as(ptr),
            len(b),
            output.ctypes.data_as(ptr),
        )
        if code:
            raise ValueError("native_corridor_resource_limit")
        return output.tolist()

    a, b = r[:-1], r[1:]
    result = []
    for offset in range(0, len(p), 64):
        q = p[offset : offset + 64, None, :]
        scale = np.cos(np.radians(q[:, :, 1]))
        x = (b[:, 0] - a[:, 0]) * scale
        y = b[:, 1] - a[:, 1]
        norm = x * x + y * y
        numerator = (q[:, :, 0] - a[:, 0]) * scale * x + (q[:, :, 1] - a[:, 1]) * y
        t = np.clip(
            np.divide(numerator, norm, out=np.zeros_like(numerator), where=norm != 0),
            0,
            1,
        )
        lon = a[:, 0] + t * (b[:, 0] - a[:, 0])
        lat = a[:, 1] + t * (b[:, 1] - a[:, 1])
        d = 6371000 * np.hypot(
            np.radians(q[:, :, 0] - lon) * np.cos(np.radians((q[:, :, 1] + lat) / 2)),
            np.radians(q[:, :, 1] - lat),
        )
        result.extend(np.min(d, axis=1).tolist())
    return result


def compile_kernel():
    import subprocess

    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "cc",
            "-O3",
            "-shared",
            "-fPIC",
            str(Path(__file__).with_name("fast_frechet.c")),
            "-lm",
            "-o",
            str(path.with_suffix(".build.so")),
        ],
        check=True,
    )
    path.with_suffix(".build.so").replace(path)
    return str(path)


if __name__ == "__main__":
    print(compile_kernel())
