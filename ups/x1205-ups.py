#!/usr/bin/python3

"""X1205 / X120x UPS monitor for the Raspberry Pi 5.

Reads battery voltage and capacity over I2C, controls battery charging
through a GPIO line, and shuts the Pi down cleanly when the battery is
critical or AC power stays lost for several consecutive checks.

How it is run
-------------
This script is NOT started by cron or by a user service. It runs as a
SYSTEM-level systemd service:

    Unit file : /etc/systemd/system/x1205-ups.service
    Interpreter: /home/tommaso/.Python-venvs/jupy/bin/python3
    Runs as   : user tommaso (User=/Group= in the unit)
    Restart   : on-failure, every 10 seconds

Because it is a system unit, it will not appear under ~/.config/systemd/user
and "systemctl --user" will not show it. Use these commands instead:

    systemctl status x1205-ups.service
    sudo systemctl restart x1205-ups.service
    journalctl -u x1205-ups.service -n 50 --no-pager

Notes
-----
- The shebang points to /usr/bin/python3, but the service uses the "jupy"
  virtualenv interpreter; dependencies (gpiod, smbus2) must exist there.
- Do not run it manually while the service is active: two instances would
  compete for the I2C bus and the charge-control GPIO.
- The similar BTCups.py from the vendor repo is redundant and is not used.
"""

import contextlib
import fcntl
import logging
import os
import struct
import sys
import time
from pathlib import Path
from subprocess import CalledProcessError, call, check_output

import gpiod
import smbus2
from gpiod.line import Direction, Value

# ============================================================
# COSTANTI DI CONFIGURAZIONE (tutte qui all'inizio)
# ============================================================

# User-configurable variables
SHUTDOWN_THRESHOLD = 3  # Number of consecutive failures required for shutdown
SLEEP_TIME = 60  # Time in seconds to wait between failure checks
Loop = True

# Critical thresholds for shutdown
CRITICAL_VOLTAGE_THRESHOLD = 3.2  # Critical voltage threshold for shutdown (V)
CRITICAL_CAPACITY_THRESHOLD = 20  # Critical capacity threshold for shutdown (%)

# Charging control variables
MAX_CHARGE_VOLTAGE = 4.10  # Maximum charging voltage (V)
CHARGE_RESUME_VOLTAGE = 3.95  # Resume charging below this voltage (V)
CHARGE_CONTROL_PIN = 16  # GPIO pin to control charging (per Suptronics X120X manual)
CHARGE_ENABLE_STATE = 0  # GPIO state to enable charging 1 = disable (high), 0 = enable (low)

PLD_PIN = 6

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def find_gpiochip():
    """
    Trova il gpiochip corrispondente ai pin a 40 pin.
    Su Raspberry Pi 5 è etichettato 'pinctrl-rp1'.
    Il numero del chip NON è stabile tra i riavvii, quindi cerchiamo per etichetta.
    """
    import glob
    for path in sorted(glob.glob('/dev/gpiochip*')):
        try:
            with gpiod.Chip(path) as chip:
                label = chip.get_info().label
                if 'pinctrl-rp1' in label or 'pinctrl-bcm' in label:
                    logger.info(f"Trovato chip GPIO: {path} (etichetta: {label})")
                    return path
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Impossibile aprire {path}: {e}")
            continue
    raise RuntimeError("Nessun gpiochip compatibile trovato (pinctrl-rp1 / pinctrl-bcm)")

def readVoltage(bus):
    """Read battery voltage from MAX17040/MAX17041"""
    try:
        read = bus.read_word_data(address, 2)  # VCELL register
        swapped = struct.unpack("<H", struct.pack(">H", read))[0]  # big endian to little endian
        voltage = swapped * 1.25 / 1000 / 16  # convert to voltage (MAX17040: 1.25mV resolution)
        return voltage
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error reading voltage: {e}")
        return None

def readCapacity(bus):
    """Read battery capacity from MAX17040/MAX17041"""
    try:
        read = bus.read_word_data(address, 4)  # SOC register
        swapped = struct.unpack("<H", struct.pack(">H", read))[0]  # big endian to little endian
        capacity = swapped / 256  # convert to 1-100% scale
        return capacity
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error reading capacity: {e}")
        return None

def readRawVoltage(bus):
    """Read raw voltage register value for debugging"""
    try:
        read = bus.read_word_data(address, 2)
        return read
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error reading raw voltage: {e}")
        return None

def get_battery_status(voltage):
    """Determine battery status based on voltage"""
    if voltage is None:
        return "Unknown"
    elif 3.87 <= voltage <= 4.2:
        return "Full"
    elif 3.7 <= voltage < 3.87:
        return "High"
    elif 3.55 <= voltage < 3.7:
        return "Medium"
    elif 3.4 <= voltage < 3.55:
        return "Low"
    elif voltage < 3.4:
        return "Critical"
    else:
        return "Unknown"

def read_hardware_metric(command_args, strip_chars):
    """Read hardware metrics using vcgencmd (from qtx120x.py)"""
    try:
        output = check_output(command_args).decode("utf-8")
        metric_str = output.split("=")[1].strip().rstrip(strip_chars)
        return float(metric_str)
    except (CalledProcessError, ValueError) as e:
        logger.error(f"Error reading hardware metric: {e}")
        return None

def read_cpu_volts():
    """Return current CPU voltage"""
    return read_hardware_metric(["vcgencmd", "pmic_read_adc", "VDD_CORE_V"], 'V')

def read_cpu_amps():
    """Return current CPU amperage"""
    return read_hardware_metric(["vcgencmd", "pmic_read_adc", "VDD_CORE_A"], 'A')

def read_cpu_temp():
    """Return current CPU temperature"""
    return read_hardware_metric(["vcgencmd", "measure_temp"], "'C")

def read_input_voltage():
    """Return input voltage"""
    return read_hardware_metric(["vcgencmd", "pmic_read_adc", "EXT5V_V"], 'V')

def get_fan_rpm():
    """Get fan RPM from system files"""
    try:
        sys_devices_path = Path('/sys/devices/platform/cooling_fan') 
        fan_input_files = list(sys_devices_path.rglob('fan1_input'))
        if not fan_input_files:
            return "No fan detected"
        with open(fan_input_files[0], 'r') as file:
            rpm = file.read().strip()
        return f"{rpm} RPM"
    except FileNotFoundError: 
        return "Fan RPM file not found"
    except PermissionError:
        return "Permission denied accessing fan RPM"
    except Exception as e:  # noqa: BLE001
        return f"Fan error: {e}"

def power_consumption_watts():
    """Calculate total system power consumption"""
    try:
        output = check_output(['vcgencmd', 'pmic_read_adc']).decode("utf-8")
        lines = output.split('\n')
        amperages = {}
        voltages = {}
        for line in lines:
            cleaned_line = line.strip()
            if cleaned_line:
                parts = cleaned_line.split(' ')
                label, value = parts[0], parts[-1]
                val = float(value.split('=')[1][:-1])
                short_label = label[:-2]
                if label.endswith('A'):
                    amperages[short_label] = val
                else:
                    voltages[short_label] = val
        wattage = sum(amperages[key] * voltages[key] for key in amperages if key in voltages)
        return wattage
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error calculating power consumption: {e}")
        return None

def _fmt(value, spec):
    """Format a metric with a format spec, or return "n/a" if it is missing.

    Args:
        value: Metric value, or None if it could not be read.
        spec: Format specification, for example ".3f".

    Returns:
        The formatted value, or "n/a".
    """
    return "n/a" if value is None else format(value, spec)


def log_status_summary(
    voltage, capacity, charge_status, ac_power_state, input_voltage, pwr_use, cpu_temp, fan_rpm
):
    """Log a compact one-line UPS status, used when stdout is not a terminal.

    With AC power OK the line is INFO. On power loss it is a WARNING, or an
    ERROR from 24% capacity downwards, with a label matching the battery level.

    Args:
        voltage: UPS battery voltage in volts, or None.
        capacity: Battery capacity in percent, or None.
        charge_status: "enabled" or "disabled".
        ac_power_state: 1 if AC power is present, anything else on power loss.
        input_voltage: Raspberry Pi input voltage in volts, or None.
        pwr_use: System power draw in watts, or None.
        cpu_temp: CPU temperature in Celsius, or None.
        fan_rpm: Fan speed in rpm, or None.
    """
    summary = (
        f"UPS {_fmt(voltage, '.3f')}V {_fmt(capacity, '.1f')}% "
        f"charging={charge_status} ac={'ok' if ac_power_state == 1 else 'LOST'} "
        f"in={_fmt(input_voltage, '.2f')}V {_fmt(pwr_use, '.1f')}W "
        f"{_fmt(cpu_temp, '.1f')}C fan={fan_rpm}"
    )
    if ac_power_state == 1:
        logger.info(summary)
        return
    if capacity is None:
        label = "POWER LOSS"
    elif capacity <= 15:
        label = "UPS POWER FAILURE IMMINENT"
    elif capacity <= 24:
        label = "UPS POWER CRITICAL"
    elif capacity <= 50:
        label = "UPS POWER APPROACHING CRITICAL"
    else:
        label = "RUNNING ON UPS BACKUP POWER"
    log = logger.error if capacity is not None and capacity <= 24 else logger.warning
    log("%s - %s", label, summary)


def display_system_stats(voltage, capacity, charging_enabled, ac_power_state):
    """Display comprehensive system statistics"""
    # Read additional system metrics
    cpu_volts = read_cpu_volts()
    cpu_amps = read_cpu_amps()
    cpu_temp = read_cpu_temp()
    input_voltage = read_input_voltage()
    fan_rpm = get_fan_rpm()
    pwr_use = power_consumption_watts()
    
    # ANSI color codes
    ORANGE = '\033[38;5;208m'  # Orange color
    RED_BOLD = '\033[1;31m'    # Red bold
    GREEN = '\033[32m'         # Green
    YELLOW = '\033[33m'        # Yellow for warnings
    RESET = '\033[0m'          # Reset color
    
    # Determine charging status
    charge_status = "enabled" if charging_enabled else "disabled"
    
    # Determine power status
    if ac_power_state == 1:
        power_status = f"{GREEN}✓ AC Power: OK! ✓\n               ✓ Power Adapter: OK! ✓{RESET}"
    else:
        power_status = f"{RED_BOLD}⚠⚠{RESET} {YELLOW}Power Loss OR Power Adapter Failure{RESET} {RED_BOLD}⚠⚠{RESET}"
    
    # Format display text with centered headers
    # Under systemd there is no terminal: log one line instead of the colored report
    if not sys.stdout.isatty():
        log_status_summary(
            voltage, capacity, charge_status, ac_power_state,
            input_voltage, pwr_use, cpu_temp, fan_rpm,
        )
        return

    print("\n" + "="*50)
    print(f"        {ORANGE}=-=-=-=-=-= X120x Stats =-=-=-=-=-{RESET}")
    print(f"             UPS Voltage: {RED_BOLD}{voltage:.3f}V{RESET}" if voltage else f"                 UPS Voltage: {RED_BOLD}N/A{RESET}")
    print(f"             Battery: {RED_BOLD}{capacity:.3f}%{RESET}" if capacity else f"                   Battery: {RED_BOLD}N/A{RESET}")
    print(f"             Charging: {RED_BOLD}{charge_status}{RESET}")
    print(f"        {ORANGE}=-=-=-=-=-= RPi5 Stats =-=-=-=-=-{RESET}")
    print(f"             Input Voltage: {RED_BOLD}{input_voltage:.3f}V{RESET}" if input_voltage else f"               Input Voltage: {RED_BOLD}N/A{RESET}")
    print(f"             CPU Volts: {RED_BOLD}{cpu_volts:.3f}V{RESET}" if cpu_volts else f"                 CPU Volts: {RED_BOLD}N/A{RESET}")
    print(f"             CPU Amps: {RED_BOLD}{cpu_amps:.3f}A{RESET}" if cpu_amps else f"                  CPU Amps: {RED_BOLD}N/A{RESET}")
    print(f"             System Watts: {RED_BOLD}{pwr_use:.3f}W{RESET}" if pwr_use else f"               System Watts: {RED_BOLD}N/A{RESET}")
    print(f"             CPU Temp: {RED_BOLD}{cpu_temp:.1f}°C{RESET}" if cpu_temp else f"                  CPU Temp: {RED_BOLD}N/A{RESET}")
    print(f"             Fan RPM: {RED_BOLD}{fan_rpm}{RESET}")
    print(f"        {ORANGE}=-=-=-=-=-= Charging Control =-=-=-=-=-{RESET}")
    print(f"             Max Charge Voltage: {RED_BOLD}{MAX_CHARGE_VOLTAGE}V{RESET}")
    print(f"             Resume Charge Voltage: {RED_BOLD}{CHARGE_RESUME_VOLTAGE}V{RESET}")
    print(f"             Current Charging State: {RED_BOLD}{charge_status}{RESET}")
    print(f"        {ORANGE}=-=-=-= ⚡ Power Status ⚡ =-=-=-=-{RESET}")
    print(f"                 {power_status}")
    
    # Add warning status based on power and battery levels
    if ac_power_state != 1 and capacity and capacity >= 51:
        print(f"        {RED_BOLD}⚠⚠{RESET} {YELLOW}Running on UPS Backup Power - Batteries @ {RED_BOLD}{capacity:.2f}%{RESET}")
    elif ac_power_state != 1 and capacity and capacity <= 50 and capacity >= 25:
        print(f"     {RED_BOLD}⚠⚠{RESET} {YELLOW}UPS Power levels approaching critical - Batteries @ {RED_BOLD}{capacity:.2f}%{RESET}")
    elif ac_power_state != 1 and capacity and capacity <= 24 and capacity >= 16:
        print(f"          {RED_BOLD}⚠⚠{RESET} {YELLOW}UPS Power levels critical - Batteries @ {RED_BOLD}{capacity:.2f}%{RESET}")
    elif ac_power_state != 1 and capacity and capacity <= 15:
        print(f"        {RED_BOLD}⚠⚠{RESET} {YELLOW}UPS Power failure imminent - Batteries @ {RED_BOLD}{capacity:.2f}%{RESET}")
    
    print("="*50 + "\n")

def control_charging(request, voltage, current_charge_state):
    """
    Control charging based on voltage thresholds.
    
    Args:
        request: LineRequest object from gpiod v2 (contains both PLD and CHARGE_CTRL lines)
        voltage: current battery voltage (float) or None
        current_charge_state: True if charging currently enabled, False otherwise
    
    Returns:
        Updated charging state (True = enabled, False = disabled)
    """
    if voltage is None:
        logger.warning("Cannot control charging - voltage reading failed")
        return current_charge_state
    
    # Helper per convertire lo stato logico in Value della v2
    def state_to_value(enabled: bool) -> Value:
        # CHARGE_ENABLE_STATE == 0 significa "active low" (0 = enable)
        if CHARGE_ENABLE_STATE == 0:
            return Value.ACTIVE if enabled else Value.INACTIVE
        else:
            return Value.INACTIVE if enabled else Value.ACTIVE
    
    try:
        if voltage >= MAX_CHARGE_VOLTAGE and current_charge_state:
            # Stop charging if voltage too high
            request.set_value(CHARGE_CONTROL_PIN, state_to_value(False))
            logger.info(f"CHARGING STOPPED - Voltage {voltage:.3f}V >= {MAX_CHARGE_VOLTAGE}V")
            return False
        
        elif voltage <= CHARGE_RESUME_VOLTAGE and not current_charge_state:
            # Resume charging if voltage dropped sufficiently
            request.set_value(CHARGE_CONTROL_PIN, state_to_value(True))
            logger.info(f"CHARGING RESUMED - Voltage {voltage:.3f}V <= {CHARGE_RESUME_VOLTAGE}V")
            return True
        
        else:
            # No change needed
            return current_charge_state
    
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error controlling charging: {e}")
        return current_charge_state

def check_critical_conditions(ac_power_state, voltage, capacity):
    """
    Check for critical conditions that warrant shutdown.
    AC power loss alone is not critical - it must be combined with low voltage.
    """
    critical_conditions = []
    
    # Check for critical voltage (primary shutdown trigger)
    # Shutdown only when AC is absent; a dead cell with AC present keeps the Pi running.
    if ac_power_state == 0 and voltage is not None and voltage < CRITICAL_VOLTAGE_THRESHOLD:
        critical_conditions.append(f"critical battery voltage ({voltage:.3f}V < {CRITICAL_VOLTAGE_THRESHOLD}V)")
    
    # AC power loss is only critical when combined with critical voltage
    if ac_power_state == 0 and voltage is not None and voltage < CRITICAL_VOLTAGE_THRESHOLD:
        critical_conditions.append("AC power loss with critical battery voltage")
    
    # Optional: Also check capacity as a secondary condition (uncomment if desired)
    # if capacity is not None and capacity < CRITICAL_CAPACITY_THRESHOLD:
    #     critical_conditions.append(f"critical battery capacity ({capacity:.1f}% < {CRITICAL_CAPACITY_THRESHOLD}%)")
    
    return critical_conditions

def quick_start_fuel_gauge(bus):
    """Perform quick start on fuel gauge for better initial readings"""
    try:
        bus.write_word_data(address, 6, 0x4000)  # MODE register quick-start command
        time.sleep(1)  # Wait for quick-start to complete
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error performing quick-start: {e}")

# Lock a livello di kernel: rilasciato automaticamente alla morte del processo
lockfile = os.path.expanduser("~/.X1200.lock")
lock_fd = open(lockfile, 'w')  # noqa: SIM115
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("Script already running")
    sys.exit(1)

# Initialize variables
charging_enabled = True  # Assume charging starts enabled
bus = None
chip = None
request = None

try:
    # Initialize I2C bus
    bus = smbus2.SMBus(1)
    address = 0x36
    
    # Initialize GPIO (libgpiod v2)
    chip_path = find_gpiochip()
    
    try:
        request = gpiod.request_lines(
            chip_path,
            consumer="x1205-ups",
            config={
                PLD_PIN: gpiod.LineSettings(
                    direction=Direction.INPUT,
                ),
                CHARGE_CONTROL_PIN: gpiod.LineSettings(
                    direction=Direction.OUTPUT,
                    output_value=Value.ACTIVE if CHARGE_ENABLE_STATE == 0 else Value.INACTIVE,
                ),
            },
        )
        logger.info(f"GPIO inizializzati su {chip_path} (PLD={PLD_PIN}, CHARGE={CHARGE_CONTROL_PIN})")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Errore nell'inizializzazione GPIO su {chip_path}: {e}")
        logger.error("Impossibile monitorare UPS senza GPIO. Uscita.")
        sys.exit(1)
    
    # Perform initial fuel gauge quick-start for better accuracy
    quick_start_fuel_gauge(bus)
    
    logger.info("UPS monitoring started")
    logger.info(f"Critical voltage threshold: {CRITICAL_VOLTAGE_THRESHOLD}V")
    logger.info("AC power loss will only trigger shutdown when combined with critical voltage")
        
    while True:
        failure_counter = 0

        for _ in range(SHUTDOWN_THRESHOLD):
            # Read sensor values
            # Dopo (v2):
            value = request.get_value(PLD_PIN)
            ac_power_state = 1 if value == Value.ACTIVE else 0
            voltage = readVoltage(bus)
            capacity = readCapacity(bus)
            battery_status = get_battery_status(voltage)
            
            # Control charging if charge control is available
            if request is not None:
                charging_enabled = control_charging(request, voltage, charging_enabled)
            
            # Display comprehensive system statistics
            display_system_stats(voltage, capacity, charging_enabled, ac_power_state)
            
            # Check for critical conditions using new logic
            critical_conditions = check_critical_conditions(ac_power_state, voltage, capacity)
            
            if critical_conditions:
                for condition in critical_conditions:
                    logger.warning(f"Critical condition detected: {condition}")
                failure_counter += 1
            else:
                # Reset failure counter if no critical conditions
                failure_counter = 0
                
                # Log status for AC power loss without critical conditions
                if ac_power_state == 0:
                    logger.info("AC power loss detected, but battery voltage is above critical threshold - continuing operation")
                
                break

            if failure_counter < SHUTDOWN_THRESHOLD:
                time.sleep(SLEEP_TIME)

        # Handle shutdown conditions
        if failure_counter >= SHUTDOWN_THRESHOLD:
            critical_conditions = check_critical_conditions(ac_power_state, voltage, capacity)
            shutdown_reason = "due to: " + ", ".join(critical_conditions)

            shutdown_message = f"Critical conditions met {shutdown_reason}. Initiating shutdown."
            logger.critical(shutdown_message)
            print(shutdown_message)
            
            # Disable charging before shutdown
            if request is not None:
                try:
                    request.set_value(
                        CHARGE_CONTROL_PIN,
                        Value.INACTIVE if CHARGE_ENABLE_STATE == 0 else Value.ACTIVE
                    )
                    logger.info("Charging disabled before shutdown")
                except Exception as e:  # noqa: BLE001
                    logger.error(f"Error disabling charging before shutdown: {e}")
            
            call("sudo nohup shutdown -h now", shell=True)
        else:
            if Loop:
                time.sleep(SLEEP_TIME)
            else:
                logger.info("Single check completed, exiting")
                break

except KeyboardInterrupt:
    logger.info("Script interrupted by user")
except Exception as e:  # noqa: BLE001
    import traceback
    logger.error(f"Unexpected error: {e}")
    logger.error(traceback.format_exc())
finally:
    # Cleanup libgpiod v2
    if request is not None:
        with contextlib.suppress(Exception):
            request.release()

    logger.info("UPS monitoring stopped")
    sys.exit(0)
