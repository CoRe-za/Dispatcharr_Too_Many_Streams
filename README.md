# Too Many Streams for Dispatcharr

Too Many Streams replaces Dispatcharr's provider-capacity error with a
low-bandwidth MPEG-TS fallback screen. The screen can show other channels that
are currently active and can be styled from the Dispatcharr plugin settings.

## Compatibility

- Plugin version: **4.0.1**
- Supported and tested Dispatcharr version: **v0.29.0**
- FFmpeg must be available in the Dispatcharr container.
- Pillow is declared in `requirements.txt` and is also included in the standard
  Dispatcharr v0.29.0 image.

Version 4 uses Dispatcharr v0.29's native live-proxy URL resolver. Dispatcharr
continues to own connection pools, provider limits, requester priority, stale
assignments, and cleanup. The plugin substitutes its fallback URL only when
Dispatcharr reports that every compatible provider profile has reached its
connection limit.

## Features

- Automatic fallback when provider capacity is exhausted.
- A configurable 1920×1080 screen generated with Pillow.
- A grid of other channels currently active in Dispatcharr.
- Custom title, description, layout, and theme colors.
- CPU and hardware FFmpeg encoder options.
- One shared 1 FPS FFmpeg process for all fallback viewers.
- A manual action to add the TooManyStreams source at the bottom of every
  channel for workflows that require an explicit source.
- Safe cleanup when the plugin is disabled or reloaded.
- Multi-worker handling: one worker owns port 1337 while every worker installs
  the resolver integration it needs.

## Installation

1. Download [`TooManyStreams.zip`](./TooManyStreams.zip).
2. Open Dispatcharr's **Plugins** page.
3. Import the ZIP and enable **Too Many Streams**.
4. Configure the appearance and FFmpeg encoder if desired.

The ZIP contains the required `too_many_streams/plugin.py`, `plugin.json`, and
package `__init__.py` structure expected by Dispatcharr v0.29.0.

When upgrading from a release earlier than v4:

1. Install and enable v4.0.1.
2. Run **Clean legacy assignments** once to remove obsolete channel-stream
   links created by older releases.
3. Restart Dispatcharr if an old plugin worker remains loaded.

The v4 fallback operates automatically while the plugin is enabled. You do not
need to add the source to every channel. The **Add TooManyStreams source to all
channels** action remains available when you explicitly want the custom source
listed at order `9999` on every channel.

## How it works

1. Dispatcharr performs its normal stream selection and connection reservation.
2. If a provider stream is available, the plugin changes nothing.
3. If all compatible provider profiles are at capacity, the plugin returns its
   local MPEG-TS URL using Dispatcharr's built-in Proxy stream profile.
4. The local server generates the fallback screen and FFmpeg encodes it at
   1 FPS.
5. One broadcaster supplies the same fallback stream to all viewers.

Active channels are read from Dispatcharr v0.29.0's
`live:channel:*:metadata` Redis hashes. The fallback stream is excluded from
the channel grid.

## Actions

| Action | Purpose |
|---|---|
| Add TooManyStreams source to all channels | Adds the shared custom source at order `9999` on every channel. |
| Remove TooManyStreams source from all channels | Removes explicit assignments; automatic v4 fallback remains active. |
| Clean legacy assignments | Removes obsolete assignments made by pre-v4 versions. |
| Save Plugin Config | Persists the current settings under `/data/plugins`. |
| Search for Persistent Config | Reloads the saved configuration from disk. |

## Settings

| Setting | Default | Purpose |
|---|---|---|
| Stream Title | `Sorry, this channel is unavailable.` | Main heading |
| Stream Description | Availability explanation | Supporting text |
| Number of channel columns | `5` | Grid width |
| Static Image Path | Empty | Uses a fixed image instead of the generated grid |
| Log Level | `INFO` | Plugin logging verbosity |
| Video Encoder | `libx264` | FFmpeg encoder, including supported hardware encoders |
| Theme colors | Dark theme | Background, cards, text, borders, and accents |

Supported encoder values include `libx264`, `h264_nvenc`, `h264_qsv`,
`h264_omx`, and `h264_videotoolbox`. Use `libx264` when unsure.

Environment overrides:

| Variable | Default |
|---|---|
| `TMS_HOST` | `0.0.0.0` |
| `TMS_PORT` | `1337` |
| `TMS_LOG_LEVEL` | `INFO` |
| `TMS_IMAGE_PATH` | Empty |
| `TMS_LOGO_CACHE_DIR` | `/tmp/tms_logos` |

Persistent settings are stored at
`/data/plugins/TMS_Persistent_Config/too_many_streams_persistent_config.json`.

## Development and verification

The v0.29 integration contract can be tested without a running Dispatcharr
instance:

```bash
python -m unittest discover -s tests -v
```

The tests verify that normal stream selections remain unchanged, provider
capacity errors switch to the fallback URL, the Proxy profile is selected only
for that request, and disabling the plugin restores Dispatcharr's original
methods.

## Notes

- The plugin hooks an internal Dispatcharr resolver because Dispatcharr does
  not currently expose a public capacity-fallback extension point. A future
  Dispatcharr resolver change may therefore require a compatibility update.
- A configured static image must be readable inside the Dispatcharr container.
- The fallback listener is internal by default at
  `http://127.0.0.1:1337/stream.ts`; no additional Unraid port mapping is
  normally required.

Original plugin by [JamesWRC](https://github.com/JamesWRC/Dispatcharr_Too_Many_Streams).
Version 4 compatibility and reliability work by contributors.
