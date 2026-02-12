# Dispatcharr Too Many Streams Plugin (Overhaul Edition)

This is a high-performance, optimized fork of the original "Too Many Streams" plugin for Dispatcharr. It enhances the user experience when a stream limit is reached by displaying a beautiful, dynamic splash screen instead of a generic error.

## Key Optimizations & Features

### 🚀 High-Performance Image Generation
Replaced the heavy `wkhtmltoimage` dependency with the native **Pillow** library. 
- **Near-Instant Generation:** Images are created in milliseconds without launching a hidden web browser.
- **Low Resource Usage:** Uses a fraction of the RAM and CPU compared to the original version.
- **Beautiful Dark Theme:** Features a modern, polished dark slate theme with rounded channel cards and cyan accent pills.

### 📡 Scalable Video Streaming
Optimized the FFmpeg implementation to use a **Broadcaster/Subscriber** model.
- **Single Process:** Only one FFmpeg process runs at a time, regardless of how many users are watching the "Too Many Streams" screen.
- **Zero-Idle CPU:** The broadcaster automatically pauses FFmpeg when no one is watching, resulting in near-zero CPU usage during idle periods.
- **1 FPS Encoding:** Uses a highly efficient 1 frame-per-second stream to minimize network bandwidth.

### 🧠 Robust State Management
Migrated all state handling to **Redis**.
- **Atomic Operations:** Prevents race conditions when multiple users hit stream limits simultaneously.
- **No Disk I/O:** Eliminates the need for slow "pickle" files, making the plugin much faster and more reliable in containerized environments.

### ⚙️ Modern Configuration & Persistence
- **Robust Dataclasses:** Uses Python's native dataclass system for reliable, lightweight configuration management.
- **Persistent Storage:** Your settings (Title, Description, Columns) are saved to `/data/plugins/TMS_Persistent_Config/` and will survive plugin updates.
- **Dynamic UI:** The settings page in Dispatcharr stays perfectly in sync with your persistent file.

## Features
- **Dynamic Splash Screen:** Shows a grid of other currently active streams that the user can watch.
- **Automatic Fallback:** Displays a clean "This Channel is Unavailable" message if no other streams are active.
- **Fully Configurable:** Customize the Title, Description, and the number of columns in the grid directly from the UI.

## Installation (Dispatcharr v0.19+)

1.  **Prepare the Zip:** Run the following command in the plugin directory:
    ```bash
    tar -a -c -f TooManyStreams.zip plugin.py plugin.json __init__.py src img LICENSE README.md
    ```
2.  **Install Dependencies:** Ensure the following packages are in your Dispatcharr environment:
    ```bash
    pip install Pillow
    ```
3.  **Upload:** Use the Dispatcharr web UI to upload and install the `TooManyStreams.zip` file.
4.  **Configure:** Go to the plugin settings page to set your preferred title and column count.

## Configuration

### UI Settings
| Setting | Default | Description |
|---------|---------|-------------|
| **Stream Title** | "Sorry, this channel is unavailable." | The main headline on the splash screen. |
| **Stream Description** | "While this channel is not currently available..." | Sub-text displayed below the title. |
| **Number of Columns** | `5` | How many channel cards to show side-by-side in the grid. |
| **Log Level** | `INFO` | Verbosity of the plugin logs. |

### Environment Variables
| Variable | Default | Description |
|----------|---------|-------------|
| `TMS_HOST` | `0.0.0.0` | Host for the internal HTTP server. |
| `TMS_PORT` | `1337` | TCP port for the internal HTTP server. |
| `TMS_IMAGE_PATH` | *(none)* | Path to a static image (bypasses dynamic generation). |

## Development
This overhaul focuses on efficiency and production stability. Feel free to fork and contribute to further improve the layout or performance.

---
*Maintained with ❤️ for the Dispatcharr community.*
