# Fixed Outro Asset

The Facebook → second-channel pipeline appends a fixed outro file:

```
assets/outro.mp4
```

## Current asset

✅ `assets/outro.mp4` is present in this branch (branded outro, moved verbatim
from the repository root on 2026-09-28 — binary bit-identical).

To replace it with a new branded outro, overwrite `assets/outro.mp4` and push.
Requirements:

- Container: MP4 (H.264 video + AAC audio preferred; the pipeline normalizes
  resolution, fps and audio automatically)
- Recommended: 1920×1080, 30 fps, with stereo audio
- Keep it short (typical: 5–40 seconds)

## Behavior when missing

- `/second_channel_check` reports the asset as MISSING.
- `/upload` jobs fail fast with a clear error **before** any download/render.
- First-channel (`/new`, queue, approve) jobs are completely unaffected.

To use a different path, set the `OUTRO_ASSET_PATH` repository variable
(Settings → Secrets and variables → Actions → Variables).
