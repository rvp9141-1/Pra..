import json
import os
import queue
import socket
import struct
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox

import cv2
from PIL import Image, ImageTk


# INS scene lifecycle message IDs
SCENE_START = 1001
SCENE_STOP = 1002

# Prevent invalid frames from requesting too much memory.
MAX_MESSAGE_SIZE = 16 * 1024 * 1024

APP_FOLDER = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_FOLDER, "config.json")


def load_config():
    """Read settings from config.json beside this file."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as config_file:
        config = json.load(config_file)

    required = ("server_ip", "server_port", "camera_source")
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"Missing config settings: {', '.join(missing)}")

    config.setdefault("record_folder", "recordings")
    config.setdefault("snapshot_folder", "snapshots")
    return config


def read_exact(sock, count):
    """Read exactly count bytes from a TCP socket."""
    data = bytearray()

    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("INS disconnected")
        data.extend(chunk)

    return bytes(data)


class CCTVApp:
    BG = "#101820"
    PANEL = "#192A35"
    TEXT = "#FFFFFF"
    MUTED = "#AAB7C4"
    GREEN = "#00D084"
    RED = "#FF4D4D"
    AMBER = "#F0B429"

    def __init__(self, root, config):
        self.root = root
        self.config = config

        # Camera source can be 0 for a local webcam or an RTSP URL.
        self.camera_source = config["camera_source"]

        # Save recordings and snapshots inside the project folder by default.
        self.record_folder = os.path.join(APP_FOLDER, config["record_folder"])
        self.snapshot_folder = os.path.join(APP_FOLDER, config["snapshot_folder"])
        os.makedirs(self.record_folder, exist_ok=True)
        os.makedirs(self.snapshot_folder, exist_ok=True)

        # Camera state.
        self.cap = None
        self.camera_running = False
        self.recording = False
        self.writer = None
        self.frame = None
        self.frame_lock = threading.Lock()
        self.record_start = None

        # INS listener state, separate from camera_running.
        self.client_open = True
        self.events = queue.Queue()

        # Tkinter image references must be retained.
        self.video_photo = None

        self.root.title("CCTV Surveillance System")
        self.root.geometry("1250x780")
        self.root.minsize(900, 600)
        self.root.configure(bg=self.BG)

        self.create_ui()
        self.update_clock()
        self.process_ins_events()

        self.root.protocol("WM_DELETE_WINDOW", self.close_app)

    # ---------------- USER INTERFACE ----------------

    def create_ui(self):
        # Header stays visible on both screens.
        header = tk.Frame(self.root, bg=self.PANEL, height=65)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(
            header,
            text="◉ CCTV SURVEILLANCE",
            bg=self.PANEL,
            fg=self.GREEN,
            font=("Arial", 20, "bold"),
        ).pack(side="left", padx=25)

        self.clock = tk.Label(
            header,
            text="",
            bg=self.PANEL,
            fg=self.TEXT,
            font=("Arial", 12, "bold"),
        )
        self.clock.pack(side="right", padx=25)

        # Shared area where the standby screen or camera screen is shown.
        self.main = tk.Frame(self.root, bg=self.BG)
        self.main.pack(fill="both", expand=True, padx=20, pady=20)

        # Standby display: shown at startup and after a scene stops.
        self.display_view = tk.Frame(self.main, bg="#080F1A")
        self.display_view.pack(fill="both", expand=True)

        tk.Label(
            self.display_view,
            text="◉",
            bg="#080F1A",
            fg="#315071",
            font=("Segoe UI", 68),
        ).pack(pady=(90, 12))

        tk.Label(
            self.display_view,
            text="CAMERA DISPLAY READY",
            bg="#080F1A",
            fg=self.TEXT,
            font=("Segoe UI", 20, "bold"),
        ).pack()

        tk.Label(
            self.display_view,
            text="The camera interface will appear when INS starts a scene.",
            bg="#080F1A",
            fg=self.MUTED,
            font=("Segoe UI", 11),
        ).pack(pady=8)

        # Camera interface: hidden until INS sends SCENE_START.
        self.camera_view = tk.Frame(self.main, bg=self.BG)

        # Camera information strip.
        info = tk.Frame(self.camera_view, bg="#20313D", height=60)
        info.pack(fill="x", pady=(0, 12))
        info.pack_propagate(False)

        tk.Label(
            info,
            text="CAMERA 01",
            bg="#20313D",
            fg=self.TEXT,
            font=("Arial", 14, "bold"),
        ).pack(side="left", padx=20)

        self.camera_status = tk.Label(
            info,
            text="● OFFLINE",
            bg="#20313D",
            fg=self.RED,
            font=("Arial", 11, "bold"),
        )
        self.camera_status.pack(side="right", padx=25)

        # Video display.
        video_container = tk.Frame(
            self.camera_view,
            bg="#253743",
            highlightbackground="#344955",
            highlightthickness=1,
        )
        video_container.pack(fill="both", expand=True)

        self.video_label = tk.Label(
            video_container,
            text="CAMERA OFFLINE\n\nNO VIDEO SIGNAL",
            bg="#080D12",
            fg=self.MUTED,
            font=("Arial", 22, "bold"),
        )
        self.video_label.pack(fill="both", expand=True, padx=5, pady=5)

        # Recording status and elapsed time.
        details = tk.Frame(self.camera_view, bg=self.PANEL, height=55)
        details.pack(fill="x", pady=12)
        details.pack_propagate(False)

        self.record_status = tk.Label(
            details,
            text="● RECORDING: STOPPED",
            bg=self.PANEL,
            fg=self.MUTED,
            font=("Arial", 11, "bold"),
        )
        self.record_status.pack(side="left", padx=20)

        self.duration = tk.Label(
            details,
            text="Duration: 00:00:00",
            bg=self.PANEL,
            fg=self.TEXT,
            font=("Arial", 11),
        )
        self.duration.pack(side="right", padx=20)

        # Camera controls.
        controls = tk.Frame(self.camera_view, bg=self.BG)
        controls.pack(fill="x", pady=3)

        self.button(controls, "START CAMERA", self.start_camera, "#087F5B")
        self.button(controls, "STOP CAMERA", self.stop_camera, "#B83232")
        self.button(controls, "START RECORDING", self.start_recording, "#087F5B")
        self.button(controls, "STOP RECORDING", self.stop_recording, "#B83232")
        self.button(controls, "SNAPSHOT", self.snapshot, "#344955")
        self.button(controls, "FULLSCREEN", self.fullscreen, "#344955")
        self.button(controls, "PLAYBACK", self.playback, "#344955")

        # Footer stays visible on both screens.
        self.footer = tk.Label(
            self.root,
            text=f"INS {self.config['server_ip']}:{self.config['server_port']}  |  CONNECTING",
            bg=self.PANEL,
            fg=self.AMBER,
            font=("Arial", 9),
        )
        self.footer.pack(fill="x", pady=5)

    def button(self, parent, text, command, color):
        tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg=self.TEXT,
            activebackground="#526B79",
            activeforeground=self.TEXT,
            font=("Arial", 9, "bold"),
            relief="flat",
            padx=12,
            pady=12,
        ).pack(side="left", padx=5)

    # ---------------- SCENE HANDLERS ----------------

    def on_scene_start(self):
        """INS called this client because a scene has started."""
        self.display_view.pack_forget()
        self.camera_view.pack(fill="both", expand=True)

        self.camera_status.config(text="● CONNECTING", fg=self.AMBER)
        self.start_camera()

    def on_scene_stop(self):
        """INS called this client because the scene has stopped."""
        self.stop_camera()

        self.camera_view.pack_forget()
        self.display_view.pack(fill="both", expand=True)

    # ---------------- INS CONNECTION ----------------

    def listen_to_ins(self):
        """Connect to INS and listen for scene start/stop messages."""
        host = str(self.config["server_ip"])
        port = int(self.config["server_port"])

        while self.client_open:
            try:
                with socket.create_connection((host, port), timeout=5) as sock:
                    sock.settimeout(None)
                    self.events.put(("connected", f"INS CONNECTED: {host}:{port}"))

                    while self.client_open:
                        # Read INS's 4-byte little-endian message length.
                        length_bytes = read_exact(sock, 4)
                        message_length = struct.unpack("<I", length_bytes)[0]

                        if message_length < 4 or message_length > MAX_MESSAGE_SIZE:
                            raise ValueError(
                                f"Invalid INS message length: {message_length}"
                            )

                        # Read the full message body.
                        packet = read_exact(sock, message_length)

                        # The first 4 bytes of the body are its message ID.
                        message_id = struct.unpack_from("<i", packet)[0]

                        if message_id == SCENE_START:
                            self.events.put(("scene_start", ""))
                        elif message_id == SCENE_STOP:
                            self.events.put(("scene_stop", ""))

            except (OSError, ConnectionError, ValueError) as error:
                if self.client_open:
                    self.events.put(("disconnected", str(error)))
                    threading.Event().wait(3)

    def process_ins_events(self):
        """Handle network events on Tkinter's UI thread."""
        if not self.client_open:
            return

        try:
            while True:
                event, detail = self.events.get_nowait()

                if event == "connected":
                    self.footer.config(text=f"{detail}  |  READY", fg=self.GREEN)

                elif event == "disconnected":
                    self.footer.config(
                        text=f"INS DISCONNECTED — RETRYING: {detail}",
                        fg=self.AMBER,
                    )

                elif event == "scene_start":
                    self.on_scene_start()

                elif event == "scene_stop":
                    self.on_scene_stop()

        except queue.Empty:
            pass

        self.root.after(100, self.process_ins_events)

    # ---------------- CAMERA ----------------

    def start_camera(self):
        """Open the configured camera source in a background thread."""
        if self.camera_running:
            return

        self.camera_status.config(text="● CONNECTING", fg=self.AMBER)

        def connect_camera():
            try:
                cap = cv2.VideoCapture(self.camera_source)

                if not cap.isOpened():
                    cap.release()
                    self.root.after(0, self.show_camera_error)
                    return

                self.cap = cap
                self.camera_running = True
                self.root.after(
                    0,
                    lambda: self.camera_status.config(text="● ONLINE", fg=self.GREEN),
                )
                self.read_camera()

            except Exception as error:
                self.root.after(
                    0,
                    lambda error=error: self.camera_status.config(
                        text=f"● CAMERA ERROR: {error}",
                        fg=self.RED,
                    ),
                )

        threading.Thread(target=connect_camera, daemon=True).start()

    def show_camera_error(self):
        self.camera_status.config(text="● OFFLINE", fg=self.RED)
        self.video_label.config(
            image="",
            text="CAMERA OFFLINE\n\nCHECK camera_source IN config.json",
        )

    def read_camera(self):
        """Continuously read frames and schedule their display."""
        while self.camera_running and self.cap is not None:
            ret, frame = self.cap.read()

            if not ret:
                break

            # Add a timestamp to the image.
            timestamp = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
            cv2.putText(
                frame,
                timestamp,
                (20, 35),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2,
            )

            with self.frame_lock:
                self.frame = frame.copy()

            # Save frames while recording is active.
            if self.recording and self.writer is not None:
                self.writer.write(frame)

            # Convert OpenCV BGR image to RGB for Tkinter.
            display_frame = cv2.resize(frame, (1000, 500))
            display_frame = cv2.cvtColor(display_frame, cv2.COLOR_BGR2RGB)
            photo = ImageTk.PhotoImage(Image.fromarray(display_frame))

            # Tkinter UI updates must happen on its main thread.
            self.root.after(0, lambda photo=photo: self.show_frame(photo))

            if self.recording:
                self.root.after(0, self.update_duration)

            time.sleep(0.01)

        # If the stream ends unexpectedly, clean up its state.
        if self.camera_running:
            self.camera_running = False

            if self.cap is not None:
                self.cap.release()
                self.cap = None

            self.root.after(
                0,
                lambda: self.camera_status.config(text="● OFFLINE", fg=self.RED),
            )

    def show_frame(self, photo):
        if self.camera_running:
            self.video_label.config(image=photo, text="")
            self.video_label.image = photo

    def stop_camera(self):
        """Stop camera and recording, then clear the displayed frame."""
        self.camera_running = False
        self.stop_recording()

        if self.cap is not None:
            self.cap.release()
            self.cap = None

        with self.frame_lock:
            self.frame = None

        self.video_label.config(
            image="",
            text="CAMERA OFFLINE\n\nNO VIDEO SIGNAL",
        )
        self.camera_status.config(text="● OFFLINE", fg=self.MUTED)

    # ---------------- RECORDING ----------------

    def start_recording(self):
        with self.frame_lock:
            current_frame = self.frame.copy() if self.frame is not None else None

        if not self.camera_running or current_frame is None:
            messagebox.showwarning("Camera", "Start the camera before recording.")
            return

        if self.recording:
            return

        height, width = current_frame.shape[:2]
        filename = datetime.now().strftime("CCTV_%Y%m%d_%H%M%S.avi")
        path = os.path.join(self.record_folder, filename)

        fourcc = cv2.VideoWriter_fourcc(*"XVID")
        self.writer = cv2.VideoWriter(path, fourcc, 20.0, (width, height))

        if not self.writer.isOpened():
            self.writer.release()
            self.writer = None
            messagebox.showerror("Recording", "Unable to create recording file.")
            return

        self.recording = True
        self.record_start = time.time()
        self.record_status.config(text="● RECORDING: ACTIVE", fg=self.GREEN)

    def stop_recording(self):
        self.recording = False

        if self.writer is not None:
            self.writer.release()
            self.writer = None

        if hasattr(self, "record_status"):
            self.record_status.config(text="● RECORDING: STOPPED", fg=self.MUTED)

        if hasattr(self, "duration"):
            self.duration.config(text="Duration: 00:00:00")

    def update_duration(self):
        if self.recording and self.record_start:
            elapsed = int(time.time() - self.record_start)
            hours = elapsed // 3600
            minutes = (elapsed % 3600) // 60
            seconds = elapsed % 60
            self.duration.config(
                text=f"Duration: {hours:02}:{minutes:02}:{seconds:02}"
            )

    # ---------------- SNAPSHOT ----------------

    def snapshot(self):
        with self.frame_lock:
            current_frame = self.frame.copy() if self.frame is not None else None

        if current_frame is None:
            messagebox.showwarning("Snapshot", "Camera is not connected.")
            return

        filename = datetime.now().strftime("Snapshot_%Y%m%d_%H%M%S.jpg")
        path = os.path.join(self.snapshot_folder, filename)

        if not cv2.imwrite(path, current_frame):
            messagebox.showerror("Snapshot", "Could not save snapshot.")
            return

        messagebox.showinfo("Snapshot", f"Snapshot saved:\n{path}")

    # ---------------- FULLSCREEN ----------------

    def fullscreen(self):
        with self.frame_lock:
            first_frame = self.frame.copy() if self.frame is not None else None

        if first_frame is None:
            messagebox.showinfo("Fullscreen", "Start the camera before using fullscreen.")
            return

        window = tk.Toplevel(self.root)
        window.configure(bg="black")
        window.attributes("-fullscreen", True)

        label = tk.Label(window, bg="black")
        label.pack(fill="both", expand=True)

        tk.Button(
            window,
            text="EXIT FULLSCREEN",
            command=window.destroy,
        ).place(relx=0.85, rely=0.03)

        def update_fullscreen():
            if not window.winfo_exists():
                return

            with self.frame_lock:
                frame = self.frame.copy() if self.frame is not None else None

            if frame is not None:
                size = (
                    max(1, window.winfo_width()),
                    max(1, window.winfo_height()),
                )
                frame = cv2.resize(frame, size)
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                photo = ImageTk.PhotoImage(Image.fromarray(frame))
                label.config(image=photo)
                label.image = photo

            window.after(40, update_fullscreen)

        update_fullscreen()

    # ---------------- PLAYBACK ----------------

    def playback(self):
        # This project is intended for Windows.
        os.startfile(os.path.abspath(self.record_folder))

    # ---------------- CLOCK ----------------

    def update_clock(self):
        self.clock.config(text=datetime.now().strftime("%d-%m-%Y    %H:%M:%S"))
        self.root.after(1000, self.update_clock)

    # ---------------- CLOSE APPLICATION ----------------

    def close_app(self):
        self.client_open = False
        self.camera_running = False
        self.stop_recording()

        if self.cap is not None:
            self.cap.release()
            self.cap = None

        self.root.destroy()


if __name__ == "__main__":
    try:
        config = load_config()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"Cannot load {CONFIG_PATH}: {error}")

    root = tk.Tk()
    app = CCTVApp(root, config)

    # Listen for INS scene messages without freezing the interface.
    threading.Thread(target=app.listen_to_ins, daemon=True).start()

    root.mainloop()