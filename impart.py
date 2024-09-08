import os
import sys
import socket
import threading
import json
import time
import ssl
import logging
from pathlib import Path
from typing import List, Tuple, Dict

import stun
from zeroconf import ServiceInfo, Zeroconf, ServiceBrowser, ServiceStateChange
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QPushButton, QFileDialog, QLabel, QListWidget, 
                             QInputDialog, QMessageBox, QProgressBar, QDialog,
                             QHBoxLayout, QGroupBox)
from PyQt5.QtCore import pyqtSignal, QObject, QThread
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPalette

# Constants
CHUNK_SIZE = 8192
# DOWNLOAD_FOLDER = Path('downloads')
# DOWNLOAD_FOLDER.mkdir(exist_ok=True)


# Find the default downloads folder and create an "Impart" subfolder

if os.name == 'nt':  # For Windows
    DOWNLOAD_FOLDER = Path(os.path.join(os.environ['USERPROFILE'], 'Downloads', 'Impart'))
else:  # For Linux/MacOS
    DOWNLOAD_FOLDER = Path(os.path.join(os.path.expanduser('~'), 'Downloads', 'Impart'))

# Ensure the folder exists
DOWNLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
SERVICE_TYPE = "_p2pfileshare._tcp.local."

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# Utility function to get local IP address
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connect to an external server to determine local IP
        s.connect(('8.8.8.8', 1))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = '127.0.0.1'  # Fallback to localhost
    finally:
        s.close()
    return local_ip



class FileTransferSignals(QObject):
    update_status = pyqtSignal(str)
    update_progress = pyqtSignal(int)
    update_file_list = pyqtSignal(list)
    update_peer_list = pyqtSignal(dict)
    file_transfer_request = pyqtSignal(str, str, str)  # sender_name, filename, filesize
    

class FileTransferWorker(QThread):
    def __init__(self, file_path: Path, peer_address: Tuple[str, int], is_sender: bool, sender_name: str):
        super().__init__()
        self.file_path = file_path
        self.peer_address = peer_address
        self.is_sender = is_sender
        self.sender_name = sender_name
        self.signals = FileTransferSignals()
        self.should_stop = False

    def run(self):
        try:
            if self.is_sender:
                self._connect_and_send()
            else:
                self._receive_file()
        except Exception as e:
            logger.error(f"Error during file transfer: {e}")
            self.signals.update_status.emit(f"Error: {str(e)}")

    def _connect_and_send(self):
        sock = socket.create_connection(self.peer_address)
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with context.wrap_socket(sock, server_hostname=self.peer_address[0]) as secure_sock:
            self._send_file(secure_sock)

    def _send_file(self, sock: ssl.SSLSocket):
        file_size = self.file_path.stat().st_size
        file_info = {
            "sender_name": self.sender_name,
            "filename": self.file_path.name,
            "filesize": file_size
        }
        sock.sendall(json.dumps(file_info).encode())

        # Wait for acceptance
        response = sock.recv(1024).decode()
        if response != "ACCEPT":
            self.signals.update_status.emit(f"File transfer rejected by receiver")
            return

        with self.file_path.open('rb') as f:
            bytes_sent = 0
            while chunk := f.read(CHUNK_SIZE):
                sock.sendall(chunk)
                bytes_sent += len(chunk)
                progress = int(bytes_sent / file_size * 100)
                self.signals.update_progress.emit(progress)

        self.signals.update_status.emit(f"Sent {self.file_path.name}")


    def _receive_file(self, sock: ssl.SSLSocket):
        file_info = json.loads(sock.recv(1024).decode())
        self.signals.file_transfer_request.emit(file_info['sender_name'], file_info['filename'], str(file_info['filesize']))

        # Wait for user acceptance (implemented in main app)
        # For now, we'll auto-accept
        sock.sendall(b"ACCEPT")

        file_path = DOWNLOAD_FOLDER / file_info['filename']
        file_size = file_info['filesize']

        with file_path.open('wb') as f:
            bytes_received = 0
            while bytes_received < file_size:
                chunk = sock.recv(min(CHUNK_SIZE, file_size - bytes_received))
                if not chunk:
                    break
                f.write(chunk)
                bytes_received += len(chunk)
                progress = int(bytes_received / file_size * 100)
                self.signals.update_progress.emit(progress)

        self.signals.update_status.emit(f"Received {file_path.name}")
        self.signals.update_file_list.emit(list(DOWNLOAD_FOLDER.glob('*')))

class P2PFileSharing:
    def __init__(self, port: int = 5000):
        super().__init__()
        self.port = port
        self.zeroconf = Zeroconf()
        self.browser = None
        self.service_info = None
        self.server_thread = None
        self.signals = FileTransferSignals()
        self.peers: Dict[str, Tuple[str, int]] = {}  # {name: (ip, port)}
        self.user_name = ""
        self.active_workers = []
        self.local_ip = self.get_local_ip()  # Get the local IP address

    def get_local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Connect to an external server to determine local IP
            s.connect(('8.8.8.8', 1))
            local_ip = s.getsockname()[0]
        except Exception:
            local_ip = '127.0.0.1'  # Fallback to localhost
        finally:
            s.close()
        return local_ip

    def _register_service(self):
        self.service_info = ServiceInfo(
            SERVICE_TYPE,
            f"{self.user_name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(self.local_ip)],  # Use the local IP
            port=self.port,
            properties={"user_name": self.user_name}
        )
        self.zeroconf.register_service(self.service_info)

    def _on_service_state_change(self, zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            info = zeroconf.get_service_info(service_type, name)
            if info:
                user_name = info.properties.get(b'user_name', b'').decode('utf-8')
                ip = socket.inet_ntoa(info.addresses[0])
                if ip != self.local_ip:  # Only add peers that are not the local machine
                    self.peers[user_name] = (ip, info.port)
                    self.signals.update_peer_list.emit(self.peers)
                    logger.info(f"New peer discovered: {user_name} at {ip}:{info.port}")

    

    def start(self, user_name: str):
        self.user_name = user_name
        self.server_thread = threading.Thread(target=self._run_server, daemon=True)
        self.server_thread.start()
        self._register_service()
        self._start_discovery()

    def stop(self):
        if self.service_info:
            self.zeroconf.unregister_service(self.service_info)
        if self.browser:
            self.browser.cancel()
        self.zeroconf.close()

    def _start_discovery(self):
        self.browser = ServiceBrowser(self.zeroconf, SERVICE_TYPE, handlers=[self._on_service_state_change])


    def _run_server(self):
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile="server.crt", keyfile="server.key")
        
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('0.0.0.0', self.port))
            sock.listen(5)
            with context.wrap_socket(sock, server_side=True) as secure_sock:
                while True:
                    try:
                        client, addr = secure_sock.accept()
                        threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()
                    except ssl.SSLError as e:
                        logger.error(f"SSL Error during client connection: {e}")
                    except Exception as e:
                        logger.error(f"Error accepting client connection: {e}")

    def _handle_client(self, client: ssl.SSLSocket):
        try:
            file_info = json.loads(client.recv(1024).decode())
            self.signals.file_transfer_request.emit(file_info['sender_name'], file_info['filename'], str(file_info['filesize']))

            # Wait for user acceptance (implemented in main app)
            # For now, we'll auto-accept
            client.sendall(b"ACCEPT")

            file_path = DOWNLOAD_FOLDER / file_info['filename']
            file_size = file_info['filesize']

            with file_path.open('wb') as f:
                bytes_received = 0
                while bytes_received < file_size:
                    chunk = client.recv(min(CHUNK_SIZE, file_size - bytes_received))
                    if not chunk:
                        break
                    f.write(chunk)
                    bytes_received += len(chunk)
                    progress = int(bytes_received / file_size * 100)
                    self.signals.update_progress.emit(progress)

            self.signals.update_status.emit(f"Received {file_path.name}")
            self.signals.update_file_list.emit(list(DOWNLOAD_FOLDER.glob('*')))
        except Exception as e:
            logger.error(f"Error handling client: {e}")
        finally:
            client.close()
    
    def send_file(self, file_path: Path, peer_address: Tuple[str, int], sender_name: str):
        worker = FileTransferWorker(file_path, peer_address, is_sender=True, sender_name=sender_name)
        worker.signals.update_status.connect(self.signals.update_status.emit)
        worker.signals.update_progress.connect(self.signals.update_progress.emit)
        self.active_workers.append(worker)
        worker.finished.connect(lambda: self.active_workers.remove(worker))
        worker.start()

    def stop(self):
        if self.service_info:
            self.zeroconf.unregister_service(self.service_info)
        if self.browser:
            self.browser.cancel()
        self.zeroconf.close()
        for worker in self.active_workers:
            worker.stop()
            worker.wait()

class FileTransferDialog(QDialog):
    def __init__(self, sender_name: str, filename: str, filesize: str):
        super().__init__()
        self.setWindowTitle("File Transfer Request")
        self.layout = QVBoxLayout()
        self.layout.addWidget(QLabel(f"{sender_name} wants to send you a file:"))
        self.layout.addWidget(QLabel(f"Filename: {filename}"))
        self.layout.addWidget(QLabel(f"Size: {filesize} bytes"))
        
        btn_layout = QHBoxLayout()
        accept_btn = QPushButton("Accept")
        accept_btn.clicked.connect(self.accept)
        reject_btn = QPushButton("Reject")
        reject_btn.clicked.connect(self.reject)
        btn_layout.addWidget(accept_btn)
        btn_layout.addWidget(reject_btn)
        
        self.layout.addLayout(btn_layout)
        self.setLayout(self.layout)

class P2PApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.p2p = P2PFileSharing()
        self.init_ui()

    def init_ui(self):
        # self.setWindowTitle('P2P File Sharing')
        # self.setGeometry(100, 100, 400, 300)

        # layout = QVBoxLayout()

        # self.status_label = QLabel('Ready')
        # layout.addWidget(self.status_label)

        # self.file_list = QListWidget()
        # layout.addWidget(self.file_list)

        # self.peer_list = QListWidget()
        # layout.addWidget(self.peer_list)

        # self.progress_bar = QProgressBar(self)
        # layout.addWidget(self.progress_bar)

        # send_button = QPushButton('Send Files')
        # send_button.clicked.connect(self.select_files)
        # layout.addWidget(send_button)

        # container = QWidget()
        # container.setLayout(layout)
        # self.setCentralWidget(container)

        self.setWindowTitle('P2P File Sharing 📁')
        self.setGeometry(100, 100, 400, 300)

        layout = QVBoxLayout()
        layout.setSpacing(20)
        layout.setContentsMargins(20, 20, 20, 20)

        self.status_label = QLabel('Ready 🔄')
        self.status_label.setStyleSheet("""
            color: #4CAF50; 
            font-weight: bold; 
            font-size: 16px;
        """)
        layout.addWidget(self.status_label)

        file_list_group = QGroupBox('File List 📝')
        file_list_group.setStyleSheet("""
            border: 1px solid #DDDDDD; 
            border-radius: 10px; 
            padding: 10px;
        """)
        file_list_layout = QVBoxLayout()
        file_list_layout.setSpacing(10)
        self.file_list = QListWidget()
        self.file_list.setStyleSheet("""
            border: none; 
            background-color: #F5F5F5;
            padding: 10px;
            border-radius: 10px;
        """)
        file_list_layout.addWidget(self.file_list)
        file_list_group.setLayout(file_list_layout)
        layout.addWidget(file_list_group)

        peer_list_group = QGroupBox('Peer List 👥')
        peer_list_group.setStyleSheet("""
            border: 1px solid #DDDDDD; 
            border-radius: 10px; 
            padding: 10px;
        """)
        peer_list_layout = QVBoxLayout()
        peer_list_layout.setSpacing(10)
        self.peer_list = QListWidget()
        self.peer_list.setStyleSheet("""
            border: none; 
            background-color: #F5F5F5;
            padding: 10px;
            border-radius: 10px;
        """)
        peer_list_layout.addWidget(self.peer_list)
        peer_list_group.setLayout(peer_list_layout)
        layout.addWidget(peer_list_group)

        layout.addSpacing(20)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setStyleSheet("""
            border-radius: 10px; 
            background-color: #F5F5F5; 
            padding: 2px;
            text-align: center;
        """)
        layout.addWidget(self.progress_bar)

        send_button = QPushButton('Send Files 📤')
        send_button.clicked.connect(self.select_files)
        send_button.setStyleSheet("""
            background-color: #4CAF50; 
            color: white; 
            padding: 10px 20px; 
            border-radius: 10px; 
            border: none;
        """)
        layout.addWidget(send_button)

        layout.addStretch()

        container = QWidget()
        container.setLayout(layout)
        container.setStyleSheet("""
            background-color: #F5F5F5;
        """)
        self.setCentralWidget(container)


        self.p2p.signals.update_status.connect(self.update_status)
        self.p2p.signals.update_progress.connect(self.update_progress)
        self.p2p.signals.update_file_list.connect(self.update_file_list)
        self.p2p.signals.update_peer_list.connect(self.update_peer_list)
        self.p2p.signals.file_transfer_request.connect(self.show_file_transfer_dialog)

        self.get_user_name()

    def get_user_name(self):
        name, ok = QInputDialog.getText(self, "User Identification", "Enter your name:")
        if ok and name:
            self.p2p.start(name)
        else:
            QMessageBox.warning(self, "Invalid Name", "Please enter a valid name.")
            self.close()

    def select_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Files to Send")
        if files:
            selected_peer = self.peer_list.currentItem()
            if selected_peer:
                peer_name = selected_peer.text()
                peer_address = self.p2p.peers[peer_name]

                list_widget = self.peer_list
                items = [list_widget.item(i).text() for i in range(list_widget.count())]
                print(items, self.p2p.peers)
                     
                for file in files:
                    self.send_file(Path(file), peer_address, self.p2p.user_name)
            else:
                QMessageBox.warning(self, "No Peer Selected", "Please select a peer to send the file to.")

    def update_status(self, message: str):
        self.status_label.setText(message)

    def update_progress(self, value: int):
        self.progress_bar.setValue(value)

    def update_file_list(self, files: List[Path]):
        self.file_list.clear()
        for file in files:
            self.file_list.addItem(file.name)

    def update_peer_list(self, peers: Dict[str, Tuple[str, int]]):
        self.peer_list.clear()
        for peer_name, (peer_ip, peer_port) in peers.items():
            if peer_ip != self.p2p.local_ip:
                self.peer_list.addItem(f"{peer_name}")

    def show_file_transfer_dialog(self, sender_name: str, filename: str, filesize: str):
        dialog = FileTransferDialog(sender_name, filename, filesize)
        if dialog.exec_():
            self.update_status(f"Accepting file {filename} from {sender_name}")
        else:
            self.update_status(f"Rejected file {filename} from {sender_name}")

    def send_file(self, file_path: Path, peer_address: Tuple[str, int], sender_name: str):
        # Ensure the peer is not the local machine
        local_ip = get_local_ip()
        peer_ip, peer_port = peer_address
        if peer_ip == local_ip:
            self.update_status("Cannot send file to the same device.")
            return
        self.p2p.send_file(file_path, peer_address, sender_name)

    def closeEvent(self, event):
        self.p2p.stop()
        super().closeEvent(event)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = P2PApp()
    window.show()
    sys.exit(app.exec_())