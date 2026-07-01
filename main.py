import os
import sys
import json
import sqlite3
import urllib.request
import urllib.parse
import re
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(SCRIPT_DIR, "links.db")
THUMBNAILS_DIR = os.path.join(SCRIPT_DIR, "thumbnails")

# Suppress Chromium terminal warnings and errors (like the media pipeline errors)
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-logging --log-level=3"
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLineEdit, QTextEdit,
    QLabel, QComboBox, QMessageBox, QInputDialog, QFileDialog, QSplitter,
    QTabWidget, QMenu, QAction, QGridLayout, QCheckBox, QShortcut
)
from PyQt5.QtCore import Qt, QUrl, QDateTime, QThread, pyqtSignal, QSize
from PyQt5.QtGui import QIcon, QFont, QKeySequence, QPixmap, QImage

from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineProfile, QWebEngineSettings, QWebEnginePage, QWebEngineDownloadItem
from downloader import DownloaderWidget

try:
    from PIL import Image as PILImage
    import io as _io
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


def load_pixmap(path, width=200, height=150):
    """Load an image via Pillow → in-memory PNG → QPixmap.
    Qt's PNG decoder is built-in (no plugin needed), so this is
    reliable even when the JPEG Qt plugin is missing.
    Falls back to a plain white QPixmap on any error."""
    if _PIL_AVAILABLE and os.path.exists(path):
        try:
            img = PILImage.open(path).convert("RGB")
            img = img.resize((width, height), PILImage.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue(), "PNG")
            if not px.isNull():
                return px
        except Exception as _e:
            print(f"[load_pixmap] {path}: {_e}")
    # Fallback: solid white placeholder
    px = QPixmap(width, height)
    px.fill(Qt.white)
    return px


TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    tags TEXT,
    notes TEXT,
    visit_count INTEGER DEFAULT 0,
    last_visited DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    cast TEXT
)
"""


class LinkManager:
    """Handles database operations."""
    def __init__(self, db_path=DB_NAME):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self._init_db()

    def _init_db(self):
        self.cursor.execute(TABLE_SCHEMA)
        # Safe migration: add cast column if it doesn't exist yet
        try:
            self.cursor.execute("ALTER TABLE links ADD COLUMN cast TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        self.conn.commit()

    def add_link(self, title, url, tags="", notes="", cast=""):
        self.cursor.execute(
            "INSERT INTO links (title, url, tags, notes, cast) VALUES (?, ?, ?, ?, ?)",
            (title, url, tags, notes, cast)
        )
        self.conn.commit()
        return self.cursor.lastrowid

    def update_link(self, link_id, title, url, tags, notes, cast=""):
        self.cursor.execute(
            """UPDATE links SET title=?, url=?, tags=?, notes=?, cast=?
               WHERE id=?""",
            (title, url, tags, notes, cast, link_id)
        )
        self.conn.commit()

    def update_metadata(self, link_id, tags, cast):
        """Silently update tags and cast extracted in the background."""
        # Only fill in fields that are currently empty
        self.cursor.execute("SELECT tags, cast FROM links WHERE id=?", (link_id,))
        row = self.cursor.fetchone()
        if not row:
            return
        new_tags = row[0] if row[0] else tags
        new_cast = row[1] if row[1] else cast
        self.cursor.execute(
            "UPDATE links SET tags=?, cast=? WHERE id=?",
            (new_tags, new_cast, link_id)
        )
        self.conn.commit()

    def delete_link(self, link_id):
        self.cursor.execute("DELETE FROM links WHERE id=?", (link_id,))
        self.conn.commit()

    def get_all_links(self, search_term="", tag_filter="", search_field="All Fields"):
        query = "SELECT * FROM links WHERE 1=1"
        params = []
        if search_term:
            like = f"%{search_term}%"
            if search_field == "Title":
                query += " AND title LIKE ?"
                params.append(like)
            elif search_field == "URL":
                query += " AND url LIKE ?"
                params.append(like)
            elif search_field == "Tags":
                query += " AND tags LIKE ?"
                params.append(like)
            elif search_field == "Cast":
                query += " AND cast LIKE ?"
                params.append(like)
            else: # All Fields
                query += " AND (title LIKE ? OR url LIKE ? OR tags LIKE ? OR cast LIKE ?)"
                params.extend([like, like, like, like])
        if tag_filter:
            query += " AND tags LIKE ?"
            params.append(f"%{tag_filter}%")
        query += " ORDER BY created_at DESC"
        self.cursor.execute(query, params)
        return self.cursor.fetchall()

    def get_link(self, link_id):
        self.cursor.execute("SELECT * FROM links WHERE id=?", (link_id,))
        return self.cursor.fetchone()

    def record_visit(self, link_id):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "UPDATE links SET visit_count = visit_count + 1, last_visited = ? WHERE id=?",
            (now, link_id)
        )
        self.conn.commit()

    def get_all_tags(self):
        # Returns a set of all unique tags from all links
        self.cursor.execute("SELECT tags FROM links")
        rows = self.cursor.fetchall()
        tags = set()
        for row in rows:
            if row[0]:
                for t in row[0].split(','):
                    tags.add(t.strip())
        return sorted(tags)

    def close(self):
        self.conn.close()


class ThumbnailFetcher(QThread):
    finished = pyqtSignal(int, str)  # link_id, local_path

    # A full browser User-Agent that passes most server-side checks
    USER_AGENT = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )

    def __init__(self, link_id, url):
        super().__init__()
        self.link_id = link_id
        self.url = url
        self.thumbnails_dir = THUMBNAILS_DIR

    def run(self):
        os.makedirs(self.thumbnails_dir, exist_ok=True)

        local_path = os.path.join(self.thumbnails_dir, f"{self.link_id}.jpg")
        if os.path.exists(local_path):
            self.finished.emit(self.link_id, local_path)
            return

        url_to_fetch = self.url
        if not url_to_fetch.startswith("http"):
            url_to_fetch = "http://" + url_to_fetch

        img_url = None

        # ── Strategy 1: yt-dlp (best for YouTube / Vimeo / Twitter / etc.) ──
        try:
            import yt_dlp
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'extract_flat': False,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url_to_fetch, download=False)
                img_url = info.get('thumbnail')
        except Exception as e:
            print(f"[ThumbnailFetcher] yt-dlp failed for {url_to_fetch}: {e}")

        # ── Strategy 2: og:image scraping with a real browser User-Agent ──
        if not img_url:
            try:
                headers = {
                    'User-Agent': self.USER_AGENT,
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate',
                    'Connection': 'keep-alive',
                }
                req = urllib.request.Request(url_to_fetch, headers=headers)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    # Handle gzip if server sends it
                    raw = resp.read()
                    try:
                        import gzip
                        html = gzip.decompress(raw).decode('utf-8', errors='ignore')
                    except Exception:
                        html = raw.decode('utf-8', errors='ignore')

                # Match og:image in any attribute order, with or without quotes
                patterns = [
                    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                    r'<meta[^>]+property=["\']og:image["\'][^>]+content=([^\s>]+)',
                    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
                ]
                for pat in patterns:
                    m = re.search(pat, html, re.IGNORECASE)
                    if m:
                        img_url = m.group(1).strip()
                        break

            except Exception as e:
                print(f"[ThumbnailFetcher] og:image scrape failed for {url_to_fetch}: {e}")

        # ── Download the image if we found a URL ──
        if img_url:
            try:
                if img_url.startswith("//"):
                    img_url = "https:" + img_url
                elif img_url.startswith("/"):
                    parsed = urllib.parse.urlparse(url_to_fetch)
                    img_url = f"{parsed.scheme}://{parsed.netloc}{img_url}"

                req_img = urllib.request.Request(img_url, headers={'User-Agent': self.USER_AGENT})
                img_data = urllib.request.urlopen(req_img, timeout=8).read()

                if img_data and len(img_data) > 100:   # sanity check — not empty
                    with open(local_path, 'wb') as f:
                        f.write(img_data)
                    self.finished.emit(self.link_id, local_path)
                else:
                    print(f"[ThumbnailFetcher] Downloaded image too small, skipping: {img_url}")
            except Exception as e:
                print(f"[ThumbnailFetcher] Image download failed: {e}")
        else:
            print(f"[ThumbnailFetcher] No thumbnail found for: {url_to_fetch}")




class MetadataFetcher(QThread):
    """Background thread: extracts tags and cast/actor names from a URL.
    Uses yt-dlp for video sites and HTML scraping for everything else."""
    metadata_ready = pyqtSignal(int, str, str)  # link_id, tags_csv, cast_csv

    USER_AGENT = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )

    def __init__(self, link_id, url):
        super().__init__()
        self.link_id = link_id
        self.url = url

    def _scrape_xhamster(self, html, url):
        """xHamster-specific scraper: extracts tags and pornstar/model names."""
        tags, cast = [], []

        # ── JSON-LD (most reliable, xHamster includes schema.org/VideoObject) ──
        for m in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.I | re.S
        ):
            try:
                data = json.loads(m.group(1))
                items = data if isinstance(data, list) else [data]
                for item in items:
                    # Tags from keywords
                    kw = item.get('keywords', '')
                    if isinstance(kw, str) and kw:
                        for t in kw.split(','):
                            t = t.strip()
                            if t and t not in tags:
                                tags.append(t)
                    elif isinstance(kw, list):
                        for t in kw:
                            t = str(t).strip()
                            if t and t not in tags:
                                tags.append(t)
                    # Cast from actor
                    actors = item.get('actor', [])
                    if isinstance(actors, dict):
                        actors = [actors]
                    for actor in actors:
                        name = actor.get('name') if isinstance(actor, dict) else str(actor)
                        if name and name not in cast:
                            cast.append(name)
            except Exception:
                pass

        # ── Tag links: /tags/xxx or /categories/xxx ──
        if not tags:
            for m in re.finditer(
                r'href=["\'](?:https?://[^"\'/]*)?/(?:tags?|categories?)/[^"\'>]+["\'][^>]*>([^<]{1,60})</a>',
                html, re.I
            ):
                val = m.group(1).strip()
                if val and val.lower() not in ('next', 'prev', 'more', 'all') and val not in tags:
                    tags.append(val)

        # ── Pornstar / model links: /pornstars/xxx or /models/xxx ──
        if not cast:
            for m in re.finditer(
                r'href=["\'](?:https?://[^"\'/]*)?/(?:pornstars?|models?)/[^"\'>]+["\'][^>]*>([^<]{1,80})</a>',
                html, re.I
            ):
                name = m.group(1).strip()
                if name and name not in cast:
                    cast.append(name)

        # ── Fallback: data-tag / data-model attributes ──
        if not tags:
            for m in re.finditer(r'data-(?:tag|category)=["\']([^"\']{1,60})["\']', html, re.I):
                val = m.group(1).strip()
                if val and val not in tags:
                    tags.append(val)
        if not cast:
            for m in re.finditer(r'data-(?:model|pornstar)=["\']([^"\']{1,80})["\']', html, re.I):
                val = m.group(1).strip()
                if val and val not in cast:
                    cast.append(val)

        return tags[:15], cast[:10]

    def run(self):
        tags, cast = [], []
        url = self.url if self.url.startswith("http") else "http://" + self.url
        is_xhamster = 'xhamster' in url.lower()

        # ── Strategy 1: yt-dlp (video sites) ──
        try:
            import yt_dlp
            with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True,
                                    'skip_download': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                if info.get('tags'):
                    tags = [t.strip() for t in info['tags'] if t.strip()][:12]
                for key in ('artist', 'creator', 'uploader', 'channel'):
                    val = info.get(key, '')
                    if val and val not in cast:
                        cast.append(val)
        except Exception:
            pass

        # ── Strategy 2: HTML scraping ──
        if not tags or not cast:
            try:
                import gzip
                headers = {
                    'User-Agent': self.USER_AGENT,
                    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate',
                    'Connection': 'keep-alive',
                }
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read()
                try:
                    html = gzip.decompress(raw).decode('utf-8', errors='ignore')
                except Exception:
                    html = raw.decode('utf-8', errors='ignore')

                # ── xHamster-specific parsing (priority) ──
                if is_xhamster:
                    xh_tags, xh_cast = self._scrape_xhamster(html, url)
                    if xh_tags:
                        tags = xh_tags
                    if xh_cast:
                        cast = xh_cast

                # ── Generic: meta keywords ──
                if not tags:
                    m = re.search(
                        r'<meta[^>]+name=["\']keywords["\'][^>]+content=["\']([^"\']{1,500})["\']',
                        html, re.I)
                    if not m:
                        m = re.search(
                            r'<meta[^>]+content=["\']([^"\']{1,500})["\'][^>]+name=["\']keywords["\']',
                            html, re.I)
                    if m:
                        tags = [t.strip() for t in m.group(1).split(',') if t.strip()][:12]

                # ── Generic: article:tag / og:video:tag ──
                for pat in [
                    r'<meta[^>]+property=["\'](?:article|og):(?:tag|video:tag)["\'][^>]+content=["\']([^"\']+)["\']',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\'](?:article|og):(?:tag|video:tag)["\']',
                ]:
                    for m in re.finditer(pat, html, re.I):
                        val = m.group(1).strip()
                        if val and val not in tags:
                            tags.append(val)

                # ── Generic: JSON-LD schema.org actor ──
                if not cast:
                    for m in re.finditer(
                        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                        html, re.I | re.S
                    ):
                        try:
                            data = json.loads(m.group(1))
                            items = data if isinstance(data, list) else [data]
                            for item in items:
                                actors = item.get('actor', [])
                                if isinstance(actors, dict):
                                    actors = [actors]
                                for actor in actors:
                                    name = actor.get('name') if isinstance(actor, dict) else str(actor)
                                    if name and name not in cast:
                                        cast.append(name)
                        except Exception:
                            pass

                # ── Generic: og:video:actor ──
                if not cast:
                    for m in re.finditer(
                        r'<meta[^>]+property=["\']og:video:actor["\'][^>]+content=["\']([^"\']+)["\']',
                        html, re.I
                    ):
                        val = m.group(1).strip()
                        if val and val not in cast:
                            cast.append(val)

            except Exception as e:
                print(f"[MetadataFetcher] HTML scraping failed for {url}: {e}")

        self.metadata_ready.emit(
            self.link_id,
            ", ".join(tags[:12]),
            ", ".join(cast[:8])
        )


class CustomWebEngineView(QWebEngineView):
    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main_window = main_window

    def createWindow(self, type):
        # Handle popups and links that open in a new tab
        view = self.main_window.add_browser_tab(label="Popup")
        return view


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db = LinkManager()

        # Optimize browser settings for speed and compatibility
        settings = QWebEngineSettings.globalSettings()
        settings.setAttribute(QWebEngineSettings.PluginsEnabled, True)
        settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebGLEnabled, True)
        settings.setAttribute(QWebEngineSettings.Accelerated2dCanvasEnabled, True)
        settings.setAttribute(QWebEngineSettings.ScrollAnimatorEnabled, True)
        
        # Enable disk caching to improve load speed
        profile = QWebEngineProfile.defaultProfile()
        profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
        profile.setHttpCacheMaximumSize(500 * 1024 * 1024) # 500 MB
        
        # Handle Downloads
        profile.downloadRequested.connect(self.on_download_requested)
        
        self.current_link_id = None
        self.current_search = ""
        self.current_tag_filter = ""

        self.setWindowTitle("Link Storage Pro")
        self.setGeometry(100, 100, 1200, 700)

        # Central widget with tabs: Links list + Browser
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # ------------------- Tab 1: Links Manager -------------------
        self.manager_widget = QWidget()
        self.manager_layout = QVBoxLayout(self.manager_widget)

        # Top controls: search, tag filter, add button
        controls = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search links...")
        self.search_input.textChanged.connect(self.on_search_changed)

        self.search_field_combo = QComboBox()
        self.search_field_combo.addItems(["All Fields", "Title", "URL", "Tags", "Cast"])
        self.search_field_combo.currentTextChanged.connect(lambda: self.refresh_list())

        controls.addWidget(QLabel("Search:"))
        controls.addWidget(self.search_input)
        controls.addWidget(self.search_field_combo)

        self.tag_combo = QComboBox()
        self.tag_combo.addItem("All Tags")
        self.tag_combo.currentTextChanged.connect(self.on_tag_filter_changed)
        controls.addWidget(QLabel("Tag:"))
        controls.addWidget(self.tag_combo)

        controls.addStretch()
        add_btn = QPushButton("➕ Add Link")
        add_btn.clicked.connect(self.add_link_dialog)
        controls.addWidget(add_btn)

        dedup_btn = QPushButton("🧹 Remove Duplicates")
        dedup_btn.setToolTip("Find and delete all duplicate URLs, keeping the oldest copy")
        dedup_btn.clicked.connect(self.remove_duplicate_links)
        controls.addWidget(dedup_btn)


        self.manager_layout.addLayout(controls)

        # Splitter: list on left, details on right
        splitter = QSplitter(Qt.Horizontal)
        self.list_widget = QListWidget()
        self.list_widget.setIconSize(QSize(100, 75))
        self.list_widget.itemClicked.connect(self.on_link_selected)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self.show_context_menu)
        splitter.addWidget(self.list_widget)

        # Right panel: details and actions
        detail_widget = QWidget()
        detail_layout = QGridLayout(detail_widget)

        # Labels and fields
        detail_layout.addWidget(QLabel("Title:"), 0, 0)
        self.title_edit = QLineEdit()
        detail_layout.addWidget(self.title_edit, 0, 1)

        detail_layout.addWidget(QLabel("URL:"), 1, 0)
        self.url_edit = QLineEdit()
        detail_layout.addWidget(self.url_edit, 1, 1)

        detail_layout.addWidget(QLabel("Tags:"), 2, 0)
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("comma separated — auto-filled from URL")
        detail_layout.addWidget(self.tags_edit, 2, 1)

        detail_layout.addWidget(QLabel("Cast / Creator:"), 3, 0)
        self.cast_edit = QLineEdit()
        self.cast_edit.setPlaceholderText("auto-extracted from URL")
        detail_layout.addWidget(self.cast_edit, 3, 1)

        # Fetch Tags & Cast button row
        fetch_meta_layout = QHBoxLayout()
        self.fetch_meta_btn = QPushButton("🔄 Fetch Tags & Cast")
        self.fetch_meta_btn.setToolTip(
            "Scrape tags and cast/models from the link URL.\n"
            "Works for xHamster, YouTube, and most sites."
        )
        self.fetch_meta_btn.clicked.connect(self.fetch_tags_cast_now)
        self.fetch_meta_btn.setStyleSheet("font-weight: bold; color: #0057b7;")
        fetch_meta_layout.addWidget(self.fetch_meta_btn)
        fetch_meta_layout.addStretch()
        detail_layout.addLayout(fetch_meta_layout, 4, 0, 1, 2)

        detail_layout.addWidget(QLabel("Notes:"), 5, 0)
        self.notes_edit = QTextEdit()
        self.notes_edit.setMaximumHeight(80)
        detail_layout.addWidget(self.notes_edit, 5, 1)

        # Thumbnail Preview
        detail_layout.addWidget(QLabel("Thumbnail:"), 6, 0)
        self.thumbnail_preview = QLabel("")
        self.thumbnail_preview.setAlignment(Qt.AlignCenter)
        self.thumbnail_preview.setFixedSize(200, 150)
        self.thumbnail_preview.setStyleSheet("border: 1px solid #ccc; background-color: #ffffff; border-radius: 4px;")
        detail_layout.addWidget(self.thumbnail_preview, 6, 1)

        # Stats
        self.stats_label = QLabel("")
        detail_layout.addWidget(self.stats_label, 7, 0, 1, 2)

        # Buttons
        btn_layout = QHBoxLayout()
        self.save_btn = QPushButton("💾 Save")
        self.save_btn.clicked.connect(self.save_link)
        btn_layout.addWidget(self.save_btn)
        self.delete_btn = QPushButton("🗑️ Delete")
        self.delete_btn.clicked.connect(self.delete_link)
        btn_layout.addWidget(self.delete_btn)
        self.open_internal_btn = QPushButton("🌐 Open in Browser Tab")
        self.open_internal_btn.clicked.connect(self.open_internal)
        btn_layout.addWidget(self.open_internal_btn)
        self.open_external_btn = QPushButton("🌍 Open External")
        self.open_external_btn.clicked.connect(self.open_external)
        btn_layout.addWidget(self.open_external_btn)
        self.download_link_btn = QPushButton("⬇️ Download with Downloader")
        self.download_link_btn.setToolTip("Download this link's video using the built-in Downloader (Ctrl+D)")
        self.download_link_btn.clicked.connect(self.download_selected_link)
        self.download_link_btn.setStyleSheet("font-weight: bold;")
        btn_layout.addWidget(self.download_link_btn)
        detail_layout.addLayout(btn_layout, 8, 0, 1, 2)

        # Import/Export buttons
        imp_exp_layout = QHBoxLayout()
        import_btn = QPushButton("📥 Import JSON")
        import_btn.clicked.connect(self.import_json)
        imp_exp_layout.addWidget(import_btn)
        import_txt_btn = QPushButton("📄 Import TXT")
        import_txt_btn.clicked.connect(self.import_txt)
        imp_exp_layout.addWidget(import_txt_btn)
        export_btn = QPushButton("📤 Export JSON")
        export_btn.clicked.connect(self.export_json)
        imp_exp_layout.addWidget(export_btn)
        detail_layout.addLayout(imp_exp_layout, 9, 0, 1, 2)

        splitter.addWidget(detail_widget)
        splitter.setSizes([400, 800])
        self.manager_layout.addWidget(splitter)

        # Add manager tab
        self.tabs.addTab(self.manager_widget, "📁 Links")

        # ------------------- Tab 2: Browser -------------------
        self.browser_widget = QWidget()
        browser_layout = QVBoxLayout(self.browser_widget)
        browser_nav = QHBoxLayout()
        
        self.back_btn = QPushButton("⬅️")
        self.back_btn.clicked.connect(self.browser_back)
        browser_nav.addWidget(self.back_btn)
        
        self.forward_btn = QPushButton("➡️")
        self.forward_btn.clicked.connect(self.browser_forward)
        browser_nav.addWidget(self.forward_btn)
        
        self.reload_btn = QPushButton("🔄")
        self.reload_btn.clicked.connect(self.browser_reload)
        browser_nav.addWidget(self.reload_btn)

        self.url_bar = QLineEdit()
        self.url_bar.returnPressed.connect(self.navigate_to_url)
        browser_nav.addWidget(QLabel("Address:"))
        browser_nav.addWidget(self.url_bar)
        
        go_btn = QPushButton("Go")
        go_btn.clicked.connect(self.navigate_to_url)
        browser_nav.addWidget(go_btn)
        
        add_tab_btn = QPushButton("➕")
        add_tab_btn.clicked.connect(lambda: self.add_browser_tab(QUrl("https://www.google.com/"), "New Tab"))
        browser_nav.addWidget(add_tab_btn)
        
        download_shortcut_btn = QPushButton("⬇️")
        download_shortcut_btn.setToolTip("Download video from this page with MudMoovie")
        download_shortcut_btn.clicked.connect(self.download_current_page_video)
        browser_nav.addWidget(download_shortcut_btn)

        save_page_btn = QPushButton("🔖")
        save_page_btn.setToolTip("Save current page to Links (Ctrl+S)")
        save_page_btn.setStyleSheet("font-weight: bold; color: #4CAF50;")
        save_page_btn.clicked.connect(self.save_current_page)
        browser_nav.addWidget(save_page_btn)

        
        browser_layout.addLayout(browser_nav)


        self.browser_tabs = QTabWidget()
        self.browser_tabs.setDocumentMode(True)
        self.browser_tabs.setTabsClosable(True)
        self.browser_tabs.tabBar().setElideMode(Qt.ElideRight)
        self.browser_tabs.setStyleSheet("""
            QTabBar::tab {
                max-width: 150px;
                min-width: 40px;
                padding: 4px 8px;
            }
        """)
        self.browser_tabs.tabCloseRequested.connect(self.close_browser_tab)
        self.browser_tabs.currentChanged.connect(self.on_browser_tab_changed)
        browser_layout.addWidget(self.browser_tabs)
        
        self.tabs.addTab(self.browser_widget, "🌐 Browser")

        # ------------------- Tab 3: Downloader -------------------
        self.downloader_widget = DownloaderWidget(self)
        self.tabs.addTab(self.downloader_widget, "🎥 Downloader")


        # Create the initial default tab
        self.add_browser_tab(QUrl("https://www.google.com/"), "New Tab")

        # Load links and populate tags
        self.refresh_list()

        # ------------------- Keyboard Shortcuts -------------------
        # Ctrl+D  => Download selected link with Downloader
        QShortcut(QKeySequence("Ctrl+D"), self).activated.connect(self.download_selected_link)
        # Ctrl+S  => Save current browser page to Links
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self.save_current_page)
        # Ctrl+T  => Open a new browser tab
        QShortcut(QKeySequence("Ctrl+T"), self).activated.connect(
            lambda: self.add_browser_tab(QUrl("https://www.google.com/"), "New Tab")
        )
        # Ctrl+L  => Focus address bar
        QShortcut(QKeySequence("Ctrl+L"), self).activated.connect(self.focus_address_bar)
        # Ctrl+R  => Reload current browser tab
        QShortcut(QKeySequence("Ctrl+R"), self).activated.connect(self.browser_reload)
        # Ctrl+W  => Close current browser tab
        QShortcut(QKeySequence("Ctrl+W"), self).activated.connect(self.close_current_browser_tab)
        # Ctrl+F  => Focus search box in Links tab
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(self.focus_search)
        # Ctrl+N  => Add new link dialog
        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(self.add_link_dialog)
        # F5      => Reload browser
        QShortcut(QKeySequence("F5"), self).activated.connect(self.browser_reload)



    # ---------- Link list operations ----------
    def refresh_list(self):
        """Refresh the link list based on current search/tag filter."""
        self.list_widget.clear()
        search_field = self.search_field_combo.currentText() if hasattr(self, 'search_field_combo') else "All Fields"
        links = self.db.get_all_links(self.current_search, self.current_tag_filter, search_field)
        # NOTE: keep fetchers on self so threads are never GC'd mid-download
        if not hasattr(self, 'fetchers'):
            self.fetchers = []
        # Remove finished threads to avoid unbounded growth
        self.fetchers = [f for f in self.fetchers if f.isRunning()]

        for link in links:
            item = QListWidgetItem(f"{link[1]}")  # title
            item.setData(Qt.UserRole, link[0])   # store id
            if link[3]:
                item.setToolTip(f"Tags: {link[3]}")

            local_path = os.path.join(THUMBNAILS_DIR, f"{link[0]}.jpg")
            if os.path.exists(local_path):
                # Thumbnail already on disk — display it immediately
                item.setIcon(QIcon(load_pixmap(local_path, 100, 75)))
            else:
                # Show white placeholder and kick off background download
                px = QPixmap(100, 75)
                px.fill(Qt.white)
                item.setIcon(QIcon(px))
                fetcher = ThumbnailFetcher(link[0], link[2])
                fetcher.finished.connect(self.on_thumbnail_fetched)
                self.fetchers.append(fetcher)
                fetcher.start()

            self.list_widget.addItem(item)
        # Update tag combo
        self.populate_tag_combo()

    def on_thumbnail_fetched(self, link_id, local_path):
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.UserRole) == link_id:
                item.setIcon(QIcon(load_pixmap(local_path, 100, 75)))
                break
        
        # If the fetched thumbnail belongs to the currently selected link, update the preview
        if self.current_link_id == link_id:
            self.thumbnail_preview.setPixmap(load_pixmap(local_path, 200, 150))

    def populate_tag_combo(self):
        current = self.tag_combo.currentText()
        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItem("All Tags")
        tags = self.db.get_all_tags()
        self.tag_combo.addItems(tags)
        if current in tags:
            self.tag_combo.setCurrentText(current)
        else:
            self.tag_combo.setCurrentText("All Tags")
        self.tag_combo.blockSignals(False)

    def on_search_changed(self, text):
        self.current_search = text.strip()
        self.refresh_list()

    def on_tag_filter_changed(self, tag):
        if tag == "All Tags":
            self.current_tag_filter = ""
        else:
            self.current_tag_filter = tag
        self.refresh_list()

    def on_link_selected(self, item):
        link_id = item.data(Qt.UserRole)
        link = self.db.get_link(link_id)
        if not link:
            return
        self.current_link_id = link_id
        self.title_edit.setText(link[1] or "")
        self.url_edit.setText(link[2] or "")
        self.tags_edit.setText(link[3] or "")
        self.notes_edit.setPlainText(link[4] or "")
        visits = link[5] or 0
        last = link[6] or "Never"
        if last != "Never":
            try:
                dt = datetime.fromisoformat(last)
                last = dt.strftime("%Y-%m-%d %H:%M")
            except:
                pass
        self.stats_label.setText(f"Visits: {visits}  |  Last visited: {last}")
        # Cast (column index 10, added after created_at)
        self.cast_edit.setText(link[10] or "" if len(link) > 10 else "")

        # Update thumbnail preview using Pillow-based loader
        local_path = os.path.join(THUMBNAILS_DIR, f"{link_id}.jpg")
        self.thumbnail_preview.setPixmap(load_pixmap(local_path, 200, 150))

    # ---------- CRUD operations ----------
    def add_link_dialog(self):
        title, ok1 = QInputDialog.getText(self, "Add Link", "Title:")
        if not ok1 or not title.strip():
            return
        url, ok2 = QInputDialog.getText(self, "Add Link", "URL:")
        if not ok2 or not url.strip():
            return
        tags, ok3 = QInputDialog.getText(self, "Add Link", "Tags (comma separated, or leave blank to auto-extract):")
        if not ok3:
            tags = ""
        link_id = self.db.add_link(title.strip(), url.strip(), tags.strip())
        self.refresh_list()
        self.clear_details()
        # Kick off background metadata extraction
        self._start_metadata_fetcher(link_id, url.strip())

    def fetch_tags_cast_now(self):
        """Manually trigger background scraping for the selected link."""
        if self.current_link_id is None:
            QMessageBox.warning(self, "Warning", "No link selected.")
            return
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "Warning", "URL is empty.")
            return
        self.fetch_meta_btn.setEnabled(False)
        self.fetch_meta_btn.setText("🔄 Fetching...")

        if not hasattr(self, 'meta_fetchers'):
            self.meta_fetchers = []
        self.meta_fetchers = [f for f in self.meta_fetchers if f.isRunning()]

        mf = MetadataFetcher(self.current_link_id, url)

        def on_manual_fetched(link_id, tags, cast):
            self.fetch_meta_btn.setEnabled(True)
            self.fetch_meta_btn.setText("🔄 Fetch Tags & Cast")
            if link_id == self.current_link_id:
                if tags:
                    self.tags_edit.setText(tags)
                if cast:
                    self.cast_edit.setText(cast)
                # Auto save changes to the DB too
                self.db.update_link(
                    link_id,
                    self.title_edit.text().strip(),
                    self.url_edit.text().strip(),
                    self.tags_edit.text().strip(),
                    self.notes_edit.toPlainText().strip(),
                    self.cast_edit.text().strip()
                )
                QMessageBox.information(self, "Success", f"Metadata extracted successfully!\n\nTags: {tags}\nCast: {cast}")
            else:
                self.db.update_metadata(link_id, tags, cast)
            self.refresh_list()

        mf.metadata_ready.connect(on_manual_fetched)
        self.meta_fetchers.append(mf)
        mf.start()

    def _start_metadata_fetcher(self, link_id, url):
        """Launch a MetadataFetcher for the given link in the background."""
        if not hasattr(self, 'meta_fetchers'):
            self.meta_fetchers = []
        self.meta_fetchers = [f for f in self.meta_fetchers if f.isRunning()]
        mf = MetadataFetcher(link_id, url)
        mf.metadata_ready.connect(self.on_metadata_fetched)
        self.meta_fetchers.append(mf)
        mf.start()

    def on_metadata_fetched(self, link_id, tags, cast):
        """Called when MetadataFetcher finishes. Updates DB and refreshes UI."""
        self.db.update_metadata(link_id, tags, cast)
        # If the link is currently selected, refresh the fields live
        if self.current_link_id == link_id:
            if tags and not self.tags_edit.text().strip():
                self.tags_edit.setText(tags)
            if cast and not self.cast_edit.text().strip():
                self.cast_edit.setText(cast)
        self.refresh_list()

    def save_link(self):
        if self.current_link_id is None:
            QMessageBox.warning(self, "Warning", "No link selected.")
            return
        title = self.title_edit.text().strip()
        url = self.url_edit.text().strip()
        if not title or not url:
            QMessageBox.warning(self, "Warning", "Title and URL cannot be empty.")
            return
        tags = self.tags_edit.text().strip()
        notes = self.notes_edit.toPlainText().strip()
        cast = self.cast_edit.text().strip()
        self.db.update_link(self.current_link_id, title, url, tags, notes, cast)
        self.refresh_list()
        # Update the selected item text
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.UserRole) == self.current_link_id:
                item.setText(title)
                break
        QMessageBox.information(self, "Success", "Link updated.")

    def delete_link(self):
        if self.current_link_id is None:
            return
        reply = QMessageBox.question(self, "Confirm", "Delete this link?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.db.delete_link(self.current_link_id)
            self.refresh_list()
            self.clear_details()
            self.current_link_id = None

    def clear_details(self):
        self.title_edit.clear()
        self.url_edit.clear()
        self.tags_edit.clear()
        self.cast_edit.clear()
        self.notes_edit.clear()
        self.stats_label.setText("")
        self.thumbnail_preview.setText("")
        self.thumbnail_preview.setPixmap(QPixmap())

    # ---------- Opening links ----------
    def open_internal(self):
        if self.current_link_id is None:
            return
        link = self.db.get_link(self.current_link_id)
        if not link:
            return
        url = link[2]
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        # Record visit
        self.db.record_visit(self.current_link_id)
        # Switch to browser tab and load
        self.tabs.setCurrentIndex(1)  # browser tab
        self.add_browser_tab(QUrl(url), link[1])
        # Update stats
        self.on_link_selected(self.list_widget.currentItem())

    def open_external(self):
        if self.current_link_id is None:
            return
        link = self.db.get_link(self.current_link_id)
        if not link:
            return
        url = link[2]
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        import webbrowser
        webbrowser.open(url)
        self.db.record_visit(self.current_link_id)
        self.on_link_selected(self.list_widget.currentItem())

    def navigate_to_url(self):
        url = self.url_bar.text().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        current_view = self.browser_tabs.currentWidget()
        if current_view:
            current_view.load(QUrl(url))

    def add_browser_tab(self, qurl=None, label="New Tab"):
        web_view = CustomWebEngineView(self)
        
        # Handle Permissions and Fullscreen
        web_view.page().featurePermissionRequested.connect(self.on_feature_permission_requested)
        web_view.page().fullScreenRequested.connect(self.on_fullscreen_requested)

        if qurl:
            web_view.load(qurl)
        
        i = self.browser_tabs.addTab(web_view, label)
        self.browser_tabs.setCurrentIndex(i)

        # Update title and URL dynamically
        web_view.urlChanged.connect(lambda qurl, view=web_view: self.update_url_bar(qurl, view))
        web_view.titleChanged.connect(lambda title, view=web_view: self.update_tab_title(title, view))
        
        return web_view

    # ---------- Advanced Browser Features ----------
    def on_download_requested(self, download):
        suggested_name = download.downloadFileName()
        path, _ = QFileDialog.getSaveFileName(self, "Save File", suggested_name)
        if path:
            download.setPath(path)
            download.accept()
            QMessageBox.information(self, "Download Started", f"Downloading to {path}")

    def on_feature_permission_requested(self, url, feature):
        feature_name = str(feature)
        if feature == QWebEnginePage.MediaAudioCapture:
            feature_name = "Microphone"
        elif feature == QWebEnginePage.MediaVideoCapture:
            feature_name = "Camera"
        elif feature == QWebEnginePage.MediaAudioVideoCapture:
            feature_name = "Camera and Microphone"
        elif feature == QWebEnginePage.Geolocation:
            feature_name = "Location"
        elif feature == QWebEnginePage.Notifications:
            feature_name = "Notifications"
            
        reply = QMessageBox.question(self, "Permission Request",
                                     f"{url.host()} wants to use your {feature_name}. Allow?",
                                     QMessageBox.Yes | QMessageBox.No)
        
        page = self.sender()
        if reply == QMessageBox.Yes:
            page.setFeaturePermission(url, feature, QWebEnginePage.PermissionGrantedByUser)
        else:
            page.setFeaturePermission(url, feature, QWebEnginePage.PermissionDeniedByUser)

    def on_fullscreen_requested(self, request):
        request.accept()
        if request.toggleOn():
            self.showFullScreen()
            self.tabs.tabBar().hide()
            self.browser_tabs.tabBar().hide()
        else:
            self.showNormal()
            self.tabs.tabBar().show()
            self.browser_tabs.tabBar().show()

    def close_browser_tab(self, index):
        if self.browser_tabs.count() < 2:
            return # Keep at least one tab open
        
        widget = self.browser_tabs.widget(index)
        self.browser_tabs.removeTab(index)
        widget.deleteLater()

    def on_browser_tab_changed(self, index):
        current_view = self.browser_tabs.widget(index)
        if current_view:
            self.url_bar.setText(current_view.url().toString())

    def update_url_bar(self, q, view):
        if view == self.browser_tabs.currentWidget():
            self.url_bar.setText(q.toString())
            
    def update_tab_title(self, title, view):
        index = self.browser_tabs.indexOf(view)
        if index != -1:
            self.browser_tabs.setTabText(index, title)

    def browser_back(self):
        current_view = self.browser_tabs.currentWidget()
        if current_view:
            current_view.back()

    def browser_forward(self):
        current_view = self.browser_tabs.currentWidget()
        if current_view:
            current_view.forward()

    def browser_reload(self):
        current_view = self.browser_tabs.currentWidget()
        if current_view:
            current_view.reload()

    def download_current_page_video(self):
        current_view = self.browser_tabs.currentWidget()
        if current_view:
            url = current_view.url().toString()
            if url and url != "about:blank":
                self.tabs.setCurrentWidget(self.downloader_widget)
                self.downloader_widget.quick_url_input.setText(url)
                self.downloader_widget.fetch_formats_for_url(url)
            else:
                QMessageBox.warning(self, "Warning", "No active URL to download.")
        else:
            QMessageBox.warning(self, "Warning", "No active browser tab open.")

    def download_selected_link(self):
        """Download the currently selected link using the Downloader (also triggered by Ctrl+D)."""
        if self.current_link_id is None:
            QMessageBox.warning(self, "No Link Selected", "Please select a link from the list first.")
            return
        link = self.db.get_link(self.current_link_id)
        if link:
            url = link[2]
            self.tabs.setCurrentWidget(self.downloader_widget)
            self.downloader_widget.quick_url_input.setText(url)
            self.downloader_widget.fetch_formats_for_url(url)

    def focus_address_bar(self):
        """Switch to the Browser tab and focus the address bar (Ctrl+L)."""
        self.tabs.setCurrentWidget(self.browser_widget)
        self.url_bar.setFocus()
        self.url_bar.selectAll()

    def close_current_browser_tab(self):
        """Close the currently visible browser tab (Ctrl+W)."""
        idx = self.browser_tabs.currentIndex()
        if self.browser_tabs.count() > 1:
            self.close_browser_tab(idx)

    def focus_search(self):
        """Switch to the Links tab and focus the search box (Ctrl+F)."""
        self.tabs.setCurrentWidget(self.manager_widget)
        self.search_input.setFocus()
        self.search_input.selectAll()


    # ---------- Context menu ----------
    def show_context_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if not item:
            return
        link_id = item.data(Qt.UserRole)
        link = self.db.get_link(link_id)
        if not link:
            return
        menu = QMenu()
        open_internal_action = QAction("Open in Browser Tab", self)
        open_internal_action.triggered.connect(lambda: self.open_context_internal(link_id))
        menu.addAction(open_internal_action)

        open_external_action = QAction("Open External", self)
        open_external_action.triggered.connect(lambda: self.open_context_external(link_id))
        menu.addAction(open_external_action)

        copy_action = QAction("Copy URL", self)
        copy_action.triggered.connect(lambda: self.copy_url(link_id))
        menu.addAction(copy_action)

        delete_action = QAction("Delete", self)
        delete_action.triggered.connect(lambda: self.delete_context(link_id))
        menu.addAction(delete_action)

        download_action = QAction("⬇️ Download Video with MudMoovie", self)
        download_action.triggered.connect(lambda: self.download_context_video(link_id))
        menu.addAction(download_action)

        menu.exec_(self.list_widget.mapToGlobal(pos))


    def open_context_internal(self, link_id):
        self.current_link_id = link_id
        self.open_internal()

    def open_context_external(self, link_id):
        self.current_link_id = link_id
        self.open_external()

    def copy_url(self, link_id):
        link = self.db.get_link(link_id)
        if link:
            QApplication.clipboard().setText(link[2])
            QMessageBox.information(self, "Copied", "URL copied to clipboard.")

    def delete_context(self, link_id):
        self.current_link_id = link_id
        self.delete_link()

    def download_context_video(self, link_id):
        link = self.db.get_link(link_id)
        if link:
            url = link[2]
            self.tabs.setCurrentWidget(self.downloader_widget)
            self.downloader_widget.quick_url_input.setText(url)
            self.downloader_widget.fetch_formats_for_url(url)

    def save_current_page(self):
        current_view = self.browser_tabs.currentWidget()
        if current_view:
            url = current_view.url().toString()
            if url and url != "about:blank":
                title = current_view.page().title() or url
                tags, ok = QInputDialog.getText(self, "Save Page", f"Saving: {title}\nURL: {url}\n\nTags (comma separated):")
                if ok:
                    self.db.add_link(title, url, tags.strip())
                    self.refresh_list()
                    QMessageBox.information(self, "Success", "Page saved to links.")
            else:
                QMessageBox.warning(self, "Warning", "No active URL to save.")
        else:
            QMessageBox.warning(self, "Warning", "No active browser tab open.")

    def remove_duplicate_links(self):
        links = self.db.get_all_links()
        seen_urls = set()
        duplicates_removed = 0
        
        # Sort links by ID to keep the oldest ones
        links.sort(key=lambda x: x[0])
        
        for link in links:
            url = link[2]
            if url in seen_urls:
                self.db.delete_link(link[0])
                duplicates_removed += 1
            else:
                seen_urls.add(url)
                
        if duplicates_removed > 0:
            self.refresh_list()
            self.clear_details()
            QMessageBox.information(self, "Success", f"Removed {duplicates_removed} duplicate link(s).")
        else:
            QMessageBox.information(self, "Info", "No duplicate links found.")


    # ---------- Import / Export ----------
    def export_json(self):
        links = self.db.get_all_links()
        data = []
        for link in links:
            data.append({
                "id": link[0],
                "title": link[1],
                "url": link[2],
                "tags": link[3],
                "notes": link[4],
                "visit_count": link[5],
                "last_visited": link[6],
                "created_at": link[7]
            })
        file_path, _ = QFileDialog.getSaveFileName(self, "Export JSON", "", "JSON Files (*.json)")
        if file_path:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            QMessageBox.information(self, "Export", f"Exported {len(data)} links.")

    def import_json(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Import JSON", "", "JSON Files (*.json)")
        if not file_path:
            return
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to read file: {e}")
            return
        count = 0
        for entry in data:
            # Skip if missing required fields
            if "title" not in entry or "url" not in entry:
                continue
            title = entry.get("title", "")
            url = entry.get("url", "")
            tags = entry.get("tags", "")
            notes = entry.get("notes", "")
            self.db.add_link(title, url, tags, notes)
            count += 1
        self.refresh_list()
        QMessageBox.information(self, "Import", f"Imported {count} links.")

    def import_txt(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Import TXT", "", "Text Files (*.txt);;All Files (*)")
        if not file_path:
            return
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to read file: {e}")
            return
        
        count = 0
        for line in lines:
            url = line.strip()
            if url:
                # Use URL as title
                self.db.add_link(url, url, "", "")
                count += 1
        self.refresh_list()
        QMessageBox.information(self, "Import", f"Imported {count} links from TXT.")

    def closeEvent(self, event):
        self.db.close()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
