import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import time
import subprocess
import datetime
import platform
import shutil
from collections import defaultdict, Counter

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLineEdit, QLabel, 
                             QTreeWidget, QTreeWidgetItem, QProgressBar, 
                             QComboBox, QFileDialog, QMessageBox, QHeaderView,
                             QStackedWidget, QTreeView, QMenu)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QUrl, QDir
from PyQt6.QtGui import QAction, QFileSystemModel, QCursor, QGuiApplication

from fast_file_search.search import SearchEngine

# 1. LOGIC LAYER: IR INDEXER (Now supports Folders & Frequency Sorting)


class IndexerWorker(QThread):
    progress_update = pyqtSignal(int)
    finished_indexing = pyqtSignal(int, float)
    error_occurred = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.root_path = ""
        self.doc_store = {}
        self.inverted_index = defaultdict(set)
        self.extension_counts = Counter() # Track frequency
        self.is_running = False

    def prepare(self, path):
        self.root_path = path

    def generate_trigrams(self, text):
        text = text.lower()
        if len(text) < 3: return {text}
        return {text[i:i+3] for i in range(len(text) - 2)}

    def format_size(self, size_bytes):
        if size_bytes is None: return "--"
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0: return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} TB"

    def index_item(self, name, full_path, count, is_folder=False):
        """Helper to index a single item (file or folder)"""
        try:
            stats = os.stat(full_path)
            doc_id = count
            
            if is_folder:
                ext = "Folder"
                size_str = "--"
            else:
                ext = os.path.splitext(name)[1].lower()
                if not ext: ext = "File"
                size_str = self.format_size(stats.st_size)

            # Update Extension Frequency
            self.extension_counts[ext] += 1
            
            self.doc_store[doc_id] = {
                'name': name,
                'path': full_path,
                'ext': ext,
                'size': size_str,
                'date': datetime.datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M'),
                'lower_name': name.lower(),
                'is_folder': is_folder
            }

            # Trigram Indexing
            trigrams = self.generate_trigrams(name)
            for gram in trigrams:
                self.inverted_index[gram].add(doc_id)
                
            return True
        except (PermissionError, OSError):
            return False

    def run(self):
        self.is_running = True
        self.doc_store = {}
        self.inverted_index = defaultdict(set)
        self.extension_counts = Counter()
        
        count = 0
        start_time = time.time()
        
        try:
            for root, dirs, files in os.walk(self.root_path):
                if not self.is_running: break

                # 1. Index Folders
                for d_name in dirs:
                    full_path = os.path.join(root, d_name)
                    if self.index_item(d_name, full_path, count, is_folder=True):
                        count += 1

                # 2. Index Files
                for f_name in files:
                    full_path = os.path.join(root, f_name)
                    if self.index_item(f_name, full_path, count, is_folder=False):
                        count += 1
                        
                if count % 500 == 0:
                    self.progress_update.emit(count)

        except Exception as e:
            self.error_occurred.emit(str(e))
            return

        duration = time.time() - start_time
        self.finished_indexing.emit(count, duration)

    def search_index(self, query, extension_filter=None):
        if not query: return []
        query = query.lower()

        # IR Search Logic
        trigrams = self.generate_trigrams(query)
        if not trigrams: return []
        
        # Sort trigrams by rarity
        sorted_grams = sorted(trigrams, key=lambda t: len(self.inverted_index.get(t, [])))
        
        # Intersection
        candidate_ids = self.inverted_index.get(sorted_grams[0], set()).copy()
        for gram in sorted_grams[1:]:
            if not candidate_ids: break
            candidate_ids &= self.inverted_index.get(gram, set())

        results = []
        for doc_id in candidate_ids:
            doc = self.doc_store[doc_id]
            
            # Filter Logic
            if extension_filter and extension_filter != "All Types":
                # Special handling for Folder filter if needed, or exact ext match
                if doc['ext'] != extension_filter: continue
            
            # Verification
            if query in doc['lower_name']:
                results.append(doc)

        # Ranking: Exact matches first, then shortest names
        results.sort(key=lambda x: (len(x['name']), x['name']))
        return results

    def get_sorted_extensions(self):
        # Sort by Count (Descending), then Alphabetical
        # Returns list of strings like [".pdf", ".txt", "Folder"]
        sorted_exts = sorted(self.extension_counts.items(), key=lambda item: (-item[1], item[0]))
        return [item[0] for item in sorted_exts]


# 2. UI LAYER: PRO UX FEATURES


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        
        self.setWindowTitle("Fast File Search & Explorer Pro")
        self.resize(1100, 750)
        
        # Logic
        self.indexer = IndexerWorker()
        self.indexer.progress_update.connect(self.update_progress)
        self.indexer.finished_indexing.connect(self.indexing_finished)
        self.indexer.error_occurred.connect(self.show_error)
        
        self.current_folder = None

        # --- LAYOUT ---
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.layout = QVBoxLayout(central_widget)
        self.layout.setContentsMargins(15, 15, 15, 15)
        self.layout.setSpacing(10)

        # 1. Header
        header = QHBoxLayout()
        self.btn_select = QPushButton("Select Folder")
        self.btn_select.setMinimumHeight(35)
        self.btn_select.setStyleSheet("""
            QPushButton { background-color: #007AFF; color: white; font-weight: bold; border-radius: 5px; padding: 0 15px;}
            QPushButton:hover { background-color: #0056b3; }
        """)
        self.btn_select.clicked.connect(self.select_folder)
        
        self.lbl_path = QLabel("No folder selected")
        self.lbl_path.setStyleSheet("color: #555; font-style: italic;")
        
        header.addWidget(self.btn_select)
        header.addWidget(self.lbl_path)
        header.addStretch()
        self.layout.addLayout(header)

        # 2. Search & Filter
        search_box = QHBoxLayout()
        self.input_search = QLineEdit()
        self.input_search.setPlaceholderText("Search files & folders...")
        self.input_search.setMinimumHeight(35)
        self.input_search.setStyleSheet("border: 1px solid #ccc; border-radius: 5px; padding: 5px; font-size: 13px;")
        self.input_search.textChanged.connect(self.on_search_text_change)
        
        self.combo_filter = QComboBox()
        self.combo_filter.addItems(["All Types"])
        self.combo_filter.setMinimumHeight(35)
        self.combo_filter.setMinimumWidth(150)
        self.combo_filter.currentTextChanged.connect(self.on_search_text_change)
        
        search_box.addWidget(QLabel("Search:"))
        search_box.addWidget(self.input_search)
        search_box.addWidget(self.combo_filter)
        self.layout.addLayout(search_box)

        # 3. Stacked Views
        self.stack = QStackedWidget()
        
        # VIEW 0: Empty Placeholder (Clean Start)
        self.empty_view = QLabel("Select a folder to view contents and start indexing.")
        self.empty_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_view.setStyleSheet("color: #888; font-size: 16px;")
        
        # VIEW 1: Browser (QTreeView)
        self.file_model = QFileSystemModel()
        self.file_model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot)
        self.browser_view = QTreeView()
        self.browser_view.setModel(self.file_model)
        self.browser_view.setAlternatingRowColors(True)
        self.browser_view.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        
        # Context Menu for Browser
        self.browser_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.browser_view.customContextMenuRequested.connect(self.open_context_menu_browser)
        
        # VIEW 2: Search Results (QTreeWidget)
        self.search_view = QTreeWidget()
        self.search_view.setHeaderLabels(["Name", "Type", "Size", "Date Modified", "Full Path"])
        self.search_view.setAlternatingRowColors(True)
        self.search_view.setRootIsDecorated(False)
        self.search_view.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        
        # Context Menu for Search
        self.search_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_view.customContextMenuRequested.connect(self.open_context_menu_search)

        self.stack.addWidget(self.empty_view)   # Index 0
        self.stack.addWidget(self.browser_view) # Index 1
        self.stack.addWidget(self.search_view)  # Index 2
        
        self.layout.addWidget(self.stack)

        # 4. Status Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet("height: 4px;")
        self.layout.addWidget(self.progress_bar)
        
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #666; font-size: 11px;")
        self.layout.addWidget(self.status_label)

    # --- ACTIONS ---

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Index")
        if folder:
            self.current_folder = folder
            self.lbl_path.setText(folder)
            
            # Switch to Browser View
            self.stack.setCurrentIndex(1)
            
            # Set Root Path for Model AND View (Fixes the root drive issue)
            root_index = self.file_model.setRootPath(folder)
            self.browser_view.setRootIndex(root_index)
            
            self.start_indexing(folder)

    def start_indexing(self, folder):
        self.input_search.clear()
        self.btn_select.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0) 
        self.status_label.setText("Indexing files & folders...")
        
        self.indexer.prepare(folder)
        self.indexer.start()

    def update_progress(self, count):
        self.status_label.setText(f"Indexing... Found {count} items")

    def indexing_finished(self, count, duration):
        self.btn_select.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status_label.setText(f"Indexed {count} items in {duration:.2f}s")
        
        # Populate Filters (Sorted by Frequency)
        sorted_exts = self.indexer.get_sorted_extensions()
        self.combo_filter.clear()
        self.combo_filter.addItem("All Types")
        self.combo_filter.addItems(sorted_exts)

    def show_error(self, msg):
        self.progress_bar.setVisible(False)
        self.btn_select.setEnabled(True)
        QMessageBox.critical(self, "Error", msg)

    def on_search_text_change(self):
        query = self.input_search.text().strip()
        
        if not query:
            if self.current_folder:
                self.stack.setCurrentIndex(1) 
            else:
                self.stack.setCurrentIndex(0)
            return

        self.stack.setCurrentIndex(2)
        if not self.indexer.doc_store: return
        
        start = time.time()
        engine = SearchEngine()
        results = engine.search(
            query,
            self.indexer.doc_store,
            self.indexer.inverted_index,
            extension_filter=self.combo_filter.currentText(),
        )
        dur = (time.time() - start) * 1000
        
        self.search_view.clear()
        items = []
        for doc in results[:100]:
            item = QTreeWidgetItem([doc['name'], doc['ext'], doc['size'], doc['date'], doc['path']])
            item.setData(0, Qt.ItemDataRole.UserRole, doc['path'])
            items.append(item)
            
        self.search_view.addTopLevelItems(items)
        self.status_label.setText(f"Found {len(results)} matches in {dur:.2f} ms")

    # --- CONTEXT MENUS (RIGHT CLICK) ---

    def open_context_menu_browser(self, position):
        index = self.browser_view.indexAt(position)
        if not index.isValid(): return
        path = self.file_model.filePath(index)
        self.show_context_menu(path, position, self.browser_view)

    def open_context_menu_search(self, position):
        item = self.search_view.itemAt(position)
        if not item: return
        path = item.data(0, Qt.ItemDataRole.UserRole)
        self.show_context_menu(path, position, self.search_view)

    def show_context_menu(self, path, position, parent_widget):
        menu = QMenu()
        
        # 1. Copy Path
        action_copy = QAction("Copy File Path", self)
        action_copy.triggered.connect(lambda: self.copy_to_clipboard(path))
        menu.addAction(action_copy)
        
        # 2. Reveal
        action_reveal = QAction("Reveal in File Manager", self)
        action_reveal.triggered.connect(lambda: self.reveal_file(path))
        menu.addAction(action_reveal)
        
        menu.exec(parent_widget.mapToGlobal(position))

    def copy_to_clipboard(self, path):
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(path)
        self.status_label.setText("Path copied to clipboard")

    def reveal_file(self, path):
        if not os.path.exists(path): return
        
        system_name = platform.system()
        try:
            if system_name == 'Darwin': # macOS
                subprocess.call(['open', '-R', path])
            elif system_name == 'Windows':
                subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
            else: # Linux
                subprocess.call(['xdg-open', os.path.dirname(path)])
        except Exception as e:
            print(f"Error revealing: {e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())