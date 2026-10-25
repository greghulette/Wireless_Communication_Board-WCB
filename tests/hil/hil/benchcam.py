"""The bench camera as an instrument: how much blue LED light a region of its frame holds.

The camera is the Microsoft LifeCam HD-3000 aimed at the bench (1280x720). It is driven through its own venv
(~/.venvs/benchcam: opencv-python 4.9, numpy < 2, pyobjc AVFoundation), whose snap.py finds the camera by name: OpenCV
numbers AVFoundation cameras by uniqueID order, which shifts as virtual cameras come and go. Camera access belongs to the
app this runs under (System Settings > Privacy & Security > Camera). Nothing here moves or touches a board.

The measure is a pixel count: within a window of the frame, the pixels whose blue channel is over 200 and over both
others by 40 - the halo of a lit blue LED. A white-saturated LED core does not count (its channels are equal) and a red
or green LED does not either, so the board's red power LED beside W3's status LED adds nothing; an unlit pixel and the
blue PCB in room light stay far below 200.
"""
import json
import os
import subprocess

VENV_PY = os.path.expanduser("~/.venvs/benchcam/bin/python")
SNAP_DIR = os.path.expanduser("~/.venvs/benchcam")
CAMERA = "LifeCam"

# W3's status LED - the ESP32-S3 DevKit's RGB pixel on GPIO48 - in the camera's RAW frame (not the levelled board-map
# photo), measured on 2026-10-12 at about (1030, 408) of 1280x720: a 60-pixel square around it, normalised x0, y0,
# x1, y1. It leaves out the red power LED's own light 28 px to the right, the probe LEDs in the front row and W2. A
# bench.json "camera": {"w3_led": [x0, y0, x1, y1]} overrides it; re-measure after the camera or W3 moves.
W3_LED_WINDOW = (0.781, 0.525, 0.828, 0.608)

_SCRIPT = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
import AVFoundation as AV
import cv2
import numpy as np
from snap import camera_index

out, name, frames = sys.argv[2], sys.argv[3], int(sys.argv[4])
x0, y0, x1, y1 = (float(v) for v in sys.argv[5].split(","))
if AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeVideo) != 3:
    sys.exit("camera access not allowed (System Settings > Privacy & Security > Camera)")
idx, cam = camera_index(name)
cap = cv2.VideoCapture(idx, cv2.CAP_AVFOUNDATION)
if not cap.isOpened():
    sys.exit(f"could not open {cam} (index {idx})")
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
for _ in range(25):                     # exposure settles
    cap.read()
    time.sleep(0.04)
res, last, box = [], None, None
for _ in range(frames):
    ok, f = cap.read()
    if ok:
        h, w = f.shape[:2]
        box = (int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h))
        roi = f[box[1]:box[3], box[0]:box[2]].astype(int)
        b, g, r = roi[..., 0], roi[..., 1], roi[..., 2]
        res.append(int(((b > 200) & (b - np.maximum(r, g) > 40)).sum()))
        last = f
    time.sleep(0.12)
cap.release()
if last is None:
    sys.exit(f"{cam} gave no frame")
cv2.rectangle(last, box[:2], box[2:], (0, 255, 255), 1)
cv2.imwrite(out, last)
print(json.dumps({"camera": cam, "blue": res, "box": box}))
"""


def available():
    """True when the camera venv and its snap.py are installed on this host."""
    return os.path.exists(VENV_PY) and os.path.exists(os.path.join(SNAP_DIR, "snap.py"))


def w3_led_window(cfg):
    """W3's LED window: bench.json "camera": {"w3_led": [...]}, else W3_LED_WINDOW."""
    v = ((cfg or {}).get("camera") or {}).get("w3_led")
    return tuple(float(x) for x in v) if isinstance(v, list) and len(v) == 4 else W3_LED_WINDOW


class CameraMissing(RuntimeError):
    """No camera named like CAMERA is connected: the bench's camera is unplugged, or on another computer."""


def blue_light(out_jpg, window, frames=8, timeout=60):
    """Grab `frames` frames about 0.12 s apart and count each one's blue-LED pixels in `window` -> the list of counts.
    The last frame is saved to `out_jpg` with the window drawn on it, the evidence a failure points at. Raises
    CameraMissing when no such camera is connected, RuntimeError when it cannot be used."""
    r = subprocess.run([VENV_PY, "-c", _SCRIPT, SNAP_DIR, out_jpg, CAMERA, str(frames), ",".join(str(v) for v in window)],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        last = (r.stderr or r.stdout).strip().splitlines()[-1:]
        if last and "no camera named like" in last[0]:
            raise CameraMissing(f"bench camera: {last[0]}")
        raise RuntimeError(f"bench camera: {last or r.returncode}")
    return json.loads(r.stdout.strip().splitlines()[-1])["blue"]
