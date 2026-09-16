



# StreamDeckGB

A personal fork of [StreamController](https://github.com/StreamController/StreamController) with a handful of quality-of-life changes I use on my own Stream Deck.

[<video autoplay muted loop playsinline controls src="site/videos/streamdeckgb-example-3.mp4"></video>](https://github.com/user-attachments/assets/7709af93-d3d5-4eef-8526-a8e7bf3111db)

[<video autoplay muted loop playsinline controls src="site/videos/streamdeckgb-example.mp4"></video>](https://github.com/user-attachments/assets/05b11f1d-c3f1-4656-885b-586e3486aa93)

[<video autoplay muted loop playsinline controls src="site/videos/streamdeckgb-example-2.mp4"></video>](https://github.com/user-attachments/assets/6871664a-c291-4da7-acda-4a4d6a09cb89)

If the videos do not work or play inline, you can find them also on the [demo page](https://gbra-4-669.github.io/streamcontroller-fork/).

> **This is a private, unmaintained fork.** The repository is public only so I can use GitHub's tooling (Pages, Actions, and releases) for my own testing. Treat it as a snapshot of my working tree, not as software meant for general use.

- **No prebuilt binaries.** There is no installer and no release build.
- **No support.** This will almost certainly not run as-is on your machine; expect to read and modify the source to make it work on your own setup.
- **Use at your own risk.**

All credit for the core application goes to the [StreamController](https://github.com/StreamController/StreamController) team. It is actively maintained and has far more features and broader hardware support than this fork will ever have. Please star, contribute to, and use the upstream project instead of this one.

## What's different from upstream

- **Background opacity**: opacity control for all assets (page and key backgrounds), not just foreground images.
- **Layered images per key**: each key can hold two independent image layers on top of the page background, each with its own opacity. For example, a page background plus an animated GIF plus an icon, all composited together.
- **Per-layer blend modes**: either image layer can use a CSS/SVG-style blend mode instead of a plain overlay. Supported modes are `normal`, `multiply`, `screen`, `darken`, `lighten`, `hard-light`, `overlay`, `color-dodge`, `color-burn`, `difference`, `exclusion`, `soft-light`. Layers blend the same way CSS `mix-blend-mode` does, so each one blends against everything already painted beneath it. Set per layer from the image editor's "Blend Mode" dropdown. Implemented per the W3C compositing spec (hard-light/overlay use the scaled forms).
- **Smart Command**: a new stateful action type. See the source for current behavior; it is actively evolving as I use it and has no stable spec.
- **Watch GitHub Deployments**: displays a repo's deployment status on a key. Press to refresh manually, or enable automatic updates via a local `pre-push` hook. The watch is owned by the plugin rather than by the key, so it keeps polling and keeps its last result no matter which page is on screen.
- **Static WebP support**: support for static `.webp` images as key and background assets.
- **Accurate GIF timing**: animated GIFs play at their native frame delays.
- **Playback pacing**: animated backgrounds play at their configured or native frame rate.
- **Smooth animated decks**: a frame pipeline decodes and resizes animated GIFs once per frame, caches each key's layers, and re-renders only what actually changed.
- **The deck switches off at shutdown**: when the system shuts down or you log out, the deck's screens go black (brightness 0) while USB power stays on, so it does not stay lit while the machine is off.

## Tested on

- Arch Linux
- Fedora 44
- One Stream Deck model: the 5×3 grid

And that's the full list. Upstream StreamController is Linux-only (GTK4/udev),
so Windows and macOS aren't a "might work if you patch it" situation. They're not supported at all.
Within Linux, I have not tested other distributions, desktop environments, or Stream Deck models (Mini, XL, Pedal, and so on), and I have no idea how they will behave, including whether a model with more buttons has enough headroom for layered backgrounds.

## Not included / not planned

- No packaging, binaries, or installers.
- No CI guarantees and no release channel.
- No plans to upstream these changes. My personal requirements are too specific to my setup to be worth a pull request.

## Installation

There is no installer or release build. To run this:

1. Clone the repository and build it.
2. Expect to patch things for your own distribution, desktop environment, and hardware. Paths, dependencies, and Stream Deck model handling have moved on upstream since this was forked, and this fork is not kept in sync.
