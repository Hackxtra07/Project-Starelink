import os
import sys
import json
import re
import csv
import threading
from datetime import datetime
from pathlib import Path

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QTabWidget, QTableWidget,
    QTableWidgetItem, QListWidget, QPushButton, QLineEdit, QLabel, QSpinBox,
    QComboBox, QCheckBox, QTextEdit, QFileDialog, QMessageBox, QHeaderView,
    QAbstractItemView
)
from PyQt5.QtCore import Qt, QObject, pyqtSignal, QSize
from PyQt5.QtGui import QIcon, QFont, QColor

# Check and import dependencies
try:
    import yt_dlp
except ImportError:
    import subprocess
    print("yt-dlp not found. Attempting to install...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "yt-dlp"])
    import yt_dlp


class DownloaderSignals(QObject):
    progress = pyqtSignal(str, dict)     # download_id, progress_data
    completed = pyqtSignal(str, dict)    # download_id, history_entry
    error = pyqtSignal(str, str)         # download_id, error_message
    log = pyqtSignal(str, str)           # message, level
    queue_updated = pyqtSignal()
    formats_ready = pyqtSignal(str, object, list)  # url, info_dict, formats_list
    formats_error = pyqtSignal(str)                # error_message
    playlist_ready = pyqtSignal(str, list)         # title, entries_list
    playlist_error = pyqtSignal(str)               # error_message



class VideoDownloaderManager(QObject):
    def __init__(self, config_file="downloader_config.json", history_file="download_history.json"):
        super().__init__()
        self.config_file = config_file
        self.history_file = history_file
        self.signals = DownloaderSignals()
        
        self.config = self.load_config()
        self.download_queue = []
        self.active_downloads = {}
        self.download_history = []
        self.download_id_counter = 1
        self.paused = False
        self.lock = threading.RLock()  # Use RLock to prevent deadlocks when emitting signals that trigger slots trying to acquire the lock
        
        self.load_history()

    def load_config(self):
        default_config = {
            'download_path': str(Path.home() / 'Downloads' / 'YouTube Downloads'),
            'max_concurrent': 3,
            'auto_open_folder': False,
            'show_notifications': True,
            'default_format': 'best',
            'embed_thumbnail': True,
            'embed_subtitles': False,
            'subtitles_langs': ['en'],
            'extract_audio': False,
            'audio_format': 'mp3',
            'audio_quality': '192',
            'limit_speed': False,
            'speed_limit': '0',
            'proxy': '',
            'cookies_file': '',
            'auto_number': True,
            'output_template': '%(title)s.%(ext)s',
            'playlist_start': 1,
            'playlist_end': None,
            'retry_count': 3,
            'timeout': 30,
            'concurrent_fragments': 5,
            'use_external_downloader': True,
            'external_downloader_threads': 8
        }
        
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    for key, val in default_config.items():
                        if key not in config:
                            config[key] = val
                    return config
            except Exception as e:
                print(f"Error loading downloader config: {e}")
                return default_config
        return default_config

    def save_config(self):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.config_file)), exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4)
            self.signals.log.emit("Configuration saved successfully", "INFO")
        except Exception as e:
            self.signals.log.emit(f"Failed to save config: {str(e)}", "ERROR")

    def load_history(self):
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    self.download_history = json.load(f)
            except Exception as e:
                self.signals.log.emit(f"Failed to load download history: {str(e)}", "ERROR")

    def save_history(self):
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.download_history, f, indent=4)
        except Exception as e:
            self.signals.log.emit(f"Failed to save history: {str(e)}", "ERROR")

    def add_to_queue(self, url, title, format_id, format_name):
        with self.lock:
            download_item = {
                'url': url,
                'title': title,
                'format_id': format_id,
                'format_name': format_name,
                'is_playlist': False
            }
            self.download_queue.append(download_item)
            self.signals.queue_updated.emit()
            self.signals.log.emit(f"Added to queue: {title} [{format_name}]", "INFO")
        self.process_queue()

    def process_queue(self):
        with self.lock:
            if self.paused:
                return
            if not self.download_queue:
                return
            active_count = len([k for k, v in self.active_downloads.items() if v['status'] == 'downloading'])
            if active_count >= self.config['max_concurrent']:
                return
            
            # Start next download
            item = self.download_queue.pop(0)
            self.signals.queue_updated.emit()
            
            download_id = str(self.download_id_counter)
            self.download_id_counter += 1
            
            self.active_downloads[download_id] = {
                'item': item,
                'status': 'downloading',
                'progress': 0.0,
                'speed': '0 KB/s',
                'size': 'Unknown',
                'eta': 'Unknown'
            }
            
            threading.Thread(target=self.download_thread_worker, args=(item, download_id), daemon=True).start()

    def download_thread_worker(self, item, download_id):
        self.signals.log.emit(f"Starting download: {item['title']}", "INFO")
        
        def progress_hook(d):
            if d['status'] == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                downloaded = d.get('downloaded_bytes', 0)
                
                if total:
                    progress_val = (downloaded / total) * 100 if total > 0 else 0.0
                    size_str = f"{total / (1024*1024):.1f} MB"
                else:
                    progress_val = 0.0
                    size_str = "Unknown"
                    
                speed = d.get('speed', 0)
                if speed:
                    if speed > 1024 * 1024:
                        speed_str = f"{speed / (1024*1024):.1f} MB/s"
                    else:
                        speed_str = f"{speed / 1024:.1f} KB/s"
                else:
                    speed_str = "N/A"
                
                eta = d.get('eta', 0)
                if eta:
                    try:
                        eta_int = int(eta)
                        eta_str = f"{eta_int // 60}:{eta_int % 60:02d}"
                    except (ValueError, TypeError):
                        eta_str = "N/A"
                else:
                    eta_str = "N/A"
                
                progress_info = {
                    'progress': progress_val,
                    'speed': speed_str,
                    'size': size_str,
                    'eta': eta_str,
                    'status': 'downloading'
                }
                self.signals.progress.emit(download_id, progress_info)
                
            elif d['status'] == 'finished':
                # Will handle completion at the end of the block
                pass

        # Build options dictionary
        os.makedirs(self.config['download_path'], exist_ok=True)
        ydl_opts = {
            'outtmpl': os.path.join(self.config['download_path'], self.config['output_template']),
            'progress_hooks': [progress_hook],
            'quiet': True,
            'no_warnings': True,
            'format': item.get('format_id', 'best'),
            'retries': self.config.get('retry_count', 3),
            'timeout': self.config.get('timeout', 30),
            'source_address': '0.0.0.0',
            'socket_timeout': 15,
            'concurrent_fragments': self.config.get('concurrent_fragments', 5)
        }
        
        if self.config.get('use_external_downloader'):
            ydl_opts['external_downloader'] = 'aria2c'
            threads = self.config.get('external_downloader_threads', 8)
            ydl_opts['external_downloader_args'] = {
                'aria2c': [
                    f'--max-connection-per-server={threads}',
                    f'--split={threads}',
                    '--min-split-size=1M',
                ]
            }
        
        if self.config.get('limit_speed') and self.config.get('speed_limit', '0') != '0':
            try:
                ydl_opts['ratelimit'] = int(self.config['speed_limit']) * 1024
            except ValueError:
                pass
        
        if self.config.get('proxy'):
            ydl_opts['proxy'] = self.config['proxy']
        
        if self.config.get('cookies_file') and os.path.exists(self.config['cookies_file']):
            ydl_opts['cookiefile'] = self.config['cookies_file']
        
        if self.config.get('embed_thumbnail'):
            ydl_opts['writethumbnail'] = True
            ydl_opts['embedthumbnail'] = True
        
        if self.config.get('extract_audio'):
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': self.config.get('audio_format', 'mp3'),
                'preferredquality': self.config.get('audio_quality', '192'),
            }]
        
        if self.config.get('embed_subtitles') and self.config.get('subtitles_langs'):
            ydl_opts['writesubtitles'] = True
            ydl_opts['writeautomaticsub'] = True
            ydl_opts['subtitleslangs'] = self.config['subtitles_langs']
            ydl_opts['embedsubs'] = True
            
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info_dict = ydl.extract_info(item['url'], download=True)
                final_filename = ydl.prepare_filename(info_dict)
                # Check for postprocessed extension (e.g. mp3)
                if self.config.get('extract_audio'):
                    ext = self.config.get('audio_format', 'mp3')
                    final_filename = os.path.splitext(final_filename)[0] + f".{ext}"
            
            size_str = "Unknown"
            if os.path.exists(final_filename):
                try:
                    actual_size = os.path.getsize(final_filename)
                    if actual_size > 1024 * 1024 * 1024:
                        size_str = f"{actual_size / (1024*1024*1024):.1f} GB"
                    else:
                        size_str = f"{actual_size / (1024*1024):.1f} MB"
                except:
                    pass
            
            history_entry = {
                'date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'title': item['title'],
                'format': item.get('format_name', 'Unknown'),
                'size': size_str,
                'status': 'Completed',
                'file_path': final_filename,
                'url': item.get('url', '')
            }
            
            self.signals.completed.emit(download_id, history_entry)
            self.signals.log.emit(f"Download completed: {item['title']}", "SUCCESS")
        except Exception as e:
            self.signals.error.emit(download_id, str(e))
            self.signals.log.emit(f"Download failed for {item['title']}: {str(e)}", "ERROR")
            
        # Move to next
        self.process_queue()


class DownloaderWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.manager = VideoDownloaderManager()
        self.loaded_playlist_items = []
        
        # Connect signals
        self.manager.signals.progress.connect(self.on_download_progress)
        self.manager.signals.completed.connect(self.on_download_completed)
        self.manager.signals.error.connect(self.on_download_error)
        self.manager.signals.log.connect(self.on_log_message)
        self.manager.signals.queue_updated.connect(self.on_queue_updated)
        self.manager.signals.formats_ready.connect(self.on_formats_ready)
        self.manager.signals.formats_error.connect(self.on_formats_error)
        self.manager.signals.playlist_ready.connect(self.on_playlist_ready)
        self.manager.signals.playlist_error.connect(self.on_playlist_error)
        
        self.setup_ui()
        self.refresh_history_ui()
        self.refresh_settings_ui()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # URL Quick input bar
        url_input_layout = QHBoxLayout()
        url_input_layout.addWidget(QLabel("Video/Playlist URL:"))
        self.quick_url_input = QLineEdit()
        self.quick_url_input.setPlaceholderText("Paste YouTube or video link here...")
        url_input_layout.addWidget(self.quick_url_input)
        
        fetch_formats_btn = QPushButton("🎬 Fetch Formats")
        fetch_formats_btn.clicked.connect(self.fetch_formats_quick)
        url_input_layout.addWidget(fetch_formats_btn)
        
        load_playlist_btn = QPushButton("📋 Load Playlist")
        load_playlist_btn.clicked.connect(self.load_playlist_quick)
        url_input_layout.addWidget(load_playlist_btn)
        
        main_layout.addLayout(url_input_layout)
        
        # Tab Widget for downloader sub-sections
        self.sub_tabs = QTabWidget()
        main_layout.addWidget(self.sub_tabs)
        
        # Sub-tab 1: Active Downloads
        self.active_tab = QWidget()
        self.setup_active_tab()
        self.sub_tabs.addTab(self.active_tab, "📥 Active Downloads")
        
        # Sub-tab 2: Download Queue
        self.queue_tab = QWidget()
        self.setup_queue_tab()
        self.sub_tabs.addTab(self.queue_tab, "⏳ Queue")
        
        # Sub-tab 3: Formats Selector
        self.formats_tab = QWidget()
        self.setup_formats_tab()
        self.sub_tabs.addTab(self.formats_tab, "🎬 Formats")
        
        # Sub-tab 4: Playlist Manager
        self.playlist_tab = QWidget()
        self.setup_playlist_tab()
        self.sub_tabs.addTab(self.playlist_tab, "📋 Playlist Manager")
        
        # Sub-tab 5: History
        self.history_tab = QWidget()
        self.setup_history_tab()
        self.sub_tabs.addTab(self.history_tab, "📜 History")
        
        # Sub-tab 6: Settings
        self.settings_tab = QWidget()
        self.setup_settings_tab()
        self.sub_tabs.addTab(self.settings_tab, "⚙️ Settings")
        
        # Sub-tab 7: Log Console
        self.console_tab = QWidget()
        self.setup_console_tab()
        self.sub_tabs.addTab(self.console_tab, "📝 Console")
        
        # Status Bar
        status_layout = QHBoxLayout()
        self.status_bar_label = QLabel("✅ Ready")
        status_layout.addWidget(self.status_bar_label)
        status_layout.addStretch()
        main_layout.addLayout(status_layout)

    def setup_active_tab(self):
        layout = QVBoxLayout(self.active_tab)
        
        self.active_table = QTableWidget()
        self.active_table.setColumnCount(8)
        self.active_table.setHorizontalHeaderLabels([
            "ID", "Title", "Format", "Progress", "Size", "Speed", "ETA", "Status"
        ])
        self.active_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.active_table.horizontalHeader().setStretchLastSection(True)
        self.active_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.active_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.active_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.active_table.setAlternatingRowColors(True)
        layout.addWidget(self.active_table)
        
        # Controls
        ctrl_layout = QHBoxLayout()
        remove_btn = QPushButton("❌ Remove Selected")
        remove_btn.clicked.connect(self.remove_selected_active)
        ctrl_layout.addWidget(remove_btn)
        
        retry_btn = QPushButton("🔄 Retry Failed")
        retry_btn.clicked.connect(self.retry_failed)
        ctrl_layout.addWidget(retry_btn)
        
        clear_completed_btn = QPushButton("🗑️ Clear Completed")
        clear_completed_btn.clicked.connect(self.clear_completed_active)
        ctrl_layout.addWidget(clear_completed_btn)
        
        self.pause_btn = QPushButton("⏸️ Pause All")
        self.pause_btn.clicked.connect(self.toggle_pause_all)
        ctrl_layout.addWidget(self.pause_btn)
        
        stop_all_btn = QPushButton("🛑 Stop/Clear All")
        stop_all_btn.clicked.connect(self.stop_all)
        ctrl_layout.addWidget(stop_all_btn)
        
        layout.addLayout(ctrl_layout)

    def setup_queue_tab(self):
        layout = QVBoxLayout(self.queue_tab)
        
        self.queue_list = QListWidget()
        layout.addWidget(self.queue_list)
        
        ctrl_layout = QHBoxLayout()
        up_btn = QPushButton("⬆️ Move Up")
        up_btn.clicked.connect(self.move_queue_up)
        ctrl_layout.addWidget(up_btn)
        
        down_btn = QPushButton("⬇️ Move Down")
        down_btn.clicked.connect(self.move_queue_down)
        ctrl_layout.addWidget(down_btn)
        
        remove_btn = QPushButton("❌ Remove")
        remove_btn.clicked.connect(self.remove_selected_queue)
        ctrl_layout.addWidget(remove_btn)
        
        clear_btn = QPushButton("🔄 Clear Queue")
        clear_btn.clicked.connect(self.clear_queue)
        ctrl_layout.addWidget(clear_btn)
        
        layout.addLayout(ctrl_layout)

    def setup_formats_tab(self):
        layout = QVBoxLayout(self.formats_tab)
        
        self.formats_title_label = QLabel("No URL parsed yet. Input a URL above and click Fetch Formats.")
        self.formats_title_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.formats_title_label)
        
        self.formats_table = QTableWidget()
        self.formats_table.setColumnCount(5)
        self.formats_table.setHorizontalHeaderLabels([
            "Format ID", "Resolution", "Extension", "Size", "Note"
        ])
        self.formats_table.horizontalHeader().setStretchLastSection(True)
        self.formats_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.formats_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.formats_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.formats_table.setAlternatingRowColors(True)
        layout.addWidget(self.formats_table)
        
        self.current_formats_info = None
        self.current_formats_url = None
        
        ctrl_layout = QHBoxLayout()
        self.download_format_btn = QPushButton("⬇️ Download Selected Format")
        self.download_format_btn.clicked.connect(self.download_selected_format)
        self.download_format_btn.setEnabled(False)
        ctrl_layout.addWidget(self.download_format_btn)
        layout.addLayout(ctrl_layout)

    def setup_playlist_tab(self):
        layout = QVBoxLayout(self.playlist_tab)
        
        self.playlist_title_label = QLabel("No playlist loaded.")
        self.playlist_title_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.playlist_title_label)
        
        self.playlist_table = QTableWidget()
        self.playlist_table.setColumnCount(4)
        self.playlist_table.setHorizontalHeaderLabels([
            "#", "Title", "Duration", "Status"
        ])
        self.playlist_table.horizontalHeader().setStretchLastSection(True)
        self.playlist_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.playlist_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.playlist_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.playlist_table.setAlternatingRowColors(True)
        layout.addWidget(self.playlist_table)
        
        ctrl_layout = QHBoxLayout()
        dl_sel_btn = QPushButton("⬇️ Download Selected")
        dl_sel_btn.clicked.connect(self.download_selected_playlist)
        ctrl_layout.addWidget(dl_sel_btn)
        
        dl_all_btn = QPushButton("⬇️ Download All")
        dl_all_btn.clicked.connect(self.download_all_playlist)
        ctrl_layout.addWidget(dl_all_btn)
        
        range_btn = QPushButton("📋 Select Range")
        range_btn.clicked.connect(self.select_playlist_range)
        ctrl_layout.addWidget(range_btn)
        
        layout.addLayout(ctrl_layout)

    def setup_history_tab(self):
        layout = QVBoxLayout(self.history_tab)
        
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(6)
        self.history_table.setHorizontalHeaderLabels([
            "Date", "Title", "Format", "Size", "Status", "File Path"
        ])
        self.history_table.horizontalHeader().setStretchLastSection(True)
        self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.itemDoubleClicked.connect(self.on_history_double_clicked)
        layout.addWidget(self.history_table)
        
        ctrl_layout = QHBoxLayout()
        open_folder_btn = QPushButton("📂 Open File Location")
        open_folder_btn.clicked.connect(self.open_selected_history_file)
        ctrl_layout.addWidget(open_folder_btn)
        
        redl_btn = QPushButton("🔄 Re-download")
        redl_btn.clicked.connect(self.redownload_selected_history)
        ctrl_layout.addWidget(redl_btn)
        
        export_btn = QPushButton("📊 Export History (CSV)")
        export_btn.clicked.connect(self.export_history_csv)
        ctrl_layout.addWidget(export_btn)
        
        clear_btn = QPushButton("🗑️ Clear History")
        clear_btn.clicked.connect(self.clear_history)
        ctrl_layout.addWidget(clear_btn)
        
        layout.addLayout(ctrl_layout)

    def setup_settings_tab(self):
        layout = QVBoxLayout(self.settings_tab)
        grid = QGridLayout()
        
        # Download path
        grid.addWidget(QLabel("Download Folder:"), 0, 0)
        self.path_edit = QLineEdit()
        grid.addWidget(self.path_edit, 0, 1)
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_download_path)
        grid.addWidget(browse_btn, 0, 2)
        
        # Max Concurrency
        grid.addWidget(QLabel("Max Concurrent Downloads:"), 1, 0)
        self.max_concurrent_spin = QSpinBox()
        self.max_concurrent_spin.setRange(1, 10)
        grid.addWidget(self.max_concurrent_spin, 1, 1)
        
        # Concurrent Fragments
        grid.addWidget(QLabel("Concurrent Fragments (Parallel Threads):"), 2, 0)
        self.concurrent_fragments_spin = QSpinBox()
        self.concurrent_fragments_spin.setRange(1, 16)
        grid.addWidget(self.concurrent_fragments_spin, 2, 1)
        
        # External Downloader
        self.use_external_downloader_cb = QCheckBox("Use aria2c Downloader (10x Speed)")
        grid.addWidget(self.use_external_downloader_cb, 3, 0)
        self.external_downloader_threads_spin = QSpinBox()
        self.external_downloader_threads_spin.setRange(1, 16)
        grid.addWidget(QLabel("aria2c Connections:"), 3, 1)
        grid.addWidget(self.external_downloader_threads_spin, 3, 2)
        
        # Output Template
        grid.addWidget(QLabel("Output Name Template:"), 4, 0)
        self.template_edit = QLineEdit()
        grid.addWidget(self.template_edit, 4, 1, 1, 2)
        
        # Speed Limiter
        self.speed_limit_cb = QCheckBox("Limit Download Speed")
        grid.addWidget(self.speed_limit_cb, 5, 0)
        self.speed_limit_edit = QLineEdit()
        self.speed_limit_edit.setPlaceholderText("Speed in KB/s (e.g. 500)")
        grid.addWidget(self.speed_limit_edit, 5, 1)
        grid.addWidget(QLabel("KB/s"), 5, 2)
        
        # Audio Extraction
        self.extract_audio_cb = QCheckBox("Extract Audio Only")
        grid.addWidget(self.extract_audio_cb, 6, 0)
        self.audio_format_combo = QComboBox()
        self.audio_format_combo.addItems(['mp3', 'm4a', 'aac', 'flac', 'opus', 'vorbis'])
        grid.addWidget(self.audio_format_combo, 6, 1)
        self.audio_quality_combo = QComboBox()
        self.audio_quality_combo.addItems(['128', '192', '256', '320', 'lossless'])
        grid.addWidget(self.audio_quality_combo, 6, 2)
        
        # Post-Processing embed
        self.embed_thumb_cb = QCheckBox("Embed Thumbnail in File")
        grid.addWidget(self.embed_thumb_cb, 7, 0)
        
        self.embed_subs_cb = QCheckBox("Embed Subtitles")
        grid.addWidget(self.embed_subs_cb, 7, 1)
        
        # Network settings
        grid.addWidget(QLabel("Proxy URL (optional):"), 8, 0)
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("e.g. http://127.0.0.1:8080")
        grid.addWidget(self.proxy_edit, 8, 1, 1, 2)
        
        grid.addWidget(QLabel("Cookies File (optional):"), 9, 0)
        self.cookies_edit = QLineEdit()
        self.cookies_edit.setPlaceholderText("Path to cookies.txt")
        grid.addWidget(self.cookies_edit, 9, 1)
        cookies_browse_btn = QPushButton("Browse")
        cookies_browse_btn.clicked.connect(self.browse_cookies_path)
        grid.addWidget(cookies_browse_btn, 9, 2)
        
        layout.addLayout(grid)
        layout.addStretch()
        
        # Save Button
        save_btn = QPushButton("💾 Save Settings")
        save_btn.setStyleSheet("font-weight: bold; padding: 6px;")
        save_btn.clicked.connect(self.save_settings)
        layout.addWidget(save_btn)

    def setup_console_tab(self):
        layout = QVBoxLayout(self.console_tab)
        self.console_text = QTextEdit()
        self.console_text.setReadOnly(True)
        self.console_text.setFont(QFont("Consolas", 9))
        layout.addWidget(self.console_text)
        
        ctrl_layout = QHBoxLayout()
        clear_btn = QPushButton("Clear Console")
        clear_btn.clicked.connect(self.console_text.clear)
        ctrl_layout.addWidget(clear_btn)
        
        save_log_btn = QPushButton("Save Log File")
        save_log_btn.clicked.connect(self.save_console_log)
        ctrl_layout.addWidget(save_log_btn)
        
        layout.addLayout(ctrl_layout)

    # ---------- Logic & Event Handlers ----------
    
    def on_log_message(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        emoji = {"INFO": "ℹ️", "ERROR": "❌", "WARNING": "⚠️", "SUCCESS": "✅", "DEBUG": "🐛"}.get(level, "📝")
        log_line = f"[{timestamp}] {emoji} {level}: {message}"
        self.console_text.append(log_line)
        self.status_bar_label.setText(f"{emoji} {message[:100]}")

    def on_download_progress(self, download_id, data):
        # Update Table row matching download_id
        for row in range(self.active_table.rowCount()):
            item = self.active_table.item(row, 0)
            if item and item.text() == download_id:
                self.active_table.setItem(row, 3, QTableWidgetItem(f"{data['progress']:.1f}%"))
                self.active_table.setItem(row, 4, QTableWidgetItem(data['size']))
                self.active_table.setItem(row, 5, QTableWidgetItem(data['speed']))
                self.active_table.setItem(row, 6, QTableWidgetItem(data['eta']))
                self.active_table.setItem(row, 7, QTableWidgetItem(f"⬇️ Downloading"))
                break

    def on_download_completed(self, download_id, history_entry):
        # Remove from active table or update status
        for row in range(self.active_table.rowCount()):
            item = self.active_table.item(row, 0)
            if item and item.text() == download_id:
                self.active_table.removeRow(row)
                break
                
        # Clean from manager active dict
        with self.manager.lock:
            if download_id in self.manager.active_downloads:
                del self.manager.active_downloads[download_id]
                
        # Add to history and update history view
        self.manager.download_history.insert(0, history_entry)
        self.manager.save_history()
        self.refresh_history_ui()

    def on_download_error(self, download_id, error_msg):
        # Update row to show error
        for row in range(self.active_table.rowCount()):
            item = self.active_table.item(row, 0)
            if item and item.text() == download_id:
                self.active_table.setItem(row, 7, QTableWidgetItem(f"❌ Error"))
                self.active_table.item(row, 7).setToolTip(error_msg)
                
                # Update status in active dict
                with self.manager.lock:
                    if download_id in self.manager.active_downloads:
                        self.manager.active_downloads[download_id]['status'] = 'error'
                break

    def on_queue_updated(self):
        self.queue_list.clear()
        with self.manager.lock:
            for idx, item in enumerate(self.manager.download_queue, 1):
                self.queue_list.addItem(f"{idx}. {item['title']} [{item['format_name']}]")

    def fetch_formats_quick(self):
        url = self.quick_url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Warning", "Please enter a URL first!")
            return
        self.fetch_formats_for_url(url)

    def fetch_formats_for_url(self, url):
        """Start background thread to fetch formats; result comes back via signals."""
        self.sub_tabs.setCurrentWidget(self.formats_tab)
        self.formats_title_label.setText("Fetching formats... Please wait.")
        self.formats_table.setRowCount(0)
        self.download_format_btn.setEnabled(False)
        self.current_formats_info = None
        self.current_formats_url = url

        def thread_target():
            try:
                # Add 'noplaylist': True so it only fetches the single video, not an entire playlist
                ydl_opts = {
                    'quiet': True, 
                    'no_warnings': True, 
                    'noplaylist': True,
                    'source_address': '0.0.0.0', # Force IPv4, prevents hangs on broken IPv6 networks
                    'socket_timeout': 15,        # Don't hang forever on slow hosts
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                formats = info.get('formats', [])
                # Emit signal — this safely crosses the thread boundary
                self.manager.signals.formats_ready.emit(url, info, formats)
            except Exception as e:
                self.manager.signals.formats_error.emit(str(e))

        threading.Thread(target=thread_target, daemon=True).start()

    def on_formats_ready(self, url, info, formats):
        """Called in main thread when format list is fetched successfully."""
        self.current_formats_info = info
        self.current_formats_url = url
        title = info.get('title', 'Unknown Title')
        # Truncate title to ~40 chars for display
        display_title = title[:40] + '...' if len(title) > 40 else title
        self.formats_title_label.setText(f"Formats for: {display_title}")
        self.formats_table.setRowCount(0)

        row = 0
        for f in formats:
            if f.get('vcodec') != 'none' or f.get('acodec') != 'none':
                self.formats_table.insertRow(row)

                format_id = f.get('format_id', 'unknown')
                resolution = f"{f.get('height')}p" if f.get('height') else "Audio Only"
                ext = f.get('ext', 'unknown')

                filesize = f.get('filesize') or f.get('filesize_approx')
                
                # Fallback: estimate from bitrate and duration
                if not filesize:
                    tbr = f.get('tbr')
                    if not tbr:
                        vbr = f.get('vbr') or 0
                        abr = f.get('abr') or 0
                        if vbr or abr:
                            tbr = vbr + abr
                            
                    duration = info.get('duration')
                    if tbr and duration:
                        # tbr is in kilobits per second (kbps)
                        filesize = (tbr * 1024 * duration) / 8

                if filesize:
                    if filesize > 1024 * 1024 * 1024:
                        size_str = f"{filesize / (1024*1024*1024):.1f} GB"
                    else:
                        size_str = f"{filesize / (1024*1024):.1f} MB"
                else:
                    size_str = "~"

                note = f.get('format_note', '') or ''
                if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                    format_id = f"Audio: {format_id}"

                self.formats_table.setItem(row, 0, QTableWidgetItem(str(format_id)))
                self.formats_table.setItem(row, 1, QTableWidgetItem(str(resolution)))
                self.formats_table.setItem(row, 2, QTableWidgetItem(str(ext)))
                self.formats_table.setItem(row, 3, QTableWidgetItem(str(size_str)))
                self.formats_table.setItem(row, 4, QTableWidgetItem(str(note)))
                row += 1

        self.formats_table.resizeColumnsToContents()
        self.download_format_btn.setEnabled(True)
        self.on_log_message(f"Loaded {row} formats for: {display_title}", "INFO")

    def on_formats_error(self, error_msg):
        """Called in main thread when format fetching fails."""
        self.formats_title_label.setText("Failed to retrieve formats.")
        self.on_log_message(f"Format fetch error: {error_msg}", "ERROR")
        QMessageBox.critical(self, "Fetch Error", f"Could not fetch formats:\n{error_msg}")

    def download_selected_format(self):
        selected = self.formats_table.selectedItems()
        if not selected or not self.current_formats_info:
            QMessageBox.warning(self, "Warning", "Please select a format from the table!")
            return
            
        row = selected[0].row()
        format_id_str = self.formats_table.item(row, 0).text()
        resolution = self.formats_table.item(row, 1).text()
        
        # Parse format ID
        format_id = format_id_str.replace("Audio: ", "")
        
        # If it's a video format but has no audio, request merging with best audio
        # We check the original format info dictionary for audio details of this format
        is_video_only = False
        for f in self.current_formats_info.get('formats', []):
            if str(f.get('format_id')) == format_id:
                if f.get('vcodec') != 'none' and f.get('acodec') == 'none':
                    is_video_only = True
                break
                
        final_format_id = format_id
        if is_video_only:
            final_format_id = f"{format_id}+bestaudio/best"
            
        self.manager.add_to_queue(
            url=self.current_formats_url,
            title=self.current_formats_info.get('title', 'Unknown Title'),
            format_id=final_format_id,
            format_name=resolution
        )
        
        # Insert a pending row into Active table
        active_row = self.active_table.rowCount()
        self.active_table.insertRow(active_row)
        
        # Generate temporary display ID
        display_id = str(self.manager.download_id_counter - 1)
        
        full_title = self.current_formats_info.get('title', 'Unknown Title')
        words = full_title.split()
        short_title = ' '.join(words[:4]) + ('...' if len(words) > 4 else '')

        title_item = QTableWidgetItem(short_title)
        title_item.setToolTip(full_title)   # hover to see full name
        self.active_table.setItem(active_row, 0, QTableWidgetItem(display_id))
        self.active_table.setItem(active_row, 1, title_item)
        self.active_table.setItem(active_row, 2, QTableWidgetItem(resolution))
        self.active_table.setItem(active_row, 3, QTableWidgetItem("0.0%"))
        self.active_table.setItem(active_row, 4, QTableWidgetItem("Unknown"))
        self.active_table.setItem(active_row, 5, QTableWidgetItem("0 KB/s"))
        self.active_table.setItem(active_row, 6, QTableWidgetItem("Unknown"))
        self.active_table.setItem(active_row, 7, QTableWidgetItem("⏳ Pending"))
        
        self.sub_tabs.setCurrentWidget(self.active_tab)
        QMessageBox.information(self, "Success", "Added to download queue!")

    def load_playlist_quick(self):
        url = self.quick_url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Warning", "Please enter a URL first!")
            return
        self.load_playlist_url(url)

    def load_playlist_url(self, url):
        """Start background thread to load playlist; results come back via signals."""
        self.sub_tabs.setCurrentWidget(self.playlist_tab)
        self.playlist_title_label.setText("Loading playlist... Please wait.")
        self.playlist_table.setRowCount(0)
        self.loaded_playlist_items = []

        def thread_target():
            try:
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': True,
                    'playliststart': 1
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if 'entries' not in info:
                    raise ValueError("Provided URL is not a playlist.")
                playlist_title = info.get('title', 'Unknown Playlist')
                entries = [e for e in info['entries'] if e]
                self.manager.signals.playlist_ready.emit(playlist_title, entries)
            except Exception as e:
                self.manager.signals.playlist_error.emit(str(e))

        threading.Thread(target=thread_target, daemon=True).start()

    def on_playlist_ready(self, playlist_title, entries):
        """Called in main thread when playlist data is fetched successfully."""
        self.playlist_title_label.setText(f"Playlist: {playlist_title} ({len(entries)} videos)")
        self.playlist_table.setRowCount(0)
        self.loaded_playlist_items = []

        for i, entry in enumerate(entries, 1):
            duration = entry.get('duration')
            if duration:
                try:
                    dur_int = int(duration)
                    dur_str = f"{dur_int // 60}:{dur_int % 60:02d}"
                except (ValueError, TypeError):
                    dur_str = "Unknown"
            else:
                dur_str = "Unknown"

            item_data = {
                'url': entry.get('url') or entry.get('webpage_url'),
                'title': entry.get('title', 'Unknown'),
                'format_id': 'best',
                'format_name': 'Best Quality',
                'is_playlist': False
            }
            self.loaded_playlist_items.append(item_data)

            row = self.playlist_table.rowCount()
            self.playlist_table.insertRow(row)
            self.playlist_table.setItem(row, 0, QTableWidgetItem(str(i)))
            # Truncate title to 3-4 words for the table
            words = entry.get('title', 'Unknown').split()
            short_title = ' '.join(words[:4]) + ('...' if len(words) > 4 else '')
            self.playlist_table.setItem(row, 1, QTableWidgetItem(short_title))
            self.playlist_table.item(row, 1).setToolTip(entry.get('title', 'Unknown'))
            self.playlist_table.setItem(row, 2, QTableWidgetItem(dur_str))
            self.playlist_table.setItem(row, 3, QTableWidgetItem("Ready"))

        self.on_log_message(f"Playlist loaded: {playlist_title} — {len(entries)} videos", "INFO")

    def on_playlist_error(self, error_msg):
        """Called in main thread when playlist loading fails."""
        self.playlist_title_label.setText("Failed to load playlist.")
        self.on_log_message(f"Playlist error: {error_msg}", "ERROR")
        QMessageBox.critical(self, "Playlist Error", f"Could not load playlist:\n{error_msg}")

    def download_selected_playlist(self):
        selected_indexes = sorted(list(set([item.row() for item in self.playlist_table.selectedItems()])))
        if not selected_indexes:
            QMessageBox.warning(self, "Warning", "No playlist items selected!")
            return
            
        for row in selected_indexes:
            if row < len(self.loaded_playlist_items):
                item = self.loaded_playlist_items[row]
                self.manager.add_to_queue(
                    url=item['url'],
                    title=item['title'],
                    format_id=item['format_id'],
                    format_name=item['format_name']
                )
                self.playlist_table.setItem(row, 3, QTableWidgetItem("Queued"))
                
        self.sub_tabs.setCurrentWidget(self.active_tab)
        QMessageBox.information(self, "Success", f"Added {len(selected_indexes)} videos to the download queue!")

    def download_all_playlist(self):
        if not self.loaded_playlist_items:
            QMessageBox.warning(self, "Warning", "No playlist loaded!")
            return
            
        for row in range(self.playlist_table.rowCount()):
            item = self.loaded_playlist_items[row]
            self.manager.add_to_queue(
                url=item['url'],
                title=item['title'],
                format_id=item['format_id'],
                format_name=item['format_name']
            )
            self.playlist_table.setItem(row, 3, QTableWidgetItem("Queued"))
            
        self.sub_tabs.setCurrentWidget(self.active_tab)
        QMessageBox.information(self, "Success", f"Added all {len(self.loaded_playlist_items)} videos to download queue!")

    def select_playlist_range(self):
        if not self.loaded_playlist_items:
            QMessageBox.warning(self, "Warning", "No playlist loaded!")
            return
            
        from PyQt5.QtWidgets import QInputDialog
        start, ok1 = QInputDialog.getInt(self, "Select Range", "Start Video Number:", 1, 1, len(self.loaded_playlist_items))
        if not ok1:
            return
        end, ok2 = QInputDialog.getInt(self, "Select Range", "End Video Number:", len(self.loaded_playlist_items), start, len(self.loaded_playlist_items))
        if not ok2:
            return
            
        self.playlist_table.clearSelection()
        for i in range(start - 1, end):
            self.playlist_table.selectRow(i)

    def refresh_history_ui(self):
        self.history_table.setRowCount(0)
        for idx, entry in enumerate(self.manager.download_history):
            self.history_table.insertRow(idx)
            self.history_table.setItem(idx, 0, QTableWidgetItem(entry.get('date', '')))
            self.history_table.setItem(idx, 1, QTableWidgetItem(entry.get('title', '')))
            self.history_table.setItem(idx, 2, QTableWidgetItem(entry.get('format', '')))
            self.history_table.setItem(idx, 3, QTableWidgetItem(entry.get('size', '')))
            self.history_table.setItem(idx, 4, QTableWidgetItem(entry.get('status', '')))
            self.history_table.setItem(idx, 5, QTableWidgetItem(entry.get('file_path', '')))

    def on_history_double_clicked(self, item):
        self.open_selected_history_file()

    def open_selected_history_file(self):
        selected = self.history_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        file_path = self.history_table.item(row, 5).text()
        if file_path and os.path.exists(file_path):
            import subprocess
            folder = os.path.dirname(os.path.abspath(file_path))
            if sys.platform == 'win32':
                os.startfile(folder)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', folder])
            else:
                subprocess.Popen(['xdg-open', folder])
        else:
            QMessageBox.warning(self, "Warning", "File or folder does not exist.")

    def redownload_selected_history(self):
        selected = self.history_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        if row < len(self.manager.download_history):
            entry = self.manager.download_history[row]
            url = entry.get('url')
            if url:
                self.manager.add_to_queue(
                    url=url,
                    title=entry.get('title', 'Unknown'),
                    format_id='best',
                    format_name=entry.get('format', 'Best Quality')
                )
                self.sub_tabs.setCurrentWidget(self.active_tab)
                QMessageBox.information(self, "Success", f"Re-added {entry.get('title')} to download queue.")
            else:
                QMessageBox.warning(self, "Error", "No URL stored for this history entry.")

    def export_history_csv(self):
        file_path, _ = QFileDialog.getSaveFileName(self, "Export CSV", "", "CSV Files (*.csv)")
        if file_path:
            try:
                with open(file_path, 'w', newline='', encoding='utf-8') as f:
                    if self.manager.download_history:
                        writer = csv.DictWriter(f, fieldnames=self.manager.download_history[0].keys())
                        writer.writeheader()
                        writer.writerows(self.manager.download_history)
                QMessageBox.information(self, "Success", f"History exported successfully!")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export: {str(e)}")

    def clear_history(self):
        if QMessageBox.question(self, "Confirm", "Clear all download history?", QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.manager.download_history.clear()
            self.manager.save_history()
            self.refresh_history_ui()
            self.on_log_message("History cleared", "INFO")

    def refresh_settings_ui(self):
        c = self.manager.config
        self.path_edit.setText(c.get('download_path', ''))
        self.max_concurrent_spin.setValue(c.get('max_concurrent', 3))
        self.concurrent_fragments_spin.setValue(c.get('concurrent_fragments', 5))
        self.use_external_downloader_cb.setChecked(c.get('use_external_downloader', True))
        self.external_downloader_threads_spin.setValue(c.get('external_downloader_threads', 8))
        self.template_edit.setText(c.get('output_template', '%(title)s.%(ext)s'))
        
        self.speed_limit_cb.setChecked(c.get('limit_speed', False))
        self.speed_limit_edit.setText(c.get('speed_limit', '0'))
        
        self.extract_audio_cb.setChecked(c.get('extract_audio', False))
        self.audio_format_combo.setCurrentText(c.get('audio_format', 'mp3'))
        self.audio_quality_combo.setCurrentText(c.get('audio_quality', '192'))
        
        self.embed_thumb_cb.setChecked(c.get('embed_thumbnail', True))
        self.embed_subs_cb.setChecked(c.get('embed_subtitles', False))
        
        self.proxy_edit.setText(c.get('proxy', ''))
        self.cookies_edit.setText(c.get('cookies_file', ''))

    def save_settings(self):
        c = self.manager.config
        c['download_path'] = self.path_edit.text().strip()
        c['max_concurrent'] = self.max_concurrent_spin.value()
        c['concurrent_fragments'] = self.concurrent_fragments_spin.value()
        c['use_external_downloader'] = self.use_external_downloader_cb.isChecked()
        c['external_downloader_threads'] = self.external_downloader_threads_spin.value()
        c['output_template'] = self.template_edit.text().strip()
        
        c['limit_speed'] = self.speed_limit_cb.isChecked()
        c['speed_limit'] = self.speed_limit_edit.text().strip()
        
        c['extract_audio'] = self.extract_audio_cb.isChecked()
        c['audio_format'] = self.audio_format_combo.currentText()
        c['audio_quality'] = self.audio_quality_combo.currentText()
        
        c['embed_thumbnail'] = self.embed_thumb_cb.isChecked()
        c['embed_subtitles'] = self.embed_subs_cb.isChecked()
        
        c['proxy'] = self.proxy_edit.text().strip()
        c['cookies_file'] = self.cookies_edit.text().strip()
        
        self.manager.save_config()
        QMessageBox.information(self, "Success", "Settings saved successfully!")

    def browse_download_path(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Download Directory", self.path_edit.text())
        if folder:
            self.path_edit.setText(folder)

    def browse_cookies_path(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Cookies File", "", "Text Files (*.txt);;All Files (*)")
        if file_path:
            self.cookies_edit.setText(file_path)

    def save_console_log(self):
        file_path, _ = QFileDialog.getSaveFileName(self, "Save Console Log", "", "Text Files (*.txt);;Log Files (*.log)")
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(self.console_text.toPlainText())
                QMessageBox.information(self, "Success", "Log saved successfully!")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save log: {str(e)}")

    def toggle_pause_all(self):
        self.manager.paused = not self.manager.paused
        if self.manager.paused:
            self.pause_btn.setText("▶️ Resume All")
            self.on_log_message("Downloads paused", "WARNING")
        else:
            self.pause_btn.setText("⏸️ Pause All")
            self.on_log_message("Downloads resumed", "INFO")
            self.manager.process_queue()

    def remove_selected_active(self):
        selected = self.active_table.selectedItems()
        if selected:
            row = selected[0].row()
            download_id = self.active_table.item(row, 0).text()
            title = self.active_table.item(row, 1).text()
            
            with self.manager.lock:
                if download_id in self.manager.active_downloads:
                    del self.manager.active_downloads[download_id]
            self.active_table.removeRow(row)
            self.on_log_message(f"Removed active download: {title}", "INFO")

    def retry_failed(self):
        failed_items = []
        with self.manager.lock:
            for dl_id, info in list(self.manager.active_downloads.items()):
                if info.get('status') == 'error':
                    failed_items.append(info['item'])
                    del self.manager.active_downloads[dl_id]
                    
        # Remove from table
        row = 0
        while row < self.active_table.rowCount():
            status_item = self.active_table.item(row, 7)
            if status_item and "Error" in status_item.text():
                self.active_table.removeRow(row)
            else:
                row += 1
                
        if failed_items:
            for item in failed_items:
                self.manager.add_to_queue(item['url'], item['title'], item['format_id'], item['format_name'])
            QMessageBox.information(self, "Success", f"Re-added {len(failed_items)} failed items to download queue.")

    def clear_completed_active(self):
        # Clear completed rows from active table
        row = 0
        while row < self.active_table.rowCount():
            status_item = self.active_table.item(row, 7)
            # Active completes aren't in active table if completed cleanly, but if they are there:
            if status_item and "Completed" in status_item.text():
                self.active_table.removeRow(row)
            else:
                row += 1

    def stop_all(self):
        if QMessageBox.question(self, "Confirm", "Stop all active downloads and clear queue?", QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            with self.manager.lock:
                self.manager.download_queue.clear()
                self.manager.active_downloads.clear()
            self.active_table.setRowCount(0)
            self.queue_list.clear()
            self.on_log_message("All downloads stopped and queue cleared", "WARNING")

    def move_queue_up(self):
        selected_row = self.queue_list.currentRow()
        if selected_row > 0:
            with self.manager.lock:
                q = self.manager.download_queue
                q[selected_row], q[selected_row - 1] = q[selected_row - 1], q[selected_row]
            self.on_queue_updated()
            self.queue_list.setCurrentRow(selected_row - 1)

    def move_queue_down(self):
        selected_row = self.queue_list.currentRow()
        with self.manager.lock:
            q_len = len(self.manager.download_queue)
        if 0 <= selected_row < q_len - 1:
            with self.manager.lock:
                q = self.manager.download_queue
                q[selected_row], q[selected_row + 1] = q[selected_row + 1], q[selected_row]
            self.on_queue_updated()
            self.queue_list.setCurrentRow(selected_row + 1)

    def remove_selected_queue(self):
        selected_row = self.queue_list.currentRow()
        if selected_row >= 0:
            with self.manager.lock:
                removed = self.manager.download_queue.pop(selected_row)
            self.on_queue_updated()
            self.on_log_message(f"Removed from queue: {removed['title']}", "INFO")

    def clear_queue(self):
        if QMessageBox.question(self, "Confirm", "Clear the entire download queue?", QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            with self.manager.lock:
                self.manager.download_queue.clear()
            self.on_queue_updated()
            self.on_log_message("Queue cleared", "INFO")
