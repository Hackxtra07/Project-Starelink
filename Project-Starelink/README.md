# 📁 Project-Starelink (Link Storage Pro & Downloader)

A feature-rich, high-performance desktop application designed for bookmark management, integrated web browsing, and multi-threaded video/audio downloading. Built with Python and PyQt5.

---

## 🚀 Key Features

### 1. 📁 Link Manager
* **Structured Storage:** Organize bookmarks locally using a fast, lightweight SQLite database.
* **Tagging & Filtering:** Categorize links with tags and instantly filter or search your collection.
* **Auto-generated Thumbnails:** Automatically fetches page thumbnails (via `yt-dlp` info or `og:image` metadata scraping) in the background.
* **Import/Export:** Seamlessly import/export links from JSON or TXT file backups.
* **Deduplication:** Find and remove duplicate URLs with a single click.

### 2. 🌐 Integrated Browser
* **Multi-Tab Interface:** Surf the web inside the app using a tabbed browser powered by `QWebEngineView`.
* **Keyboard Shortcuts:** Fast navigation using industry-standard hotkeys (e.g., `Ctrl+T` for new tab, `Ctrl+W` to close tab, `Ctrl+L` to focus address bar, `Ctrl+S` to bookmark the current page).
* **Rich Media Support:** Native handling of feature permissions (camera, microphone, geolocation) and HTML5 fullscreen media playback.

### 3. 🎥 Multi-Threaded Downloader
* **Powered by `yt-dlp` & `aria2c`:** Enjoy ultra-fast parallel fragment downloads (up to 10x speedup with `aria2c`).
* **Format Selector:** Fetch all available resolutions, sizes, and formats for any video URL before starting a download.
* **Playlist Downloader:** Load full video playlists, select specific ranges, and download in batches.
* **Download Queue & History:** Monitor active downloads, manage queue order, and keep a history log of all past downloads.
* **Audio Extraction:** Convert videos to high-quality audio files (`mp3`, `m4a`, `flac`, etc.) on the fly.

---

## 🛠️ Installation & Setup

### Prerequisites
Make sure you have **Python 3.8+** installed on your system.

### Dependencies
Install the required Python packages:
```bash
pip install PyQt5 PyQtWebEngine yt-dlp
```

For fast parallel downloading, it is highly recommended to install `aria2c` on your system:
* **Linux (Debian/Ubuntu):** `sudo apt install aria2`
* **macOS:** `brew install aria2`
* **Windows:** Install via chocolatey: `choco install aria` or download from official sources and add it to your PATH.

---

## 💻 How to Run

Launch the application using Python:
```bash
python main.py
```

---

## 🎹 Keyboard Shortcuts

| Shortcut | Action |
| --- | --- |
| `Ctrl + N` | Add new bookmark |
| `Ctrl + F` | Focus search bar in Links tab |
| `Ctrl + D` | Download selected link with video downloader |
| `Ctrl + T` | Open new browser tab |
| `Ctrl + W` | Close current browser tab |
| `Ctrl + L` | Focus browser address bar |
| `Ctrl + R` / `F5` | Reload current page |
| `Ctrl + S` | Bookmark current page to Link Manager |

---

## ⚙️ Configuration
Settings are saved locally in:
* `downloader_config.json` - Custom download folder, concurrency limits, format preferences, audio settings, etc.
* `download_history.json` - History of completed and failed downloads.
* `links.db` - SQLite database containing all saved bookmarks.
