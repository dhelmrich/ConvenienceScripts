#!/usr/bin/env python3
"""Detect bitrate overestimation in a video using FFmpeg/libvmaf.

Usage: python bitrate_estimator.py input.mp4 [--threshold 95] [--codec libx264] [--check-res]

Requirements: ffmpeg and ffprobe available on PATH, libvmaf enabled in ffmpeg.
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Optional heavy deps for --check-fps. Import lazily/with helpful errors.
_HAS_CV2 = False
_HAS_NUMPY = False
_HAS_SKIMAGE = False
try:
    import cv2
    _HAS_CV2 = True
except Exception:
    cv2 = None
try:
    import numpy as np
    _HAS_NUMPY = True
except Exception:
    np = None
try:
    from skimage.metrics import structural_similarity as skimage_ssim
    _HAS_SKIMAGE = True
except Exception:
    skimage_ssim = None


def check_tools():
    for t in ("ffmpeg", "ffprobe"):
        if shutil.which(t) is None:
            print(f"Error: {t} not found in PATH.")
            sys.exit(2)


def run(cmd, capture_output=True, check=True):
    try:
        if capture_output:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check, text=True)
        else:
            res = subprocess.run(cmd, check=check)
        return res
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {' '.join(cmd)}")
        if hasattr(e, 'stderr') and e.stderr:
            print(e.stderr)
        raise


def ffprobe_json(path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    res = run(cmd)
    return json.loads(res.stdout)


def get_video_info(path):
    data = ffprobe_json(path)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    # Find first video stream
    vstream = None
    for s in streams:
        if s.get("codec_type") == "video":
            vstream = s
            break

    duration = float(fmt.get("duration") or vstream.get("duration") or 0)
    bit_rate = int(fmt.get("bit_rate") or 0)
    width = int(vstream.get("width") or 0)
    height = int(vstream.get("height") or 0)
    # r_frame_rate like "30000/1001"
    fr = vstream.get("r_frame_rate", "0/1")
    try:
        fr_num, fr_den = fr.split("/")
        framerate = float(fr_num) / float(fr_den)
    except Exception:
        framerate = float(vstream.get("avg_frame_rate") or 0)

    pix_fmt = vstream.get("pix_fmt") or None

    return {
        "duration": duration,
        "bit_rate": bit_rate,
        "width": width,
        "height": height,
        "framerate": framerate,
        "pix_fmt": pix_fmt,
    }


def extract_sample(input_path, out_path, duration, width=None, height=None, reencode=True, pix_fmt="yuv420p"):
    """Extract a ~1s sample from the middle of the input.
    If reencode=True (default) the sample is re-encoded to a stable pixel format
    suitable for bitrate testing. If reencode=False a lightweight copy-based
    extraction is performed to avoid unnecessary work when only doing quick
    resolution/fps checks.
    """
    start = max(0, duration / 2.0 - 0.5)
    if reencode:
        # Re-encode a short sample to ensure consistent resolution/pixel format
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start}",
            "-i",
            str(input_path),
            "-t",
            "1",
        ]
        # If width/height provided, scale to that resolution to guarantee a matching reference
        if width and height:
            cmd += ["-vf", f"scale={width}:{height}:flags=lanczos"]
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-pix_fmt",
            pix_fmt or "yuv420p",
            "-c:a",
            "copy",
            str(out_path),
        ]
    else:
        # Lightweight copy: avoid re-encode, faster for quick checks
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start}",
            "-i",
            str(input_path),
            "-t",
            "1",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            str(out_path),
        ]
    run(cmd, capture_output=False)


def file_bitrate_kbps(path):
    info = ffprobe_json(path).get("format", {})
    b = int(info.get("bit_rate") or 0)
    if b <= 0:
        # fallback to size/duration
        size = os.path.getsize(path)
        dur = float(info.get("duration") or 1)
        b = int(size * 8 / max(1e-6, dur))
    return max(1, b // 1000)


def reencode_to_bitrate(src, dst, kbps, codec="libx264"):
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-c:v",
        codec,
        "-b:v",
        f"{kbps}k",
        "-c:a",
        "copy",
        str(dst),
    ]
    run(cmd, capture_output=False)


def compute_vmaf(distorted, reference, out_json):
    # distorted first, reference second
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(distorted),
        "-i",
        str(reference),
        "-lavfi",
        f"libvmaf=log_path={out_json}:log_fmt=json",
        "-f",
        "null",
        "-",
    ]
    run(cmd, capture_output=False)
    # Parse JSON
    with open(out_json, "r", encoding="utf-8") as f:
        j = json.load(f)

    # Try to obtain aggregate and frame-wise
    aggregate = {}
    frame_vals = []
    # libvmaf output sometimes includes 'aggregate' or 'pooled_metrics'
    if isinstance(j, dict):
        # frames
        frames = j.get("frames") or []
        for fr in frames:
            m = fr.get("metrics") or {}
            if "vmaf" in m:
                frame_vals.append(m.get("vmaf"))
        # aggregate may be under 'aggregate' or 'pooled_metrics'
        ag = j.get("aggregate") or j.get("pooled_metrics") or {}
        # common key name variations
        aggregate["vmaf"] = ag.get("VMAF_score") or ag.get("vmaf") or ag.get("VMAF")

    mean_frame = float(sum(frame_vals) / len(frame_vals)) if frame_vals else None
    overall = float(aggregate["vmaf"]) if aggregate.get("vmaf") is not None else None
    return overall, mean_frame, j

def compute_psnr_ssim(distorted, reference, target_size=None):
    """Compute PSNR and SSIM between `distorted` and `reference` using ffmpeg.
    If `target_size` is provided as (w,h), both inputs are scaled to that size
    before measurement to avoid filter reconfiguration issues.
    Returns (psnr_val, ssim_val) where values may be None if parsing failed.
    """
    try:
        if target_size:
            tw, th = int(target_size[0]), int(target_size[1])
            psnr_filter = f"[0:v]scale={tw}:{th}:flags=lanczos,setsar=1/1[dist];[1:v]scale={tw}:{th}:flags=lanczos,setsar=1/1[ref];[dist][ref]psnr"
            ssim_filter = f"[0:v]scale={tw}:{th}:flags=lanczos,setsar=1/1[dist];[1:v]scale={tw}:{th}:flags=lanczos,setsar=1/1[ref];[dist][ref]ssim"
        else:
            # try to probe reference resolution
            try:
                ref_info = ffprobe_json(reference).get("streams", [])[0]
                ref_w = int(ref_info.get("width") or 0)
                ref_h = int(ref_info.get("height") or 0)
            except Exception:
                ref_w = ref_h = 0
            if ref_w > 0 and ref_h > 0:
                psnr_filter = (
                    f"[0:v]scale={ref_w}:{ref_h}:flags=lanczos,setsar=1/1[dist];"
                    f"[1:v]scale={ref_w}:{ref_h}:flags=lanczos,setsar=1/1[ref];"
                    f"[dist][ref]psnr"
                )
                ssim_filter = (
                    f"[0:v]scale={ref_w}:{ref_h}:flags=lanczos,setsar=1/1[dist];"
                    f"[1:v]scale={ref_w}:{ref_h}:flags=lanczos,setsar=1/1[ref];"
                    f"[dist][ref]ssim"
                )
            else:
                psnr_filter = "psnr"
                ssim_filter = "ssim"
    except Exception:
        psnr_filter = "psnr"
        ssim_filter = "ssim"

    psnr_val = None
    ssim_val = None

    p = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(distorted), "-i", str(reference), "-lavfi", psnr_filter, "-f", "null", "-"], capture_output=True)
    if p.stderr:
        for line in p.stderr.splitlines():
            if "average:" in line:
                try:
                    psnr_val = float(line.split("average:")[-1].strip())
                except Exception:
                    pass

    s = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(distorted), "-i", str(reference), "-lavfi", ssim_filter, "-f", "null", "-"], capture_output=True)
    if s.stderr:
        for line in s.stderr.splitlines():
            if "All:" in line:
                try:
                    ssim_val = float(line.split("All:")[-1].split()[0])
                except Exception:
                    pass

    return psnr_val, ssim_val
    
    p = run(psnr_cmd, capture_output=True)
    if p.stderr:
        for line in p.stderr.splitlines():
            if "average:" in line:
                try:
                    psnr_val = float(line.split("average:")[-1].strip())
                except Exception:
                    pass

    ssim_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(distorted),
        "-i",
        str(reference),
        "-lavfi",
        ssim_filter,
        "-f",
        "null",
        "-",
    ]
    s = run(ssim_cmd, capture_output=True)
    if s.stderr:
        for line in s.stderr.splitlines():
            if "All:" in line:
                try:
                    ssim_val = float(line.split("All:")[-1].split()[0])
                except Exception:
                    pass

    return psnr_val, ssim_val

def write_conversion_report(path, findings, ffmpeg_cmd):
    """Write a verbose conversion report to `path`.
    `findings` should be a dict with keys describing why conversion was chosen
    (e.g., resolution/fps info). `ffmpeg_cmd` is the command list that would be run.
    """
    txt = []
    txt.append("Bitrate Estimator Conversion Report")
    txt.append("===============================")
    txt.append("")
    for k, v in findings.items():
        txt.append(f"{k}: {v}")
    txt.append("")
    txt.append("FFmpeg command that would be executed:")
    # show command as a shell-safe joined string for clarity
    try:
        import shlex
        cmdline = " ".join(shlex.quote(str(x)) for x in ffmpeg_cmd)
    except Exception:
        cmdline = " ".join(str(x) for x in ffmpeg_cmd)
    txt.append(cmdline)
    txt.append("")
    txt.append("Note: --report was passed; no conversion was performed.")
    content = "\n".join(txt)
    # Print verbose report to stdout for immediate inspection
    print("\n" + content + "\n")
    # Per user request: do not write report to disk (no file write)


def resolution_check(sample_path, orig_w, orig_h, tmpdir):
    # Candidate lower resolutions (height, width) common: 720p, 1080p, 1440p
    candidates = [ (3840,2160), (2560,1440), (1920,1080), (1280,720) ]
    # Only keep those smaller or equal to original
    cand = []
    for w,h in candidates:
        if w <= orig_w and h <= orig_h:
            cand.append((w,h))

    best = None
    best_ssim = -1.0
    results = []
    for w,h in cand:
        down = Path(tmpdir) / f"down_{w}x{h}.mp4"
        up = Path(tmpdir) / f"up_{w}x{h}.mp4"
        # Downscale to candidate
        cmd1 = [
            "ffmpeg",
            "-y",
            "-i",
            str(sample_path),
            "-vf",
            f"scale={w}:{h}:flags=lanczos",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-t",
            "1",
            str(down),
        ]
        run(cmd1, capture_output=False)
        # Upscale back to original
        cmd2 = [
            "ffmpeg",
            "-y",
            "-i",
            str(down),
            "-vf",
            f"scale={orig_w}:{orig_h}:flags=lanczos",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-t",
            "1",
            str(up),
        ]
        run(cmd2, capture_output=False)
        # Compute SSIM between up and original sample, scaling both to original size
        _, ssim = compute_psnr_ssim(str(up), str(sample_path), target_size=(orig_w, orig_h))
        ssim = ssim or 0.0
        results.append({"res": f"{w}x{h}", "ssim": ssim})
        if ssim > best_ssim:
            best_ssim = ssim
            best = (w, h, ssim)

    likely = None
    if best and best_ssim >= 0.98:
        likely = f"{best[0]}x{best[1]}"
    return likely, results


def get_encoded_r_frame_rate(sample_path):
    """Return the ffprobe r_frame_rate string and numeric fps for the sample."""
    data = ffprobe_json(sample_path)
    streams = data.get("streams", [])
    for s in streams:
        if s.get("codec_type") == "video":
            fr = s.get("r_frame_rate") or s.get("avg_frame_rate") or "0/1"
            try:
                n, d = fr.split("/")
                fps = float(n) / float(d)
            except Exception:
                try:
                    fps = float(fr)
                except Exception:
                    fps = 0.0
            return fr, fps
    return "0/1", 0.0


def extract_frames_for_analysis(sample_path, out_dir, fps=30, max_frames=300):
    """Use ffmpeg to dump frames at `fps` into out_dir as PNGs (frame_0001.png ...).
    Returns list of file paths (may be fewer than max_frames)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / "frame_%04d.png")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(sample_path),
        "-vf",
        f"fps={fps}",
        "-frames:v",
        str(max_frames),
        pattern,
    ]
    run(cmd, capture_output=False)
    # Collect files
    files = sorted(out_dir.glob("frame_*.png"))
    return files


def load_frames_cv2(paths):
    """Load list of image paths into numpy arrays (BGR->GRAY float32)."""
    if not _HAS_CV2 or not _HAS_NUMPY:
        raise RuntimeError("Missing dependencies: pip install opencv-python numpy")
    frames = []
    for p in paths:
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        frames.append(img.astype(np.float32))
    return frames


def phase_correlation(img1, img2):
    """Estimate shift between two grayscale float32 images using phase correlation.
    Returns (dx, dy) in pixels (img2 relative to img1).
    """
    if not _HAS_CV2 or not _HAS_NUMPY:
        raise RuntimeError("Missing dependencies: pip install opencv-python numpy")
    # ensure same shape
    if img1.shape != img2.shape:
        # resize img2 to img1
        img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))

    # Windowing can improve results but keep simple
    # DFT
    f1 = cv2.dft(np.float32(img1), flags=cv2.DFT_COMPLEX_OUTPUT)
    f2 = cv2.dft(np.float32(img2), flags=cv2.DFT_COMPLEX_OUTPUT)
    # cross-power spectrum: (F1 * conj(F2)) / |F1 * conj(F2)|
    conj_f2 = f2.copy()
    conj_f2[:, :, 1] *= -1
    num = np.zeros_like(f1)
    # complex multiplication: (a+ib)*(c+id) = (ac-bd) + i(ad+bc)
    a, b = f1[:, :, 0], f1[:, :, 1]
    c, d = conj_f2[:, :, 0], conj_f2[:, :, 1]
    num[:, :, 0] = a * c - b * d
    num[:, :, 1] = a * d + b * c
    denom = np.sqrt(num[:, :, 0] ** 2 + num[:, :, 1] ** 2)
    denom[denom == 0] = 1e-9
    cps = num / denom[:, :, None]
    # inverse DFT
    corr = cv2.idft(cps, flags=cv2.DFT_SCALE | cv2.DFT_COMPLEX_OUTPUT)
    # magnitude
    mag = np.sqrt(corr[:, :, 0] ** 2 + corr[:, :, 1] ** 2)
    # find peak
    max_idx = np.unravel_index(np.argmax(mag), mag.shape)
    # convert to shift (wrap-around)
    midy, midx = mag.shape[0] // 2, mag.shape[1] // 2
    dy = max_idx[0] if max_idx[0] <= midy else max_idx[0] - mag.shape[0]
    dx = max_idx[1] if max_idx[1] <= midx else max_idx[1] - mag.shape[1]
    return float(dx), float(dy)


def cluster_similar_frames(frames, threshold=0.99):
    """Cluster frames by pairwise SSIM. Returns (unique_count, total, clusters)
    Simple greedy clustering: frames with SSIM >= threshold are in same cluster.
    """
    if not _HAS_NUMPY or not _HAS_SKIMAGE:
        raise RuntimeError("Missing dependencies: pip install numpy scikit-image")
    n = len(frames)
    if n == 0:
        return 0, 0, []
    assigned = [False] * n
    clusters = []
    for i in range(n):
        if assigned[i]:
            continue
        cluster = [i]
        assigned[i] = True
        for j in range(i + 1, n):
            if assigned[j]:
                continue
            try:
                sim = skimage_ssim(frames[i], frames[j])
            except Exception:
                sim = 0.0
            if sim >= threshold:
                cluster.append(j)
                assigned[j] = True
        clusters.append(cluster)
    unique = len(clusters)
    total = n
    return unique, total, clusters


def compute_atv_and_fft(frames):
    """Compute ATV series and its FFT; return ATV array and dominant frequency index k.
    ATV series length = n (skip first and last frames for triplets), return dominant k (in cycles/frame).
    """
    if not _HAS_NUMPY:
        raise RuntimeError("Missing dependencies: pip install numpy")
    n = len(frames)
    if n < 3:
        return np.array([]), None
    atv = []
    for i in range(1, n - 1):
        prev = frames[i - 1]
        cur = frames[i]
        nxt = frames[i + 1]
        # mean absolute pixel diff
        val = np.mean(np.abs(cur - prev)) + np.mean(np.abs(cur - nxt))
        atv.append(val)
    atv = np.array(atv)
    # FFT (real)
    fft = np.abs(np.fft.rfft(atv - np.mean(atv)))
    if fft.size <= 1:
        return atv, None
    # ignore DC (index 0)
    idx = int(np.argmax(fft[1:]) + 1)
    # frequency in cycles per window (per ATV sample)
    return atv, idx


def test_candidate_source_fps(encoded_fps, frames, candidates=[12,24,25,29.97,50,59.94]):
    """Test candidate source fps by decimation and compute mean MAD; return best candidate and scores."""
    if not _HAS_NUMPY:
        raise RuntimeError("Missing dependencies: pip install numpy")
    best = None
    best_score = float('inf')
    scores = {}
    for cand in candidates:
        if cand <= 0:
            continue
        step = max(1, int(round(encoded_fps / cand)))
        dec = frames[::step]
        # compute mean absolute diff between consecutive frames
        if len(dec) < 2:
            # not enough frames to judge this candidate; mark as bad (large score)
            score = float('inf')
        else:
            diffs = [np.mean(np.abs(dec[i].astype(np.float32) - dec[i - 1].astype(np.float32))) for i in range(1, len(dec))]
            score = float(np.mean(diffs))
        scores[cand] = score
        # lower score -> more consistent decimated frames -> better match
        if score < best_score:
            best_score = score
            best = cand
    return best, scores


def binary_search_bitrate(sample_path, orig_kbps, threshold, codec, tmpdir, pix_fmt=None):
    low = max(1, orig_kbps // 16)
    high = max(low, orig_kbps)
    tested = {}

    def test_k(k):
        if k in tested:
            return tested[k]
        out = Path(tmpdir) / f"re_{k}k.mp4"
        # re-encode candidate at requested pixel format when provided
        reencode_to_bitrate(sample_path, out, k, codec=codec)
        vmaf_json = Path(tmpdir) / f"vmaf_{k}k.json"
        overall, mean_frame, j = compute_vmaf(out, sample_path, vmaf_json)
        psnr, ssim = compute_psnr_ssim(out, sample_path)
        tested[k] = {"vmaf_overall": overall, "vmaf_frame_mean": mean_frame, "psnr": psnr, "ssim": ssim}
        return tested[k]

    # Binary search for minimal k where VMAF overall or frame_mean >= threshold
    while low < high:
        mid = (low + high) // 2
        try:
            res = test_k(mid)
        except Exception:
            # In case re-encode or vmaf fails, move up
            low = mid + 1
            continue
        score = res.get("vmaf_overall") or res.get("vmaf_frame_mean") or 0
        if score >= threshold:
            high = mid
        else:
            low = mid + 1

    # Final check
    final = None
    try:
        final = test_k(low)
    except Exception:
        final = None

    if final is None or (final.get("vmaf_overall") or final.get("vmaf_frame_mean") or 0) < threshold:
        return None, tested
    return low, tested


def print_results(orig_bps, optimal_kbps, tested, threshold, res_check=None):
    orig_mbps = orig_bps / 1_000_000
    print(f"Original bitrate: {orig_mbps:.2f} Mbps")
    if optimal_kbps is None:
        print(f"No bitrate found that meets VMAF >= {threshold}")
    else:
        savings = 100.0 * (1 - (optimal_kbps * 1000) / orig_bps)
        print(f"Optimal: {optimal_kbps/1000:.3f} Mbps ({savings:.1f}% savings)")
    print("")
    print("Metrics per tested bitrate:")
    print("- bitrate_kbps | VMAF_overall | VMAF_frame_mean | PSNR | SSIM")
    for k in sorted(tested.keys()):
        r = tested[k]
        print(f"- {k} | {r.get('vmaf_overall')} | {r.get('vmaf_frame_mean')} | {r.get('psnr')} | {r.get('ssim')}")
    if res_check:
        likely, detail = res_check
        print("")
        if likely:
            print(f"Resolution: Likely {likely} (upscaled to sample)")
        else:
            print("Resolution: No clear upscaling detected")


def main():
    parser = argparse.ArgumentParser(description="Detect bitrate overestimation using ffmpeg + libvmaf")
    parser.add_argument("input", help="Input video file")
    parser.add_argument("--threshold", type=float, default=95.0, help="VMAF threshold (default 95)")
    parser.add_argument("--codec", type=str, default="libx264", help="Codec to use for re-encoding (default libx264)")
    parser.add_argument("--report", action="store_true", help="Don't perform conversions; write a verbose report describing findings and ffmpeg commands")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check-res", action="store_true", help="Check likely source resolution by down/upscaling test")
    group.add_argument("--check-fps", action="store_true", help="Analyze framerate and detect likely source FPS (requires opencv-python, numpy, scikit-image)")
    group.add_argument("--check-bit", action="store_true", help="Run bitrate binary-search using VMAF on a re-encoded sample")
    args = parser.parse_args()

    check_tools()

    inp = Path(args.input)
    if not inp.exists():
        print(f"Input file not found: {inp}")
        sys.exit(2)

    info = get_video_info(inp)
    duration = info["duration"]
    orig_bitrate = info["bit_rate"] or 0
    if orig_bitrate <= 0:
        # fallback compute from file size
        orig_bitrate = os.path.getsize(inp) * 8 / max(1e-6, duration)
    tmpdir = tempfile.mkdtemp(prefix="bitrate_est_")
    try:
        sample = Path(tmpdir) / "sample.mp4"
        # Decide which single check to perform (mutually exclusive)
        if args.check_res:
            # lightweight sample for quick analysis
            extract_sample(inp, sample, duration, info.get("width"), info.get("height"), reencode=False)
            print("Starting resolution check...")
            likely, details = resolution_check(sample, info["width"], info["height"], tmpdir)
            print("Resolution check complete.")
            if likely:
                try:
                    w, h = likely.split("x")
                    target_width = int(w)
                    target_height = int(h)
                    out_path = inp.parent / "out.mp4"
                    cmd = [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(inp),
                        "-vsync",
                        "0",
                        "-vf",
                        f"scale={target_width}:{target_height}:flags=lanczos,format=yuv420p",
                        "-c:v",
                        args.codec,
                        "-crf",
                        "18",
                        "-preset",
                        "medium",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "copy",
                        str(out_path),
                    ]
                    print(f"Conversion triggered by resolution detection — writing conforming output to: {out_path}")
                    if args.report:
                        findings = {
                            "reason": "resolution_mismatch",
                            "likely_resolution": likely,
                            "input_pix_fmt": info.get("pix_fmt"),
                            "input_width": info.get("width"),
                            "input_height": info.get("height"),
                        }
                        report_path = out_path.with_suffix(".conversion_report.txt")
                        write_conversion_report(report_path, findings, cmd)
                    else:
                        print("Starting full-file conversion...")
                        run(cmd, capture_output=False)
                        print("Full-file conversion finished.")
                except Exception as e:
                    print("Resolution detection indicated conversion but conversion failed:", e)
            else:
                print("Resolution: No clear upscaling detected")
            return

        if args.check_fps:
            # lightweight sample for quick analysis
            extract_sample(inp, sample, duration, info.get("width"), info.get("height"), reencode=False)
            try:
                print("Starting FPS check...")
                r_frame_str, encoded_fps = get_encoded_r_frame_rate(sample)
                frames_dir = Path(tmpdir) / "frames"
                extract_fps = 30
                frame_paths = extract_frames_for_analysis(sample, frames_dir, fps=extract_fps, max_frames=300)
                frames = load_frames_cv2(frame_paths)
                uniq, total, clusters = cluster_similar_frames(frames, threshold=0.99)
                dupe_ratio = float(uniq) / float(total) if total else 0.0
                atv, dom_idx = compute_atv_and_fft(frames)
                atv_info = None
                if dom_idx is not None and len(atv) > 0:
                    N = len(atv)
                    fs = extract_fps
                    dom_freq_hz = dom_idx * (fs / float(N))
                    if dom_freq_hz > 0:
                        up_factor = float(encoded_fps) / float(dom_freq_hz)
                        atv_info = {"dom_idx": int(dom_idx), "dom_freq_hz": dom_freq_hz, "up_factor": up_factor}
                best_cand, cand_scores = test_candidate_source_fps(encoded_fps, frames)
                savings_pct = None
                if best_cand and encoded_fps > 0 and best_cand != encoded_fps:
                    savings_pct = 100.0 * (1.0 - float(best_cand) / float(encoded_fps))
                    # if candidate differs from encoded, perform conversion
                    target_fps = best_cand
                    out_path = inp.parent / "out.mp4"
                    cmd = [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(inp),
                        "-vsync",
                        "0",
                        "-vf",
                        f"fps={target_fps},format=yuv420p",
                        "-c:v",
                        args.codec,
                        "-crf",
                        "18",
                        "-preset",
                        "medium",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "copy",
                        str(out_path),
                    ]
                    print(f"Conversion triggered by FPS detection — writing conforming output to: {out_path}")
                    if args.report:
                        findings = {
                            "reason": "fps_mismatch",
                            "encoded_fps": encoded_fps,
                            "likely_source_fps": best_cand,
                            "dupe_ratio": dupe_ratio,
                            "frames_analyzed": total,
                            "candidate_scores": cand_scores,
                            "atv_info": atv_info,
                        }
                        report_path = out_path.with_suffix(".conversion_report.txt")
                        write_conversion_report(report_path, findings, cmd)
                    else:
                        print("Starting full-file conversion...")
                        run(cmd, capture_output=False)
                        print("Full-file conversion finished.")
                        print(f"Likely source: {best_cand} fps (dupe ratio {dupe_ratio:.3f})")
                else:
                    print("FPS check complete.")
                    print("")
                    print(f"Encoded FPS (ffprobe r_frame_rate): {r_frame_str} | {encoded_fps}")
                    if best_cand:
                        print(f"Likely source: {best_cand} fps (dupe ratio {dupe_ratio:.3f})")
                    else:
                        print(f"Likely source: unknown (dupe ratio {dupe_ratio:.3f})")
                    if atv_info:
                        print(f"ATV dominant freq: {atv_info.get('dom_freq_hz'):.3f} Hz, up-factor ~{atv_info.get('up_factor'):.2f}")
                    if savings_pct is not None:
                        print(f"Savings if conformed to {best_cand} fps: {savings_pct:.1f}%")
            except RuntimeError as e:
                print("FPS check skipped:", e)
            except Exception as e:
                print("FPS check failed:", e)
            return

        if args.check_bit:
            # For bitrate testing we need a properly re-encoded sample
            extract_sample(inp, sample, duration, info.get("width"), info.get("height"), reencode=True)
            orig_kbps = max(1, int(orig_bitrate) // 1000)
            optimal, tested = binary_search_bitrate(sample, orig_kbps, args.threshold, args.codec, tmpdir)
            print_results(int(orig_bitrate), optimal, tested, args.threshold, None)
            return

    finally:
        # Cleanup
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass


if __name__ == "__main__":
    main()
