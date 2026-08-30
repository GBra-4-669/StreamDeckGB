# StreamDeckGB frame-pipeline measurements

Measured on the live setup: StreamDeck MK.2 (A00TA3422LO74P), page `home`
(animated GIF background `giphy-30pjsLvNyaRY0eoE0b.gif`, 7 animated GIF keys,
blend modes: difference/hard-light/screen/lighten/darken), 30fps media loop,
`--devel --debug --daemon-only`, AMD Ryzen 7 7700 + iGPU.

| metric | before | after | delta |
|---|---|---|---|
| CPU, all threads (avg) | 76.1 % | 28.3-28.4 % | **-47.8 pp** |
| render tick per frame | 21-27 ms | 8-10 ms | ~3x faster |
| full frame (tick + USB) | ~33+ ms (29 fps, stalls) | 14-15.5 ms (30 fps, headroom) | |
| Tctl avg, 5-min window | 84.4 C | 71.7 C | **-12.7 C** |
| Tctl max, 5-min window | 85.1 C | 78.1 C | -7.0 C |
| USB stalls >500 ms | 8 in 10 min + per-launch | 0 steady-state | freezes gone |
| AMD GPU fence errors | present (dmesg) | 0 new during after windows | |
| package power (RAPL) | 53.6 W | 53.1 W | ~unchanged* |

\* package power is dominated by concurrent system load (Firefox was at ~45 %
during the after sample); StreamDeckGB's own CPU contribution dropped by
~48 percentage points.

Success criterion (>= 10 C lower with animations enabled, no loss of
functionality) is met: -12.7 C average, and page switching (`home` <-> `ai`)
still works - the `ai` page runs at tick 4-6 ms / total 6-10 ms.

## Reproduce

Before: measure any running instance:
```sh
python3 tools/perf/measure_deck_perf.py --seconds 300 --label before --out before.json
```

After (relaunch the working tree with debug perf logging, wait for the page
load + cache warm-up, then):
```sh
python3 tools/perf/measure_deck_perf.py --seconds 300 --label after --out after.json
python3 tools/perf/measure_deck_perf.py --compare before.json after.json
```

Per-frame render timing (debug builds) shows up in the app log as
`[perf-light] ... tick_ms=.. bg_ms=.. write_ms=.. total_ms=..`.

## Follow-up (same session)

- **GUI fix**: the app was relaunched with `--daemon-only` (copied from the
  autostart entry) for the measurements, which suppresses the main window.
  A plain launch (`streamdeckgb`) builds it - the window is back and shows.
- **Background real-time pacing**: the background video now advances on wall
  clock time instead of the media loop's tick count, so it plays at its
  configured/native rate. Before: the home bg GIF (natively 25fps) played at
  30fps, the ai page's fps=11 ran at 15fps, and the VS page's fps=16 ran at
  30fps (integer division of the 30fps loop). Now they play at exactly their
  intended rate, `Background.update_tiles()` is a near-free no-op between
  frames (bg_ms ~0.00), and keys only re-render when the frame they show
  underneath actually changed. Verified live: home tick ~7-12ms, ai page
  median tick ~4.4ms with near-idle gaps between background frames.
