import os
import unittest

from winappdbg.system import System


def get_calc_path_from_path_env():
    """
    Attempts to find calc.exe by searching directories in the PATH environment variable.
    """
    path_env = os.environ.get('PATH')
    if not path_env:
        return None

    path_dirs = path_env.split(os.pathsep)
    for directory in path_dirs:
        calc_path = os.path.join(directory, 'calc.exe')
        if os.path.isfile(calc_path):
            return calc_path
    return None


class TestStart(unittest.TestCase):
    def test_start(self):
        # Instance a System object.
        system = System()

        # Get the calc path and start it.
        calc_path = get_calc_path_from_path_env()
        command_line = system.argv_to_cmdline([calc_path])

        # Start a new process.
        process = system.start_process(command_line)  # see the docs for more options

        # Show info on the new process.
        self.assertIsInstance(process.get_pid(), int)
        self.assertIsInstance(process.get_bits(), int)
