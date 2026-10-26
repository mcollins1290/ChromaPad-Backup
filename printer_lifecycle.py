"""Remember and restore printer service state around a backup; never interrupt a print."""
import configparser
import contextlib
import json
import os
import re
from pathlib import Path
import subprocess
import time
import urllib.parse
import urllib.request

STATE = Path('/run/chromapad-backup-printers.json')
UNITS = tuple(f'klipper-{i}.service' for i in range(1, 9))


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def service_state(unit):
    return subprocess.check_output(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'],
                                   text=True, timeout=15).strip()


def control(action, unit):
    subprocess.run(['systemctl', action, unit], check=True, timeout=40)


def write_state(path, value):
    pending = path.with_suffix('.pending')
    with pending.open('w') as f:
        os.chmod(pending, 0o600)
        json.dump(value, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(pending, path)


def endpoint(unit):
    require(unit in UNITS, 'Unexpected printer service')
    number = unit.split('-')[1].split('.')[0]
    data = Path('/home/cp') / ('printer_' + number + '_data')
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.read(data / 'config/moonraker.conf')
    port = config.getint('server', 'port')
    require(1 <= port <= 65535, 'Invalid Moonraker port')
    return 'http://127.0.0.1:' + str(port), str(data / 'comms/klippy.sock')


def api(base, path, data=None):
    request = urllib.request.Request(base + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)['result']


def validate_idle(status, heaters):
    require(status['print_stats']['state'] in ('standby', 'complete', 'cancelled'),
            'Printer is printing, paused, or in an unsafe state')
    require(not status['pause_resume']['is_paused'], 'Printer is paused')
    require(status['idle_timeout']['state'] != 'Printing', 'Printer is executing commands; wait until idle')
    require(status['toolhead']['print_time'] <= status['toolhead']['estimated_print_time'] + 0.1,
            'Printer has queued movement')
    for name, heater in heaters.items():
        require(heater['target'] == 0 and heater['power'] == 0,
                'Turn off heater before backup: ' + name)
    preheat = status['configfile']['settings'].get('delayed_gcode preheat_pla', {})
    if preheat:
        assignments = re.findall(r"\{%[-+]?\s*set\s+preheat_pla\s*=\s*(.*?)\s*[-+]?%\}",
                                 preheat.get('gcode', ''), flags=re.DOTALL)
        require(len(assignments) == 1,
                'Cannot determine preheat_pla variable before automatic Klipper restart')
        value = assignments[0].strip()
        require(value.lower() in ('true', 'false', "'true'", "'false'", '"true"', '"false"'),
                'Cannot determine preheat_pla variable before automatic Klipper restart')
        require(value.strip("\"'").lower() == 'false',
                'preheat_pla must be false before automatic Klipper restart')


def readiness(unit):
    base, socket = endpoint(unit)
    config = api(base, '/server/config')['config']['server']
    require(config['klippy_uds_address'] == socket, 'Moonraker belongs to another printer')
    require(api(base, '/server/info')['klippy_state'] == 'ready', 'Klipper must be ready before backup')
    status = api(base, '/printer/objects/query?heaters&print_stats&pause_resume&idle_timeout&toolhead&configfile=settings')['status']
    names = status['heaters']['available_heaters']
    heaters = api(base, '/printer/objects/query?' + '&'.join(urllib.parse.quote(n, safe='') for n in names))['status'] if names else {}
    validate_idle(status, heaters)
    return {'unit': unit, 'state': status['print_stats']['state'], 'heater_targets': {n: h['target'] for n, h in heaters.items()}}


def plan():
    active = []
    checks = []
    for unit in UNITS:
        state = service_state(unit)
        require(state in ('active', 'inactive'), unit + ' is not in a stable service state: ' + state)
        if state == 'active':
            active.append(unit)
            checks.append(readiness(unit))
    return {'active_printer_services': active, 'printer_checks': checks,
            'ready': True, 'automatic_stop_and_restore': True}


def wait_ready(unit):
    base, _ = endpoint(unit)
    deadline = time.monotonic() + 45
    last_error = None
    while time.monotonic() < deadline:
        try:
            if api(base, '/server/info')['klippy_state'] == 'ready':
                names = api(base, '/printer/objects/query?heaters')['status']['heaters']['available_heaters']
                heaters = api(base, '/printer/objects/query?' + '&'.join(urllib.parse.quote(n, safe='') for n in names))['status'] if names else {}
                if any(h['target'] != 0 for h in heaters.values()):
                    api(base, '/printer/gcode/script', {'script': 'TURN_OFF_HEATERS'})
                    raise RuntimeError('Unexpected heater target after restart; heaters turned off')
                return
        except (OSError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise RuntimeError('Klipper did not return ready: ' + unit + '; ' + str(last_error))


def restore(path=STATE):
    if not path.exists():
        return
    state = json.loads(path.read_text())
    if state.get('restored'):
        return
    require(state['boot_id'] == boot_id(), 'Stale printer restoration record from another boot')
    require(state['invocation'] == os.environ.get('INVOCATION_ID', ''), 'Another backup owns the printer restoration record')
    for unit in state['originally_active']:
        require(unit in UNITS, 'Invalid saved printer service')
    # Do not fight a machine shutdown. Enablement was never changed, so normal
    # boot starts the configured instances again.
    system = subprocess.run(['systemctl', 'is-system-running'], capture_output=True, text=True).stdout.strip()
    if system == 'stopping':
        print('Machine is shutting down; printer restart deferred to normal boot.', flush=True)
        return
    errors = []
    for unit in state['originally_active']:
        try:
            print('Restoring ' + unit, flush=True)
            control('start', unit)
            wait_ready(unit)
        except Exception as exc:
            errors.append(unit + ': ' + str(exc))
    state['restored'] = not errors
    state['restore_errors'] = errors
    write_state(path, state)
    require(not errors, 'Printer restart needs attention: ' + '; '.join(errors))
    print('Previously running Klipper services restored and ready; heaters off.', flush=True)


@contextlib.contextmanager
def stopped_printers(path=STATE):
    checked = plan()  # Recheck immediately before stopping anything.
    previous = json.loads(path.read_text()) if path.exists() else None
    require(not previous or previous.get('restored') is True,
            'Previous backup has an unresolved printer restoration record')
    state = {'boot_id': boot_id(), 'invocation': os.environ.get('INVOCATION_ID', ''),
             'originally_active': checked['active_printer_services'], 'restored': False,
             'printer_checks': checked['printer_checks']}
    write_state(path, state)
    try:
        for unit in state['originally_active']:
            print('Stopping idle printer service ' + unit, flush=True)
            control('stop', unit)
            require(service_state(unit) == 'inactive', 'Printer service did not stop: ' + unit)
        yield
    finally:
        restore(path)


if __name__ == '__main__':
    restore()
