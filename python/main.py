import socket
import struct
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk

ESP_IP = "10.126.153.132"
ESP_PORT = 4210
SEND_PERIOD_MS = 50
STATE_FILE = Path(__file__).with_name("seq_state.txt")

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def load_seq_id() -> int:
    try:
        return int(STATE_FILE.read_text(encoding="utf-8").strip()) & 0xFFFF
    except (FileNotFoundError, ValueError):
        return int(time.time() * 1000) & 0xFFFF


def save_seq_id(value: int) -> None:
    STATE_FILE.write_text(f"{value & 0xFFFF}\n", encoding="utf-8")


def build_flags(da: bool, db: bool, sa: bool, sb: bool) -> int:
    return (
        (int(da) << 0)
        | (int(db) << 1)
        | (int(sa) << 2)
        | (int(sb) << 3)
    )


def send_to_car(seq_value: int, pwm_d: int, pwm_s: int, da: bool, db: bool, sa: bool, sb: bool) -> None:
    """Wysyla jeden pakiet UDP zgodny z RC_Command po stronie ESP32-S3.

    Format pakietu:
      <HBBB
      H = uint16 seq_id
      B = pwm_drive
      B = pwm_steer
      B = motor_flags
    """
    flags = build_flags(da, db, sa, sb)
    packet = struct.pack(
        "<HBBB",
        seq_value & 0xFFFF,
        pwm_d & 0xFF,
        pwm_s & 0xFF,
        flags & 0xFF,
    )
    sock.sendto(packet, (ESP_IP, ESP_PORT))


class MotorGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("RCDC-2026 Motor Control")
        self.root.geometry("420x340")
        self.root.resizable(False, False)

        self.seq_id = load_seq_id()
        self.running = True

        self.drive_a = tk.BooleanVar(value=False)
        self.drive_b = tk.BooleanVar(value=False)
        self.steer_a = tk.BooleanVar(value=False)
        self.steer_b = tk.BooleanVar(value=False)

        self.pwm_drive = tk.IntVar(value=255)
        self.pwm_steer = tk.IntVar(value=255)

        self.status_var = tk.StringVar(value=f"Start seq: {self.seq_id}")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(SEND_PERIOD_MS, self.send_loop)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill="both", expand=True)

        title = ttk.Label(main, text="Sterowanie silnikami", font=("Segoe UI", 14, "bold"))
        title.pack(anchor="w", pady=(0, 12))

        flags_frame = ttk.LabelFrame(main, text="Piny sterujące")
        flags_frame.pack(fill="x", pady=(0, 12))

        row1 = ttk.Frame(flags_frame)
        row1.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Checkbutton(row1, text="Drive A", variable=self.drive_a).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(row1, text="Drive B", variable=self.drive_b).pack(side="left", padx=(0, 16))

        row2 = ttk.Frame(flags_frame)
        row2.pack(fill="x", padx=12, pady=(4, 10))
        ttk.Checkbutton(row2, text="Steer A", variable=self.steer_a).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(row2, text="Steer B", variable=self.steer_b).pack(side="left", padx=(0, 16))

        pwm_frame = ttk.LabelFrame(main, text="PWM")
        pwm_frame.pack(fill="x", pady=(0, 12))

        drive_row = ttk.Frame(pwm_frame)
        drive_row.pack(fill="x", padx=12, pady=(10, 6))
        ttk.Label(drive_row, text="Drive PWM").pack(anchor="w")
        drive_scale = ttk.Scale(
            drive_row,
            from_=0,
            to=255,
            orient="horizontal",
            command=lambda value: self.pwm_drive.set(int(float(value))),
        )
        drive_scale.set(self.pwm_drive.get())
        drive_scale.pack(fill="x")
        ttk.Label(drive_row, textvariable=self.pwm_drive).pack(anchor="e")

        steer_row = ttk.Frame(pwm_frame)
        steer_row.pack(fill="x", padx=12, pady=(6, 10))
        ttk.Label(steer_row, text="Steer PWM").pack(anchor="w")
        steer_scale = ttk.Scale(
            steer_row,
            from_=0,
            to=255,
            orient="horizontal",
            command=lambda value: self.pwm_steer.set(int(float(value))),
        )
        steer_scale.set(self.pwm_steer.get())
        steer_scale.pack(fill="x")
        ttk.Label(steer_row, textvariable=self.pwm_steer).pack(anchor="e")

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", pady=(0, 10))
        ttk.Button(buttons, text="Stop", command=self.stop_all).pack(side="left")
        ttk.Button(buttons, text="Forward", command=self.forward).pack(side="left", padx=8)
        ttk.Button(buttons, text="Save seq", command=self.persist_seq).pack(side="right")

        status = ttk.Label(main, textvariable=self.status_var)
        status.pack(anchor="w", pady=(4, 0))

    def forward(self) -> None:
        self.drive_a.set(True)
        self.drive_b.set(False)
        self.steer_a.set(False)
        self.steer_b.set(False)
        self.pwm_drive.set(255)
        self.pwm_steer.set(255)

    def stop_all(self) -> None:
        self.drive_a.set(False)
        self.drive_b.set(False)
        self.steer_a.set(False)
        self.steer_b.set(False)
        self.pwm_drive.set(0)
        self.pwm_steer.set(0)

    def persist_seq(self) -> None:
        save_seq_id(self.seq_id)
        self.status_var.set(f"seq zapisany: {self.seq_id & 0xFFFF}")

    def send_loop(self) -> None:
        if not self.running:
            return

        try:
            send_to_car(
                self.seq_id,
                self.pwm_drive.get(),
                self.pwm_steer.get(),
                self.drive_a.get(),
                self.drive_b.get(),
                self.steer_a.get(),
                self.steer_b.get(),
            )
            flags = build_flags(
                self.drive_a.get(),
                self.drive_b.get(),
                self.steer_a.get(),
                self.steer_b.get(),
            )
            self.status_var.set(
                f"wyslano seq={self.seq_id & 0xFFFF} pwm=({self.pwm_drive.get()},{self.pwm_steer.get()}) flags=0b{flags:04b}"
            )
            self.seq_id = (self.seq_id + 1) & 0xFFFF
            save_seq_id(self.seq_id)
        except OSError as exc:
            self.status_var.set(f"blad UDP: {exc}")

        self.root.after(SEND_PERIOD_MS, self.send_loop)

    def on_close(self) -> None:
        self.running = False
        save_seq_id(self.seq_id)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MotorGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
