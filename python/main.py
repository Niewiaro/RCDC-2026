import asyncio
from time import sleep
from dualsense_controller import DualSenseController
import websockets

# config
ESP_WS_URL = "ws://192.168.4.1/ws"
RUMBLE_STOP = 0
RUMBLE_DEFAULT = 255
SENSITIVITY = 0.65

# global
data_changed = False
left_trigger = 0.0
right_trigger = 0.0
left_stick_x = 0.0
button_cross = False

# list availabe devices and throw exception when tzhere is no device detected
device_infos = DualSenseController.enumerate_devices()
if len(device_infos) < 1:
    raise Exception("No DualSense Controller available.")

# flag, which keeps program alive
is_running = True

# create an instance, use first available device
controller = DualSenseController()


# switches the keep alive flag, which stops the below loop
def stop():
    global is_running
    is_running = False


def rumble_start(value: int = RUMBLE_DEFAULT):
    controller.left_rumble.set(value)
    controller.right_rumble.set(value)


def rumble_stop(value: int = RUMBLE_STOP):
    controller.left_rumble.set(value)
    controller.right_rumble.set(value)


def lightbar():
    global left_trigger, right_trigger, left_stick_x, button_cross

    if left_trigger != 0.0 or button_cross:
        controller.lightbar.set_color_red()
    elif right_trigger != 0.0:
        controller.lightbar.set_color_green()
    elif left_stick_x != 0.0:
        controller.lightbar.set_color_white()
    else:
        controller.lightbar.set_color_blue()


def rumble():
    global left_trigger, left_stick_x, button_cross

    if left_trigger != 0.0 or abs(left_stick_x) >= 0.9 or button_cross:
        rumble_start()
    else:
        rumble_stop()


def on_left_trigger(value):
    # print(f"left trigger changed: {value}")
    global data_changed, left_trigger
    left_trigger = value
    data_changed = True

    rumble()
    lightbar()


def on_right_trigger(value):
    # print(f"right trigger changed: {value}")
    global data_changed, right_trigger
    right_trigger = value
    data_changed = True

    lightbar()


def on_left_stick_x_changed(value):
    # print(f"on_left_stick_x_changed: {value}")
    global data_changed, left_stick_x
    left_stick_x = value
    data_changed = True

    rumble()
    lightbar()


def on_cross_btn_pressed():
    # print('cross button pressed')
    global data_changed, button_cross
    button_cross = True
    data_changed = True

    rumble()
    lightbar()


def on_cross_btn_released():
    # print('cross button released')
    global data_changed, button_cross
    button_cross = False
    data_changed = True

    rumble()
    lightbar()


# callback, when PlayStation button is pressed
# stop program
def on_ps_btn_pressed():
    print("PS button released -> stop")
    stop()


# callback, when unintended error occurs,
# i.e. physically disconnecting the controller during operation
# stop program
def on_error(error):
    print(f"Opps! an error occured: {error}")
    stop()


controller.left_trigger.on_change(on_left_trigger)
controller.right_trigger.on_change(on_right_trigger)
controller.left_stick_x.on_change(on_left_stick_x_changed)
controller.btn_cross.on_down(on_cross_btn_pressed)
controller.btn_cross.on_up(on_cross_btn_released)

# register the button callbacks
controller.btn_ps.on_down(on_ps_btn_pressed)

# register the error callback
controller.on_error(on_error)


def serialize_controller_input(value):
    return int(abs(value) * 255)


def serialize_esp_input(value):
    return min(int(abs(value)), 255)


def serialize_data():
    global left_trigger, right_trigger, left_stick_x, button_cross

    left_trigger_serialized = serialize_controller_input(left_trigger)
    right_trigger_serialized = serialize_controller_input(right_trigger)
    left_stick_x_serialized = serialize_controller_input(left_stick_x)

    l_pwm = r_pwm = 0
    in1 = in2 = in3 = in4 = 0
    sensitivity = SENSITIVITY

    if left_trigger > 0:
        r_pwm += left_trigger_serialized
        l_pwm += left_trigger_serialized
        sensitivity = SENSITIVITY * left_trigger

    elif right_trigger > 0:
        r_pwm -= right_trigger_serialized
        l_pwm -= right_trigger_serialized
        sensitivity = SENSITIVITY * right_trigger

    if left_stick_x > 0:
        r_pwm -= left_stick_x_serialized * (sensitivity + 0.2)
        l_pwm += left_stick_x_serialized * (sensitivity + 0.2)

    elif left_stick_x < 0:
        r_pwm += left_stick_x_serialized * (sensitivity + 0.2)
        l_pwm -= left_stick_x_serialized * (sensitivity + 0.2)

    if l_pwm > 0:
        in1 = 1
        in2 = 0
    elif l_pwm < 0:
        in1 = 0
        in2 = 1

    if r_pwm > 0:
        in3 = 1
        in4 = 0
    elif r_pwm < 0:
        in3 = 0
        in4 = 1

    if button_cross:
        in1 = in3 = 1
        in2 = in4 = 1

    l_pwm = serialize_esp_input(l_pwm)
    r_pwm = serialize_esp_input(r_pwm)

    return [l_pwm, in1, in2, r_pwm, in3, in4]


async def main():
    global is_running, data_changed

    print("Connecting to ESP...")
    ws = await websockets.connect(ESP_WS_URL)
    print("ESP connected")

    print("Connecting to DualSense...")
    controller.activate()
    controller.lightbar.set_color_blue()
    print("DualSense activated!")

    try:
        while is_running:
            if data_changed:
                controller_data = serialize_data()
                message = ",".join(map(str, controller_data))
                await ws.send(message)
                data_changed = False
                print(message)
            await asyncio.sleep(0.01)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        print("Disconnecting...")
        await ws.close()
        controller.deactivate()


asyncio.run(main())
