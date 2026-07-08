import serial
import serial.tools.list_ports
import time
import numpy as np
import marge.configs.hw_config as hw


class Arduino:
    def __init__(self, baudrate=115200, timeout=1.0):
        self.device = None
        self.port = None
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial_number = None

    def findPort(self):
        for port in serial.tools.list_ports.comports():
            if self.serial_number and port.serial_number == self.serial_number:
                return port.device
        return None

    def connect(self, serial_number=None):
        self.serial_number = serial_number
        self.port = self.findPort()
        if not self.port:
            raise RuntimeError("Arduino not found")

        self.device = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout
        )

        time.sleep(2.5)
        self.device.reset_input_buffer()
        self.device.reset_output_buffer()
        print("Connected to Arduino:", self.port)

    def send(self, data: str):
        if not self.device:
            raise RuntimeError("Arduino not connected")
        self.device.write((data + "\n").encode())
        return self.receive()

    def receive(self):
        if not self.device:
            return None
        time.sleep(0.3)
        responses = []
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if self.device.in_waiting > 0:
                line = self.device.readline().decode(errors='replace').strip()
                if line:
                    responses.append(line)
                if "Ready" in responses[-1] if responses else False:
                    break
            else:
                time.sleep(0.05)
        return responses


class SDRO:
    """
    SDRO controller for RF gain/attenuation hardware.

    Hardware chain: Attenuator (0-31 dB) -> 26 dB Amplifier
    Total gain = 26 - attenuation
    Valid gain range: -5 dB (max attenuation) to 26 dB (no attenuation)

    Three modes of operation:

    - auto:       Deterministic one-shot gain correction from Larmor echo amplitude.
                  G_new = G_current + 20 * log10(targetAmplitude / echoAmplitude)
                  Target = 125 mV = 50% of Red Pitaya ADC max (250 mV).
                  Use when signal is not saturated (echo < 200 mV).

    - manual:     User provides gain directly, no calculation performed.
                  Use when signal is saturated and ADC is clipping,
                  or when a specific gain value is required.

    - iterative:  Steps gain down by 1 dB at a time until echo drops below
                  200 mV, then applies deterministic correction to reach target.
                  Use when ADC is clipping and real signal amplitude is unknown
                  (e.g. real signal is 400 mV but ADC reports 250 mV).
                  Requires Larmor to run between each step to update hw.echoAmplitude.

    Safety limit: 200 mV soft threshold (80% of Red Pitaya ADC max 250 mV).
    Serial number stored in hw_config.py as hw.ard_sn_sdro.
    """

    def __init__(self, serial_number):
        self.arduino = Arduino(baudrate=115200, timeout=1.0)
        self.serial_number = serial_number
        self.current_gain = None

    def connect(self):
        """Connect to the SDRO Arduino."""
        self.arduino.connect(serial_number=self.serial_number)

    def disconnect(self):
        """Disconnect from the SDRO Arduino."""
        if self.arduino.device:
            self.arduino.device.close()
            print("Disconnected from Arduino")

    def set_gain(self, gain: int):
        """
        Set total gain of the SDRO system.

        :param gain: Desired gain in dB. Valid range: -5 to 26 dB.
                     total_gain = 26dB (amp) - attenuation
                     e.g. gain=10 -> attenuation=16, gain=26 -> attenuation=0, gain=-5 -> attenuation=31
        :return: Arduino response.
        """
        attenuation = 26 - gain

        if not (0 <= attenuation <= 31):
            raise ValueError(f"Gain {gain} dB out of range. Valid range is -5 to 26 dB.")

        response = self.arduino.send(str(attenuation))
        self.current_gain = gain
        print(f"Gain={gain} dB -> Attenuation={attenuation} dB | Arduino: {response}")
        return response

    def get_gain(self):
        """
        Read back current gain from Arduino and compute total gain.

        :return: Current total gain in dB.
        """
        response = self.arduino.send("GET")

        attenuation = None
        for line in response:
            if "Received=" in line:
                attenuation = int(line.split("=")[1])
                break

        if attenuation is not None:
            total_gain = 26 - attenuation
            print(f"Attenuation={attenuation} dB -> Gain={total_gain} dB | Arduino: {response}")
            return total_gain
        else:
            print("WARNING: Could not parse gain from Arduino response:", response)
            return None

    def suggest_gain(self):
        """
        Suggest the correct gain based on current hw.echoAmplitude without setting it.
        Use this to know what manual gain value to input when signal is saturated.

        :return: Suggested gain in dB, or None if echo amplitude is unavailable.
        """
        echo = hw.echoAmplitude
        target = hw.targetAmplitude

        if echo is None or echo == 0.0:
            print("WARNING: No echo amplitude available.")
            return None

        if self.current_gain is None:
            print("WARNING: No current gain set. Set an initial gain first.")
            return None

        correction = 20 * np.log10(target / echo)
        suggested_gain = int(np.floor(self.current_gain + correction))
        suggested_gain = max(-5, min(26, suggested_gain))

        print(f"Current echo    : {echo:.3f} mV")
        print(f"Current gain    : {self.current_gain} dB")
        print(f"Suggested gain  : {suggested_gain} dB")
        print(f"Input this value in manual mode to bring signal to ~{target:.0f} mV")

        return suggested_gain

    def adjust_gain(self, manual_gain=None, mode='auto'):
        """
        Set SDRO gain in one of three modes.

        :param manual_gain: int, required when mode='manual'. Gain in dB (-5 to 26).
        :param mode: str, one of 'auto', 'manual', 'iterative'.

        auto mode:
            Deterministic one-shot correction:
            G_new = G_current + 20 * log10(targetAmplitude / echoAmplitude)
            Requires hw.echoAmplitude to be set by Larmor sequence first.

        manual mode:
            Sets gain directly to manual_gain value with no calculation.
            Use when signal is saturated or Larmor has not been run.
            Call suggest_gain() first to know what value to input.

        iterative mode:
            Steps gain down by 1 dB per iteration until echo < 200 mV,
            then applies deterministic correction to reach target.
            Use when ADC is clipping and real signal amplitude is unknown.
            NOTE: hw.echoAmplitude must be updated by Larmor between each
            iteration step for this mode to work correctly in MaRGE.
        """

        # MANUAL MODE
        if mode == 'manual':
            if manual_gain is None:
                print("ERROR: manual_gain must be provided in manual mode.")
                return
            if not (-5 <= manual_gain <= 26):
                raise ValueError(f"Manual gain {manual_gain} dB out of range. Valid range is -5 to 26 dB.")
            print(f"Manual mode: setting gain to {manual_gain} dB")
            self.set_gain(manual_gain)
            return

        # AUTO MODE
        if mode == 'auto':
            echo = hw.echoAmplitude
            target = hw.targetAmplitude

            if echo is None or echo == 0.0:
                print("WARNING: No echo amplitude available. Run Larmor first or use manual mode.")
                return

            print(f"Echo amplitude  : {echo:.3f} mV")
            print(f"Target amplitude: {target:.3f} mV")
            print(f"Current gain    : {self.current_gain} dB")

            if echo > 200.0:
                print("WARNING: Signal near ADC limit. Consider switching to iterative mode.")

            correction = 20 * np.log10(target / echo)
            new_gain = int(np.round(self.current_gain + correction))
            new_gain = max(-5, min(26, new_gain))

            print(f"Gain correction : {correction:+.2f} dB")
            print(f"New gain        : {new_gain} dB")
            self.set_gain(new_gain)
            return

        # ITERATIVE MODE
        if mode == 'iterative':
            echo = hw.echoAmplitude
            target = hw.targetAmplitude
            max_steps = 31
            step = 0

            if echo is None or echo == 0.0:
                print("WARNING: No echo amplitude available. Run Larmor first.")
                return

            print(f"Iterative mode started.")
            print(f"Current gain    : {self.current_gain} dB")
            print(f"Echo amplitude  : {echo:.3f} mV")

            # Step 1: step down gain by 1 dB until echo is below 200 mV
            while echo > 200.0 and step < max_steps:
                new_gain = self.current_gain - 1
                new_gain = max(-5, min(26, new_gain))
                print(f"Step {step + 1}: echo={echo:.1f} mV > 200 mV -> decreasing gain to {new_gain} dB")
                self.set_gain(new_gain)

                # Wait for hardware to settle
                # In MaRGE, Larmor must run again after this to update hw.echoAmplitude
                time.sleep(0.5)
                echo = hw.echoAmplitude
                step += 1

                if new_gain == -5:
                    print("WARNING: Reached minimum gain (-5 dB). Stopping iteration.")
                    break

            if echo > 200.0:
                print("WARNING: Signal still above 200 mV after maximum steps. Check hardware.")
                return

            # Step 2: signal in safe range, apply deterministic correction
            print(f"Signal in safe range: echo={echo:.3f} mV -> applying deterministic correction.")
            correction = 20 * np.log10(target / echo)
            new_gain = int(np.round(self.current_gain + correction))
            new_gain = max(-5, min(26, new_gain))

            print(f"Gain correction : {correction:+.2f} dB")
            print(f"Final gain      : {new_gain} dB")
            self.set_gain(new_gain)
            return

        print(f"ERROR: Unknown mode '{mode}'. Use auto, manual, or iterative.")


if __name__ == '__main__':
    sdro = SDRO(serial_number=hw.ard_sn_sdro)
    sdro.connect()

    # Set initial gain to 0 dB
    sdro.set_gain(0)
    sdro.get_gain()

    # AUTO MODE tests
    print("\n--- Auto Test 1: weak signal ---")
    hw.echoAmplitude = 20.0
    sdro.adjust_gain(mode='auto')           # +15.9 dB -> gain=16

    print("\n--- Auto Test 2: baseline ---")
    hw.echoAmplitude = 48.0
    sdro.adjust_gain(mode='auto')           # +8.3 dB -> gain=8

    print("\n--- Auto Test 3: on target ---")
    hw.echoAmplitude = 125.0
    sdro.adjust_gain(mode='auto')           # 0 dB -> no change

    print("\n--- Auto Test 4: near saturation ---")
    hw.echoAmplitude = 220.0
    sdro.adjust_gain(mode='auto')           # warning + correction

    # MANUAL MODE test
    print("\n--- Manual Test: user sets gain directly ---")
    sdro.adjust_gain(manual_gain=20, mode='manual')

    # SUGGEST GAIN test
    print("\n--- Suggest gain when saturated ---")
    hw.echoAmplitude = 50.0
    sdro.suggest_gain()

    # ITERATIVE MODE test
    print("\n--- Iterative Test: simulated saturation ---")
    hw.echoAmplitude = 100.0
    sdro.adjust_gain(mode='iterative')

    sdro.disconnect()



















#
#
# import serial
# import serial.tools.list_ports
# import time
# import numpy as np
# import marge.configs.hw_config as hw
#
#
# class Arduino:
#     def __init__(self, baudrate=115200, timeout=1.0):
#         self.device = None
#         self.port = None
#         self.baudrate = baudrate
#         self.timeout = timeout
#         self.serial_number = None
#
#     def findPort(self):
#         for port in serial.tools.list_ports.comports():
#             if self.serial_number and port.serial_number == self.serial_number:
#                 return port.device
#         return None
#
#     def connect(self, serial_number=None):
#         self.serial_number = serial_number
#         self.port = self.findPort()
#         if not self.port:
#             raise RuntimeError("Arduino not found")
#
#         self.device = serial.Serial(
#             port=self.port,
#             baudrate=self.baudrate,
#             timeout=self.timeout
#         )
#
#         time.sleep(2.5)
#         self.device.reset_input_buffer()
#         self.device.reset_output_buffer()
#         print("Connected to Arduino:", self.port)
#
#     def send(self, data: str):
#         if not self.device:
#             raise RuntimeError("Arduino not connected")
#         self.device.write((data + "\n").encode())
#         return self.receive()
#
#     def receive(self):
#         if not self.device:
#             return None
#         time.sleep(0.3)
#         responses = []
#         deadline = time.time() + 3.0
#         while time.time() < deadline:
#             if self.device.in_waiting > 0:
#                 line = self.device.readline().decode(errors='replace').strip()
#                 if line:
#                     responses.append(line)
#                 if "Ready" in responses[-1] if responses else False:
#                     break
#             else:
#                 time.sleep(0.05)
#         return responses
#
#
# class SDRO:
#     """
#     SDRO controller for RF gain/attenuation hardware.
#
#     Hardware chain: Attenuator (0-31 dB) -> 26 dB Amplifier
#     Total gain = 26 - attenuation
#     Valid gain range: -5 dB (max attenuation) to 26 dB (no attenuation)
#
#     Two modes of operation:
#         - Auto:   gain computed deterministically from Larmor echo amplitude.
#                   G_new = G_current + 20 * log10(targetAmplitude / echoAmplitude)
#                   Target amplitude = 125 mV = 50% of Red Pitaya ADC max (250 mV).
#         - Manual: user provides gain directly, bypassing automatic calculation.
#                   Used when signal saturates or auto mode cannot run.
#
#     Safety limit: 200 mV (80% of Red Pitaya ADC max 250 mV).
#     Serial number stored in hw_config.py as hw.ard_sn_sdro.
#     """
#
#     def __init__(self, serial_number):
#         self.arduino = Arduino(baudrate=115200, timeout=1.0)
#         self.serial_number = serial_number
#         self.current_gain = None
#
#     def connect(self):
#         """Connect to the SDRO Arduino."""
#         self.arduino.connect(serial_number=self.serial_number)
#
#     def disconnect(self):
#         """Disconnect from the SDRO Arduino."""
#         if self.arduino.device:
#             self.arduino.device.close()
#             print("Disconnected from Arduino")
#
#     def set_gain(self, gain: int):
#         """
#         Set total gain of the SDRO system.
#
#         :param gain: Desired gain in dB. Valid range: -5 to 26 dB.
#                      total_gain = 26dB (amp) - attenuation
#                      e.g. gain=10 -> attenuation=16, gain=26 -> attenuation=0, gain=-5 -> attenuation=31
#         :return: Arduino response.
#         """
#         attenuation = 26 - gain
#
#         if not (0 <= attenuation <= 31):
#             raise ValueError(f"Gain {gain} dB out of range. Valid range is -5 to 26 dB.")
#
#         response = self.arduino.send(str(attenuation))
#         self.current_gain = gain
#         print(f"Gain={gain} dB -> Attenuation={attenuation} dB | Arduino: {response}")
#         return response
#
#     def get_gain(self):
#         """
#         Read back current gain from Arduino and compute total gain.
#
#         :return: Current total gain in dB.
#         """
#         response = self.arduino.send("GET")
#
#         attenuation = None
#         for line in response:
#             if "Received=" in line:
#                 attenuation = int(line.split("=")[1])
#                 break
#
#         if attenuation is not None:
#             total_gain = 26 - attenuation
#             print(f"Attenuation={attenuation} dB -> Gain={total_gain} dB | Arduino: {response}")
#             return total_gain
#         else:
#             print("WARNING: Could not parse gain from Arduino response:", response)
#             return None
#
#
#
#     def adjust_gain(self, manual_gain=None):
#         """
#         Set SDRO gain in auto or manual mode.
#
#         Manual mode: if manual_gain is provided, set it directly without any calculation.
#                      Use this when signal saturates or Larmor has not been run.
#
#         Auto mode:   if manual_gain is None, compute the exact gain correction in one
#                      deterministic step from hw.echoAmplitude:
#                          G_new = G_current + 20 * log10(targetAmplitude / echoAmplitude)
#                      Target = 125 mV = 50% of Red Pitaya ADC max (250 mV).
#                      Safety: if echo > 200 mV, use floor rounding to guarantee signal
#                      drops below 125 mV immediately.
#
#         :param manual_gain: Optional int. If provided, sets gain directly (manual mode).
#                             If None, auto mode is used.
#         """
#
#         # MANUAL MODE
#         if manual_gain is not None:
#             if not (-5 <= manual_gain <= 26):
#                 raise ValueError(f"Manual gain {manual_gain} dB out of range. Valid range is -5 to 26 dB.")
#             print(f"Manual mode: setting gain to {manual_gain} dB")
#             self.set_gain(manual_gain)
#             return
#
#         # AUTO MODE
#         echo = hw.echoAmplitude
#         target = hw.targetAmplitude  # 100 mV
#
#         if echo is None or echo == 0.0:
#             print("WARNING: No echo amplitude available. Run Larmor sequence first or use manual mode.")
#             return
#
#         print(f"Echo amplitude  : {echo:.3f} mV")
#         print(f"Target amplitude: {target:.3f} mV")
#         print(f"Current gain    : {self.current_gain} dB")
#
#         # Safety check: protect Red Pitaya ADC from saturation
#         if echo > 200.0:
#             correction = 20 * np.log10(target / echo)
#             new_gain = int(np.floor(self.current_gain + correction))
#             new_gain = max(-5, min(26, new_gain))
#             print(f"WARNING: Signal {echo:.1f} mV near ADC limit -> correcting gain to {new_gain} dB")
#             self.set_gain(new_gain)
#             return
#
#         # Deterministic one-shot gain correction
#         correction = 20 * np.log10(target / echo)
#         new_gain = int(np.round(self.current_gain + correction))
#         new_gain = max(-5, min(26, new_gain))
#
#         print(f"Gain correction : {correction:+.2f} dB")
#         print(f"New gain        : {new_gain} dB")
#
#         self.set_gain(new_gain)
#
#
# if __name__ == '__main__':
#     sdro = SDRO(serial_number=hw.ard_sn_sdro)
#     sdro.connect()
#
#     # Set initial gain to 0 dB
#     sdro.set_gain(0)
#     sdro.get_gain()
#
#     # AUTO MODE tests
#     print("\n--- Auto Test 1: weak signal ---")
#     hw.echoAmplitude = 20.0
#     sdro.adjust_gain()              # +15.9 dB -> gain=16
#
#     print("\n--- Auto Test 2: baseline signal ---")
#     hw.echoAmplitude = 48.0
#     sdro.adjust_gain()              # +8.3 dB -> gain=8
#
#     print("\n--- Auto Test 3: on target ---")
#     hw.echoAmplitude = 125.0
#     sdro.adjust_gain()              # 0 dB -> no change
#
#     print("\n--- Auto Test 4: near saturation ---")
#     hw.echoAmplitude = 220.0
#     sdro.adjust_gain()              # floor -> gain=-5
#
#     # MANUAL MODE test
#     print("\n--- Manual Test: user sets gain directly ---")
#     sdro.adjust_gain(manual_gain=10)   # force safe gain when saturated
#
#     sdro.disconnect()
#
#
#









# import serial
# import serial.tools.list_ports
# import time
# import marge.configs.hw_config as hw
#
#
# class Arduino:
#     def __init__(self, baudrate=115200, timeout=1.0):
#         self.device = None
#         self.port = None
#         self.baudrate = baudrate
#         self.timeout = timeout
#         self.serial_number = None
#
#     def findPort(self):
#         for port in serial.tools.list_ports.comports():
#             if self.serial_number and port.serial_number == self.serial_number:
#                 return port.device
#         return None
#
#     def connect(self, serial_number=None):
#         self.serial_number = serial_number
#         self.port = self.findPort()
#         if not self.port:
#             raise RuntimeError("Arduino not found")
#
#         self.device = serial.Serial(
#             port=self.port,
#             baudrate=self.baudrate,
#             timeout=self.timeout
#         )
#
#         time.sleep(2.5)
#         self.device.reset_input_buffer()
#         self.device.reset_output_buffer()
#         print("Connected to Arduino:", self.port)
#
#     def send(self, data: str):
#         if not self.device:
#             raise RuntimeError("Arduino not connected")
#         self.device.write((data + "\n").encode())
#         return self.receive()
#
#     def receive(self):
#         if not self.device:
#             return None
#         time.sleep(0.3)
#         responses = []
#         deadline = time.time() + 3.0
#         while time.time() < deadline:
#             if self.device.in_waiting > 0:
#                 line = self.device.readline().decode(errors='replace').strip()
#                 if line:
#                     responses.append(line)
#                 if "Ready" in responses[-1] if responses else False:
#                     break
#             else:
#                 time.sleep(0.05)
#         return responses
#
#
# class SDRO:
#     """
#     SDRO controller for RF gain/attenuation hardware.
#
#     Hardware chain: Attenuator (0-31 dB) -> 26 dB Amplifier
#     Total gain = 26 - attenuation
#     Valid gain range: -5 dB (max attenuation) to 26 dB (no attenuation)
#
#     Workflow:
#         1. RARE sequence runs and acquires echo
#         2. RarePyPulseq() updates hw.echoAmplitude from k=0 central line
#         3. sdro.adjust_gain() reads hw.echoAmplitude and adjusts gain automatically
#         4. Target amplitude: hw.targetAmplitude = 48.0 mV (best SNR with gradients ON)
#         5. Safety limit: 200 mV (80% of Red Pitaya ADC max 250 mV)
#
#     Serial number is stored in hw_config.py as hw.ard_sn_sdro
#     """
#
#     def __init__(self, serial_number):
#         self.arduino = Arduino(baudrate=115200, timeout=1.0)
#         self.serial_number = serial_number
#         self.current_gain = None
#
#     def connect(self):
#         """Connect to the SDRO Arduino."""
#         self.arduino.connect(serial_number=self.serial_number)
#
#     def disconnect(self):
#         """Disconnect from the SDRO Arduino."""
#         if self.arduino.device:
#             self.arduino.device.close()
#             print("Disconnected from Arduino")
#
#     def set_gain(self, gain: int):
#         """
#         Set total gain of the SDRO system.
#
#         :param gain: Desired gain in dB. Valid range: -5 to 26 dB.
#                      total_gain = 26dB (amp) - attenuation
#                      e.g. gain=10 -> attenuation=16, gain=26 -> attenuation=0, gain=-5 -> attenuation=31
#         :return: Arduino response.
#         """
#         attenuation = 26 - gain
#
#         if not (0 <= attenuation <= 31):
#             raise ValueError(f"Gain {gain} dB out of range. Valid range is -5 to 26 dB.")
#
#         response = self.arduino.send(str(attenuation))
#         self.current_gain = gain
#         print(f"Gain={gain} dB -> Attenuation={attenuation} dB | Arduino: {response}")
#         return response
#
#     def get_gain(self):
#         """
#         Read back current gain from Arduino and compute total gain.
#
#         :return: Current total gain in dB.
#         """
#         response = self.arduino.send("GET")
#
#         attenuation = None
#         for line in response:
#             if "Received=" in line:
#                 attenuation = int(line.split("=")[1])
#                 break
#
#         if attenuation is not None:
#             total_gain = 26 - attenuation
#             print(f"Attenuation={attenuation} dB -> Gain={total_gain} dB | Arduino: {response}")
#             return total_gain
#         else:
#             print("WARNING: Could not parse gain from Arduino response:", response)
#             return None
#
#     def adjust_gain(self):
#         """
#         Read hw.echoAmplitude updated by RarePyPulseq() and adjust gain automatically.
#         Call this after every RARE acquisition.
#
#         Target amplitude is read from hw.targetAmplitude (48 mV, best SNR with gradients ON).
#         Safety limit: 200 mV to protect Red Pitaya ADC (max 250 mV).
#
#         Logic:
#             - If echo > 200 mV                  -> decrease gain immediately (ADC saturation risk)
#             - If echo < target * 0.9            -> increase gain by 1 dB (signal too low)
#             - If echo > target * 1.1            -> decrease gain by 1 dB (signal too high)
#             - If echo within 10% of target      -> no adjustment needed
#         """
#         echo = hw.echoAmplitude
#         target = hw.targetAmplitude
#
#         if echo is None or echo == 0.0:
#             print("WARNING: No echo amplitude available. Run RARE sequence first.")
#             return
#
#         print(f"Echo amplitude  : {echo:.3f} mV")
#         print(f"Target amplitude: {target:.3f} mV")
#         print(f"Current gain    : {self.current_gain} dB")
#
#         # Safety check: protect Red Pitaya ADC from saturation
#         if echo > 200.0:
#             new_gain = self.current_gain - 1
#             print(f"WARNING: Signal {echo:.1f} mV approaching ADC limit (250 mV) -> decreasing gain to {new_gain} dB")
#             self.set_gain(new_gain)
#             return
#
#         if echo < target * 0.9:
#             new_gain = self.current_gain + 1
#             print(f"Signal too low -> increasing gain to {new_gain} dB")
#             self.set_gain(new_gain)
#
#         elif echo > target * 1.1:
#             new_gain = self.current_gain - 1
#             print(f"Signal too high -> decreasing gain to {new_gain} dB")
#             self.set_gain(new_gain)
#
#         else:
#             print("Signal within target range -> no gain adjustment needed")
#
#
# if __name__ == '__main__':
#     sdro = SDRO(serial_number=hw.ard_sn_sdro)
#     sdro.connect()
#
#     # Set initial gain to +10 dB (known best from measurements)
#     sdro.set_gain(10)
#     sdro.get_gain()
#
#     # Simulate what happens after RarePyPulseq() updates hw.echoAmplitude
#     print("\n--- Test 1: signal too low ---")
#     hw.echoAmplitude = 20.0
#     sdro.adjust_gain()          # should increase gain to 11 dB
#
#     print("\n--- Test 2: signal too high ---")
#     hw.echoAmplitude = 80.0
#     sdro.adjust_gain()          # should decrease gain to 10 dB
#
#     print("\n--- Test 3: signal on target ---")
#     hw.echoAmplitude = 48.0
#     sdro.adjust_gain()          # should do nothing
#
#     print("\n--- Test 4: ADC saturation risk ---")
#     hw.echoAmplitude = 220.0
#     sdro.adjust_gain()          # should decrease gain immediately
#
#     sdro.disconnect()
#
#







#
# import serial
# import serial.tools.list_ports
# import time
#
#
# class Arduino:
#     def __init__(self, baudrate=115200, timeout=1.0):
#         self.device = None
#         self.port = None
#         self.baudrate = baudrate
#         self.timeout = timeout
#         self.serial_number = None
#
#     def findPort(self):
#         for port in serial.tools.list_ports.comports():
#             if self.serial_number and port.serial_number == self.serial_number:
#                 return port.device
#         return None
#
#     def connect(self, serial_number=None):
#         self.serial_number = serial_number
#         self.port = self.findPort()
#         if not self.port:
#             raise RuntimeError("Arduino not found")
#
#         self.device = serial.Serial(
#             port=self.port,
#             baudrate=self.baudrate,
#             timeout=self.timeout
#         )
#
#         time.sleep(2.5)  # wait for Arduino to boot after reset
#         self.device.reset_input_buffer()
#         self.device.reset_output_buffer()
#         print("Connected to Arduino:", self.port)
#
#     def send(self, data: str):
#         if not self.device:
#             raise RuntimeError("Arduino not connected")
#         self.device.write((data + "\n").encode())
#         return self.receive()
#
#     def receive(self):
#         if not self.device:
#             return None
#         time.sleep(0.3)
#         responses = []
#         deadline = time.time() + 3.0
#         while time.time() < deadline:
#             if self.device.in_waiting > 0:
#                 line = self.device.readline().decode(errors='replace').strip()
#                 if line:
#                     responses.append(line)
#                 if "Ready" in responses[-1] if responses else False:
#                     break
#             else:
#                 time.sleep(0.05)
#         return responses
#
#
# class SDRO:
#     """
#     SDRO controller for RF gain/attenuation hardware.
#
#     Hardware chain: Attenuator (0-31 dB) -> 26 dB Amplifier
#     Total gain = 26 - attenuation
#     Valid gain range: -5 dB (max attenuation) to 26 dB (no attenuation)
#     """
#
#     def __init__(self, serial_number):
#         self.arduino = Arduino(baudrate=115200, timeout=1.0)
#         self.serial_number = serial_number
#         self.current_gain = None
#
#     def connect(self):
#         """Connect to the SDRO Arduino."""
#         self.arduino.connect(serial_number=self.serial_number)
#
#     def disconnect(self):
#         """Disconnect from the SDRO Arduino."""
#         if self.arduino.device:
#             self.arduino.device.close()
#             print("Disconnected from Arduino")
#
#     def set_gain(self, gain: int):
#         """
#         Set total gain of the SDRO system.
#
#         :param gain: Desired gain in dB. Valid range: -5 to 26 dB.
#                      total_gain = 26dB (amp) - attenuation
#                      e.g. gain=20 -> attenuation=6, gain=26 -> attenuation=0, gain=-5 -> attenuation=31
#         :return: Arduino response.
#         """
#         attenuation = 26 - gain
#
#         if not (0 <= attenuation <= 31):
#             raise ValueError(f"Gain {gain} dB out of range. Valid range is -5 to 26 dB.")
#
#         response = self.arduino.send(str(attenuation))
#         self.current_gain = gain
#         print(f"Gain={gain} dB -> Attenuation={attenuation} dB | Arduino: {response}")
#         return response
#
#     def get_gain(self):
#         """
#         Read back current gain from Arduino and compute total gain.
#
#         :return: Current total gain in dB.
#         """
#         response = self.arduino.send("GET")
#
#         # Parse attenuation from response e.g. ['Received=6', 'Ready']
#         attenuation = None
#         for line in response:
#             if "Received=" in line:
#                 attenuation = int(line.split("=")[1])
#                 break
#
#         if attenuation is not None:
#             total_gain = 26 - attenuation
#             print(f"Attenuation={attenuation} dB -> Gain={total_gain} dB | Arduino: {response}")
#             return total_gain
#         else:
#             print("WARNING: Could not parse gain from Arduino response:", response)
#             return None
#
#
# if __name__ == '__main__':
#     sdro = SDRO(serial_number='44238313139351507280')
#     sdro.connect()
#
#     # Set gain examples
#     sdro.set_gain(6)   # attenuation=6
#     #sdro.set_gain(26)   # attenuation=0  (max gain, amp only)
#     #sdro.set_gain(0)    # attenuation=26
#     #sdro.set_gain(-5)   # attenuation=31 (max attenuation)
#
#     # Read back current gain
#     sdro.get_gain()
#
#     sdro.disconnect()