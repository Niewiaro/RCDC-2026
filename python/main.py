import asyncio
import socket
import struct
import time
from pathlib import Path

from dualsense_controller import DualSenseController

# ---------------------------------------------------------------------------
# Konfiguracja
# ---------------------------------------------------------------------------
ESP_IP = "10.126.153.132"
ESP_PORT = 4210
SEND_PERIOD_S = 0.05  # 50 ms → 20 Hz
STATE_FILE = Path(__file__).with_name("seq_state.txt")

RUMBLE_OFF = 0
RUMBLE_ON = 200
SENSITIVITY = 0.65  # bazowa czułość skrętu

# ---------------------------------------------------------------------------
# Format pakietu UDP  (little-endian, 5 bajtów)
#   H  seq_id       uint16
#   B  pwm_drive    0-255   → PWM lewego koła
#   B  pwm_steer    0-255   → PWM prawego koła
#   B  motor_flags  bitmask:
#        bit 0  da  in1_left   (lewy naprzód)
#        bit 1  db  in2_left   (lewy wstecz)
#        bit 2  sa  in3_right  (prawy naprzód)
#        bit 3  sb  in4_right  (prawy wstecz)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sekwencja pakietów – trwałość między uruchomieniami
# ---------------------------------------------------------------------------
def load_seq_id() -> int:
    try:
        return int(STATE_FILE.read_text(encoding="utf-8").strip()) & 0xFFFF
    except (FileNotFoundError, ValueError):
        return int(time.time() * 1000) & 0xFFFF


def save_seq_id(value: int) -> None:
    STATE_FILE.write_text(f"{value & 0xFFFF}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Globalny stan kontrolera
# ---------------------------------------------------------------------------
is_running = True
data_changed = False

left_trigger: float = 0.0
right_trigger: float = 0.0
left_stick_x: float = 0.0
button_cross: bool = False


# ---------------------------------------------------------------------------
# Logika czołgowa → parametry silników
# ---------------------------------------------------------------------------
def _clamp_pwm(value: float) -> int:
    return min(int(abs(value)), 255)


def compute_motor_params() -> tuple[int, bool, bool, int, bool, bool]:
    """
    Zwraca (pwm_left, in1_L, in2_L, pwm_right, in1_R, in2_R).

    Mapowanie na pola pakietu:
      pwm_left  → pwm_drive
      pwm_right → pwm_steer
      in1_L     → da  (bit 0)
      in2_L     → db  (bit 1)
      in1_R     → sa  (bit 2)
      in2_R     → sb  (bit 3)

    Kierunki:
      in1=True, in2=False  → silnik DO PRZODU
      in1=False, in2=True  → silnik DO TYŁU
      in1=True, in2=True   → hamowanie
      in1=False, in2=False → wybieg (brak zasilania)
    """
    # Hamowanie awaryjne krzyżykiem
    if button_cross:
        return 255, True, True, 255, True, True

    raw_l = 0.0
    raw_r = 0.0
    sensitivity = SENSITIVITY

    # Gaz
    if left_trigger > 0.0:
        sensitivity = SENSITIVITY * left_trigger
        raw_l += left_trigger * 255
        raw_r += left_trigger * 255
    elif right_trigger > 0.0:
        sensitivity = SENSITIVITY * right_trigger
        raw_l -= right_trigger * 255
        raw_r -= right_trigger * 255

    # Skręt przez różnicowanie prędkości kół
    # stick_x > 0 → skręt w prawo: lewe koło szybciej, prawe wolniej
    steer = left_stick_x * 255 * (sensitivity + 0.2)
    raw_l += steer
    raw_r -= steer

    pwm_l = _clamp_pwm(raw_l)
    pwm_r = _clamp_pwm(raw_r)

    in1_l = raw_l > 0
    in2_l = raw_l < 0
    in1_r = raw_r > 0
    in2_r = raw_r < 0

    return pwm_l, in1_l, in2_l, pwm_r, in1_r, in2_r


# ---------------------------------------------------------------------------
# UDP
# ---------------------------------------------------------------------------
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def build_flags(da: bool, db: bool, sa: bool, sb: bool) -> int:
    return (int(da) << 0) | (int(db) << 1) | (int(sa) << 2) | (int(sb) << 3)


def send_to_car(
    seq_value: int, pwm_d: int, pwm_s: int, da: bool, db: bool, sa: bool, sb: bool
) -> None:
    """Wysyła pakiet UDP zgodny z RC_Command po stronie ESP32-S3.

    Format pakietu:
      <HBBB
      H = uint16 seq_id
      B = pwm_drive  (PWM lewego koła)
      B = pwm_steer  (PWM prawego koła)
      B = motor_flags (da=in1_L, db=in2_L, sa=in1_R, sb=in2_R)
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


# ---------------------------------------------------------------------------
# Kontroler DualSense
# ---------------------------------------------------------------------------
device_infos = DualSenseController.enumerate_devices()
if not device_infos:
    raise RuntimeError("Brak kontrolera DualSense!")

controller = DualSenseController()


def _stop() -> None:
    global is_running
    is_running = False


# --- Rumble & lightbar ---


def _update_feedback() -> None:
    moving = left_trigger > 0.0 or right_trigger > 0.0
    turning_hard = abs(left_stick_x) >= 0.9

    if moving or turning_hard or button_cross:
        controller.left_rumble.set(RUMBLE_ON)
        controller.right_rumble.set(RUMBLE_ON)
    else:
        controller.left_rumble.set(RUMBLE_OFF)
        controller.right_rumble.set(RUMBLE_OFF)

    if button_cross:
        controller.lightbar.set_color_red()
    elif left_trigger > 0.0:
        controller.lightbar.set_color_green()
    elif right_trigger > 0.0:
        controller.lightbar.set_color_red()
    elif abs(left_stick_x) > 0.05:
        controller.lightbar.set_color_white()
    else:
        controller.lightbar.set_color_blue()


# --- Callbacki ---


def on_left_trigger(value: float) -> None:
    global data_changed, left_trigger
    left_trigger = value
    data_changed = True
    _update_feedback()


def on_right_trigger(value: float) -> None:
    global data_changed, right_trigger
    right_trigger = value
    data_changed = True
    _update_feedback()


def on_left_stick_x(value: float) -> None:
    global data_changed, left_stick_x
    left_stick_x = value
    data_changed = True
    _update_feedback()


def on_cross_down() -> None:
    global data_changed, button_cross
    button_cross = True
    data_changed = True
    _update_feedback()


def on_cross_up() -> None:
    global data_changed, button_cross
    button_cross = False
    data_changed = True
    _update_feedback()


def on_ps_down() -> None:
    print("PS → zatrzymanie programu")
    _stop()


def on_error(error) -> None:
    print(f"Błąd kontrolera: {error}")
    _stop()


# Rejestracja callbacków
controller.left_trigger.on_change(on_left_trigger)
controller.right_trigger.on_change(on_right_trigger)
controller.left_stick_x.on_change(on_left_stick_x)
controller.btn_cross.on_down(on_cross_down)
controller.btn_cross.on_up(on_cross_up)
controller.btn_ps.on_down(on_ps_down)
controller.on_error(on_error)


# ---------------------------------------------------------------------------
# Pętla główna
# ---------------------------------------------------------------------------
async def main() -> None:
    global is_running, data_changed

    seq_id = load_seq_id()

    print(f"Startujemy z seq_id={seq_id}")
    print("Podłączanie kontrolera DualSense...")
    controller.activate()
    controller.lightbar.set_color_blue()
    print("DualSense aktywny!")

    try:
        while is_running:
            if data_changed:
                pwm_l, in1_l, in2_l, pwm_r, in1_r, in2_r = compute_motor_params()
                send_to_car(seq_id, pwm_l, pwm_r, in1_l, in2_l, in1_r, in2_r)
                flags = build_flags(in1_l, in2_l, in1_r, in2_r)
                print(
                    f"seq={seq_id:05d}  "
                    f"L: pwm={pwm_l:3d} fwd={int(in1_l)} rev={int(in2_l)}  "
                    f"R: pwm={pwm_r:3d} fwd={int(in1_r)} rev={int(in2_r)}  "
                    f"flags=0b{flags:04b}"
                )
                seq_id = (seq_id + 1) & 0xFFFF
                save_seq_id(seq_id)
                data_changed = False

            await asyncio.sleep(SEND_PERIOD_S)

    except Exception as exc:
        print(f"Błąd: {exc}")
    finally:
        print("Zatrzymywanie...")
        # Wyślij pakiet stopu przed wyjściem
        send_to_car(seq_id, 0, 0, False, False, False, False)
        save_seq_id(seq_id)
        controller.deactivate()


if __name__ == "__main__":
    asyncio.run(main())
