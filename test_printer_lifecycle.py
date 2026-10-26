import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import printer_lifecycle as p

IDLE = {'print_stats': {'state': 'standby'}, 'pause_resume': {'is_paused': False},
        'idle_timeout': {'state': 'Idle'}, 'toolhead': {'print_time': 5, 'estimated_print_time': 10},
        'configfile': {'settings': {'delayed_gcode preheat_pla': {'initial_duration': 3, 'gcode': '{% set preheat_pla = false %}'}}}}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.states = {u: 'inactive' for u in p.UNITS}
        self.states[p.UNITS[0]] = 'active'
        self.calls = []
        def control(action, unit):
            self.calls.append((action, unit))
            self.states[unit] = 'inactive' if action == 'stop' else 'active'
        patches = [patch.object(p, 'service_state', side_effect=lambda u: self.states[u]),
                   patch.object(p, 'control', side_effect=control),
                   patch.object(p, 'readiness', return_value={'ready': True}),
                   patch.object(p, 'wait_ready'), patch.object(p, 'boot_id', return_value='test-boot'),
                   patch.object(p.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'running', '')),
                   patch.dict(os.environ, {'INVOCATION_ID': 'test-run'})]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)

    def test_success_restores_only_previously_active(self):
        with p.stopped_printers(self.path):
            self.assertEqual(self.states[p.UNITS[0]], 'inactive')
        self.assertEqual(self.calls, [('stop', p.UNITS[0]), ('start', p.UNITS[0])])
        self.assertTrue(json.loads(self.path.read_text())['restored'])

    def test_failure_restores_printer(self):
        with self.assertRaisesRegex(RuntimeError, 'backup failed'):
            with p.stopped_printers(self.path):
                raise RuntimeError('backup failed')
        self.assertEqual(self.states[p.UNITS[0]], 'active')

    def test_interrupt_restores_printer(self):
        with self.assertRaises(KeyboardInterrupt):
            with p.stopped_printers(self.path):
                raise KeyboardInterrupt()
        self.assertEqual(self.states[p.UNITS[0]], 'active')

    def test_previously_stopped_stays_stopped(self):
        self.states[p.UNITS[0]] = 'inactive'
        with p.stopped_printers(self.path):
            pass
        self.assertEqual(self.calls, [])

    def test_busy_printer_causes_no_stop(self):
        with patch.object(p, 'readiness', side_effect=RuntimeError('printing')):
            with self.assertRaisesRegex(RuntimeError, 'printing'):
                with p.stopped_printers(self.path):
                    self.fail('Must not enter backup')
        self.assertEqual(self.calls, [])
        self.assertFalse(self.path.exists())

    def test_partial_stop_failure_still_restores(self):
        original = p.control.side_effect
        def broken(action, unit):
            original(action, unit)
            if action == 'stop': raise RuntimeError('stop failed')
        with patch.object(p, 'control', side_effect=broken):
            with self.assertRaisesRegex(RuntimeError, 'stop failed'):
                with p.stopped_printers(self.path): pass
        self.assertEqual(self.states[p.UNITS[0]], 'active')

    def test_stop_post_recovers_unfinished_same_invocation(self):
        p.write_state(self.path, {'boot_id':'test-boot','invocation':'test-run',
                                'originally_active':[p.UNITS[0]],'restored':False})
        self.states[p.UNITS[0]] = 'inactive'
        p.restore(self.path)
        self.assertEqual(self.states[p.UNITS[0]], 'active')
        p.restore(self.path)
        self.assertEqual(self.calls, [('start', p.UNITS[0])])

    def test_wrong_invocation_cannot_restart_services(self):
        p.write_state(self.path, {'boot_id':'test-boot','invocation':'another-run',
                                'originally_active':[p.UNITS[0]],'restored':False})
        with self.assertRaisesRegex(RuntimeError, 'Another backup'): p.restore(self.path)
        self.assertEqual(self.calls, [])

    def test_completed_prior_record_is_harmless(self):
        p.write_state(self.path, {'boot_id':'old-boot','invocation':'old-run','restored':True})
        p.restore(self.path)
        self.assertEqual(self.calls, [])

    def test_restart_failure_is_not_silently_successful(self):
        with patch.object(p, 'wait_ready', side_effect=RuntimeError('MCU missing')):
            with self.assertRaisesRegex(RuntimeError, 'needs attention'):
                with p.stopped_printers(self.path): pass
        self.assertFalse(json.loads(self.path.read_text())['restored'])


class IdleGuards(unittest.TestCase):
    def test_idle_and_cold_allowed(self):
        p.validate_idle(IDLE, {'extruder': {'target': 0, 'power': 0}})

    def test_preheat_variable_controls_guard_regardless_of_duration(self):
        for value in ('false', 'False', "'false'", '"False"', 'true', "'true'", 'unknown', ''):
            for duration in (0, 3):
                state = copy.deepcopy(IDLE)
                macro = state['configfile']['settings']['delayed_gcode preheat_pla']
                macro.update(initial_duration=duration, gcode='{% set preheat_pla = ' + value + ' %}')
                with self.subTest(value=value, duration=duration):
                    if 'false' in value.lower():
                        p.validate_idle(state, {})
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'preheat_pla'):
                            p.validate_idle(state, {})

    def test_missing_macro_allowed_but_ambiguous_variable_refused(self):
        state = copy.deepcopy(IDLE)
        state['configfile']['settings'] = {}
        p.validate_idle(state, {})
        for code in ('G4 P1', '{% set preheat_pla = false %}\n{% set preheat_pla = true %}'):
            state['configfile']['settings'] = {'delayed_gcode preheat_pla': {'gcode': code}}
            with self.assertRaisesRegex(RuntimeError, 'Cannot determine preheat_pla'):
                p.validate_idle(state, {})

    def test_busy_paused_heating_motion_and_preheat_refused(self):
        cases = [('print_stats','state','printing'),('print_stats','state','paused'),
                 ('pause_resume','is_paused',True),('idle_timeout','state','Printing'),
                 ('toolhead','print_time',20)]
        for section, key, value in cases:
            state = copy.deepcopy(IDLE); state[section][key] = value
            with self.subTest(section=section, value=value):
                with self.assertRaises(RuntimeError): p.validate_idle(state, {})
        for heater in ({'target':30,'power':0}, {'target':0,'power':0.2}):
            with self.assertRaisesRegex(RuntimeError, 'heater'): p.validate_idle(IDLE, {'bed':heater})
        state = copy.deepcopy(IDLE)
        state['configfile']['settings']['delayed_gcode preheat_pla']['gcode'] = '{% set preheat_pla = true %}'
        with self.assertRaisesRegex(RuntimeError, 'preheat'): p.validate_idle(state, {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
