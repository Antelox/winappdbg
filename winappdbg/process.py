#!/usr/bin/python3
# -*- coding: utf-8 -*-

# Copyright (c) 2009-2025, Mario Vilas
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice,this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the copyright holder nor the names of its
#       contributors may be used to endorse or promote products derived from
#       this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""
Process instrumentation.

This module contains the logic for handling process lifetime, memory, etc.
"""

# FIXME
# I've been told the host process for the latest versions of VMWare
# can't be instrumented, because they try to stop code injection into the VMs.
# The solution appears to be to run the debugger from a user account that
# belongs to the VMware group. I haven't confirmed this yet.

__all__ = ["Process"]

import ctypes
import ntpath
import re
import struct
import traceback
import warnings
from os import getenv

from . import win32
from .win32.version import _machine_to_arch_map
from .disasm import Disassembler
from .module import _ModuleContainer
from .search import HexPattern, IStringPattern, Pattern, Search, StringPattern
from .textio import HexDump, HexInput
from .thread import Thread, _ThreadContainer
from .util import MemoryAddresses, PathOperations, Regenerator
from .window import Window

# delayed import
System = None

# ==============================================================================

# TODO
# * Remote GetLastError()
# * The memory operation methods do not take into account that code breakpoints
#   change the memory. This object should talk to BreakpointContainer to
#   retrieve the original memory contents where code breakpoints are enabled.
# * A memory cache could be implemented here.


class Process(_ThreadContainer, _ModuleContainer):
    """
    Interface to a process. Contains threads and modules snapshots.

    :ivar dwProcessId: Global process ID. Use :meth:`get_pid` instead.
    :type dwProcessId: int
    :ivar hProcess: Handle to the process. Use :meth:`get_handle` instead.
    :type hProcess: ~win32.ProcessHandle
    :ivar fileName: Filename of the main module. Use :meth:`get_filename` instead.
    :type fileName: str
    """

    def __init__(self, dwProcessId, hProcess=None, fileName=None):
        """
        :param dwProcessId: Global process ID.
        :type  dwProcessId: int
        :param hProcess: Handle to the process.
        :type  hProcess: ~win32.ProcessHandle
        :param fileName: (Optional) Filename of the main module.
        :type  fileName: str
        """
        _ThreadContainer.__init__(self)
        _ModuleContainer.__init__(self)

        self.dwProcessId = dwProcessId
        self.hProcess = hProcess
        self.fileName = fileName

    def get_pid(self):
        """
        :rtype:  int
        :return: Process global ID.
        """
        return self.dwProcessId

    def get_filename(self):
        """
        :rtype:  str
        :return: Filename of the main module of the process.
        """
        if not self.fileName:
            self.fileName = self.get_image_name()
        if isinstance(self.fileName, bytes):
            self.fileName = self.fileName.decode("mbcs", "ignore")
        return self.fileName

    def open_handle(self, dwDesiredAccess=win32.PROCESS_ALL_ACCESS):
        """
        Opens a new handle to the process.

        The new handle is stored in the :attr:`hProcess` property.

        .. warning::

            Normally you should call :meth:`get_handle` instead, since it's much
            "smarter" and tries to reuse handles and merge access rights.

        :param int dwDesiredAccess: Desired access rights.
            Defaults to :const:`win32.PROCESS_ALL_ACCESS`.
            See: http://msdn.microsoft.com/en-us/library/windows/desktop/ms684880(v=vs.85).aspx

        :raises WindowsError: It's not possible to open a handle to the process
            with the requested access rights. This typically happens because
            the target process is a system process and the debugger is not
            runnning with administrative rights.
        """
        hProcess = win32.OpenProcess(dwDesiredAccess, win32.FALSE, self.dwProcessId)

        try:
            self.close_handle()
        except Exception:
            warnings.warn("Failed to close process handle: %s" % traceback.format_exc())

        self.hProcess = hProcess

    def close_handle(self):
        """
        Closes the handle to the process.

        .. note::

            Normally you don't need to call this method. All handles
            created by *WinAppDbg* are automatically closed when the garbage
            collector claims them. So unless you've been tinkering with it,
            setting :attr:`hProcess` to ``None`` should be enough.
        """
        try:
            if hasattr(self.hProcess, "close"):
                self.hProcess.close()
            elif (
                self.hProcess is not None
                and self.hProcess != win32.INVALID_HANDLE_VALUE
            ):
                win32.CloseHandle(self.hProcess)
        finally:
            self.hProcess = None

    def get_handle(self, dwDesiredAccess=win32.PROCESS_ALL_ACCESS):
        """
        Returns a handle to the process with *at least* the access rights
        requested.

        .. note::

            If a handle was previously opened and has the required access
            rights, it's reused. If not, a new handle is opened with the
            combination of the old and new access rights.

        :param int dwDesiredAccess: Desired access rights.
            Defaults to :const:`win32.PROCESS_ALL_ACCESS`.
            See: http://msdn.microsoft.com/en-us/library/windows/desktop/ms684880(v=vs.85).aspx

        :rtype:  ~win32.ProcessHandle
        :return: Handle to the process.

        :raises WindowsError: It's not possible to open a handle to the process
            with the requested access rights. This typically happens because
            the target process is a system process and the debugger is not
            runnning with administrative rights.
        """
        if self.hProcess is None or self.hProcess == win32.INVALID_HANDLE_VALUE:
            self.open_handle(dwDesiredAccess)
        else:
            dwAccess = self.hProcess.dwAccess
            if (dwAccess | dwDesiredAccess) != dwAccess:
                self.open_handle(dwAccess | dwDesiredAccess)
        return self.hProcess

    # Return the list of Window objects
    def get_windows(self):
        """
        Get the list of top-level windows owned by this process.

        :rtype: list of :class:`~.window.Window`
        :return: List of top-level windows for this process.
        """
        hCurWnd = None
        window_list = list()
        while True:
            hCurWnd = win32.FindWindowEx(None, hCurWnd, None, None)
            if hCurWnd is None:
                break
            sdwThreadId, dwProcessId = win32.GetWindowThreadProcessId(hCurWnd)
            if dwProcessId == self.dwProcessId:
                window_list.append(Window(hCurWnd))
        return window_list

    # ------------------------------------------------------------------------------

    def __contains__(self, anObject):
        """
        The same as: ``self.has_thread(anObject) or self.has_module(anObject)``

        :param anObject: Object to look for.
            Can be a :class:`~.Thread`, :class:`~.Module`, thread global ID or
            module base address.
        :type  anObject: :class:`~.Thread`, :class:`~.Module` or int

        :rtype:  bool
        :return: ``True`` if the requested object was found in the snapshot.
        """
        return _ThreadContainer.__contains__(
            self, anObject
        ) or _ModuleContainer.__contains__(self, anObject)

    def __len__(self):
        """
        :see: :meth:`get_thread_count`, :meth:`get_module_count`
        :rtype: int
        :return: Count of :class:`~.Thread` and :class:`~.Module` objects in this snapshot.
        """
        return _ThreadContainer.__len__(self) + _ModuleContainer.__len__(self)

    class __ThreadsAndModulesIterator:
        """
        Iterator object for :class:`~.Process` objects.
        Iterates through :class:`~.Thread` objects first, :class:`~.Module` objects next.
        """

        def __init__(self, container):
            """
            :param container: :class:`~.Thread` and :class:`~.Module` container.
            :type  container: :class:`~.Process`
            """
            self.__container = container
            self.__iterator = None
            self.__state = 0

        def __iter__(self):
            return self

        def __next__(self):
            if self.__state == 0:
                self.__iterator = self.__container.iter_threads()
                self.__state = 1
            if self.__state == 1:
                try:
                    return next(self.__iterator)
                except StopIteration:
                    self.__iterator = self.__container.iter_modules()
                    self.__state = 2
            if self.__state == 2:
                try:
                    return next(self.__iterator)
                except StopIteration:
                    self.__iterator = None
                    self.__state = 3
            raise StopIteration

    def __iter__(self):
        """
        :see:    :meth:`iter_threads`, :meth:`iter_modules`
        :rtype:  iterator
        :return: Iterator of :class:`~.Thread` and :class:`~.Module` objects in this snapshot.
            All threads are iterated first, then all modules.
        """
        return self.__ThreadsAndModulesIterator(self)

    # ------------------------------------------------------------------------------

    def wait(self, dwTimeout=None):
        """
        Waits for the process to finish executing.

        :raises WindowsError: On error an exception is raised.
        """
        self.get_handle(win32.SYNCHRONIZE).wait(dwTimeout)

    def kill(self, dwExitCode=0):
        """
        Terminates the execution of the process.

        :raises WindowsError: On error an exception is raised.
        """
        hProcess = self.get_handle(win32.PROCESS_TERMINATE)
        win32.TerminateProcess(hProcess, dwExitCode)

    def suspend(self):
        """
        Suspends execution on all threads of the process.

        :raises WindowsError: On error an exception is raised.
        """
        self.scan_threads()  # force refresh the snapshot
        suspended = list()
        try:
            for aThread in self.iter_threads():
                aThread.suspend()
                suspended.append(aThread)
        except Exception:
            for aThread in suspended:
                try:
                    aThread.resume()
                except Exception:
                    pass
            raise

    def resume(self):
        """
        Resumes execution on all threads of the process.

        :raises WindowsError: On error an exception is raised.
        """
        if self.get_thread_count() == 0:
            self.scan_threads()  # only refresh the snapshot if empty
        resumed = list()
        try:
            for aThread in self.iter_threads():
                aThread.resume()
                resumed.append(aThread)
        except Exception:
            for aThread in resumed:
                try:
                    aThread.suspend()
                except Exception:
                    pass
            raise

    def is_debugged(self):
        """
        Tries to determine if the process is being debugged by another process.
        It may detect other debuggers besides WinAppDbg.

        :rtype:  bool
        :return: ``True`` if the process has a debugger attached.

        .. warning::

            May return inaccurate results when some anti-debug techniques are
            used by the target process.

        .. note::

            To know if a process currently being debugged by a :class:`~debug.Debug`
            object, call :meth:`~debug.Debug.is_debugee` instead.
        """
        # FIXME the MSDN docs don't say what access rights are needed here!
        hProcess = self.get_handle(win32.PROCESS_QUERY_INFORMATION)
        return win32.CheckRemoteDebuggerPresent(hProcess)

    def is_alive(self):
        """
        :rtype: bool
        :return: ``True`` if the process is currently running.
        """
        try:
            self.wait(0)
        except WindowsError as e:
            return e.winerror == win32.WAIT_TIMEOUT
        return False

    def get_exit_code(self):
        """
        :rtype:  int
        :return: Process exit code, or ``STILL_ACTIVE`` if it's still alive.

        .. warning::

            If a process returns ``STILL_ACTIVE`` as its exit code,
            you may not be able to determine if it's active or not with this
            method. Use :meth:`is_alive` to check if the process is still
            active. Alternatively, you can call :meth:`get_handle` to get the
            handle object and then call the ``wait`` method on it to wait
            until the process finishes running.
        """
        if hasattr(win32, "PROCESS_QUERY_LIMITED_INFORMATION"):
            dwAccess = win32.PROCESS_QUERY_LIMITED_INFORMATION
        else:
            dwAccess = win32.PROCESS_QUERY_INFORMATION
        return win32.GetExitCodeProcess(self.get_handle(dwAccess))

    # ------------------------------------------------------------------------------

    def scan(self):
        """
        Populates the snapshot of threads and modules.
        """
        self.scan_threads()
        self.scan_modules()

    def clear(self):
        """
        Clears the snapshot of threads and modules.
        """
        try:
            try:
                self.clear_threads()
            finally:
                self.clear_modules()
        finally:
            self.close_handle()

    # ------------------------------------------------------------------------------

    # Regular expression to find hexadecimal values of any size.
    __hexa_parameter = re.compile("0x[0-9A-Fa-f]+")

    def __fixup_labels(self, disasm):
        """
        Private method used when disassembling from process memory.

        It has no return value because the list is modified in place. On return
        all raw memory addresses are replaced by labels when possible.

        :param list[tuple[int, int, str, str]] disasm:
            Output of one of the dissassembly functions.
        """
        for index in range(len(disasm)):
            (address, size, text, dump) = disasm[index]
            m = self.__hexa_parameter.search(text)
            while m:
                s, e = m.span()
                value = text[s:e]
                try:
                    label = self.get_label_at_address(int(value, 0x10))
                except Exception:
                    label = None
                if label:
                    text = text[:s] + label + text[e:]
                    e = s + len(value)
                m = self.__hexa_parameter.search(text, e)
            disasm[index] = (address, size, text, dump)

    def disassemble_string(self, lpAddress, code):
        """
        Disassemble instructions from a block of binary code.

        :param int lpAddress: Memory address where the code was read from.
        :param bytes code: Binary code to disassemble.
        :rtype: list[tuple[int, int, str, str]]
        :return: List of tuples. Each tuple represents an assembly instruction
            and contains:

            - Memory address of instruction.
            - Size of instruction in bytes.
            - Disassembly line of instruction.
            - Hexadecimal dump of instruction.

        :raises NotImplementedError:
            No compatible disassembler was found for the current platform.
        """
        try:
            disasm = self.__disasm
        except AttributeError:
            disasm = self.__disasm = Disassembler(self.get_arch())
        return disasm.decode(lpAddress, code)

    def disassemble(self, lpAddress, dwSize):
        """
        Disassemble instructions from the address space of the process.

        :param int lpAddress: Memory address where to read the code from.
        :param int dwSize: Size of binary code to disassemble.
        :rtype: list[tuple[int, int, str, str]]
        :return: List of tuples. Each tuple represents an assembly instruction
            and contains:

            - Memory address of instruction.
            - Size of instruction in bytes.
            - Disassembly line of instruction.
            - Hexadecimal dump of instruction.
        """
        data = self.read(lpAddress, dwSize)
        disasm = self.disassemble_string(lpAddress, data)
        self.__fixup_labels(disasm)
        return disasm

    # FIXME
    # This algorithm really sucks, I've got to write a better one :P
    def disassemble_around(self, lpAddress, dwSize=64):
        """
        Disassemble around the given address.

        :param int lpAddress: Memory address where to read the code from.
        :param int dwSize: Delta offset.
            Code will be read from ``lpAddress - dwSize`` to
            ``lpAddress + dwSize``.
        :rtype: list[tuple[int, int, str, str]]
        :return: List of tuples. Each tuple represents an assembly instruction
            and contains:

            - Memory address of instruction.
            - Size of instruction in bytes.
            - Disassembly line of instruction.
            - Hexadecimal dump of instruction.
        """
        dwDelta = int(float(dwSize) / 2.0)
        addr_1 = lpAddress - dwDelta
        addr_2 = lpAddress
        size_1 = dwDelta
        # size_2   = dwSize - dwDelta
        data = self.read(addr_1, dwSize)
        data_1 = data[:size_1]
        data_2 = data[size_1:]
        disasm_1 = self.disassemble_string(addr_1, data_1)
        disasm_2 = self.disassemble_string(addr_2, data_2)
        disasm = disasm_1 + disasm_2
        self.__fixup_labels(disasm)
        return disasm

    def disassemble_around_pc(self, dwThreadId, dwSize=64):
        """
        Disassemble around the program counter of the given thread.

        :param int dwThreadId: Global thread ID. The program counter for this
            thread will be used as the disassembly address.
        :param int dwSize: Delta offset. Code will be read from ``pc - dwSize``
            to ``pc + dwSize``.
        :rtype: list[tuple[int, int, str, str]]
        :return: List of tuples. Each tuple represents an assembly instruction
            and contains:

            - Memory address of instruction.
            - Size of instruction in bytes.
            - Disassembly line of instruction.
            - Hexadecimal dump of instruction.
        """
        aThread = self.get_thread(dwThreadId)
        return self.disassemble_around(aThread.get_pc(), dwSize)

    def disassemble_instruction(self, lpAddress):
        """
        Disassemble the instruction at the given memory address.

        :param int lpAddress: Memory address where to read the code from.
        :rtype: tuple[int, int, str, str]
        :return: The tuple represents an assembly instruction and contains:

             - Memory address of instruction.
             - Size of instruction in bytes.
             - Disassembly line of instruction.
             - Hexadecimal dump of instruction.
        """
        return self.disassemble(lpAddress, 15)[0]

    def disassemble_current(self, dwThreadId):
        """
        Disassemble the instruction at the program counter of the given thread.

        :param int dwThreadId: Global thread ID. The program counter for this
            thread will be used as the disassembly address.
        :rtype: tuple[int, int, str, str]
        :return: The tuple represents an assembly instruction and contains:

             - Memory address of instruction.
             - Size of instruction in bytes.
             - Disassembly line of instruction.
             - Hexadecimal dump of instruction.
        """
        aThread = self.get_thread(dwThreadId)
        return self.disassemble_instruction(aThread.get_pc())

    # ------------------------------------------------------------------------------

    def flush_instruction_cache(self):
        """
        Flush the instruction cache. This is required if the process memory is
        modified and one or more threads are executing nearby the modified
        memory region.

        See: http://blogs.msdn.com/oldnewthing/archive/2003/12/08/55954.aspx#55958

        :raises WindowsError: Raises exception on error.
        """
        # FIXME
        # No idea what access rights are required here!
        # Maybe PROCESS_VM_OPERATION ???
        # In any case we're only calling this from the debugger,
        # so it should be fine (we already have PROCESS_ALL_ACCESS).
        win32.FlushInstructionCache(self.get_handle())

    def debug_break(self):
        """
        Triggers the system breakpoint in the process.

        :raises WindowsError: On error an exception is raised.
        """
        # The exception is raised by a new thread.
        # When continuing the exception, the thread dies by itself.
        # This thread is hidden from the debugger.
        win32.DebugBreakProcess(self.get_handle())

    def is_wow64(self):
        """
        Determines if the process is running under WOW64.

        :rtype:  bool
        :return:
            ``True`` if the process is running under WOW64. That is, a 32-bit
            application running in a 64-bit Windows.

            ``False`` if the process is either a 32-bit application running in
            a 32-bit Windows, or a 64-bit application running in a 64-bit
            Windows.

        :raises WindowsError: On error an exception is raised.

        See: http://msdn.microsoft.com/en-us/library/aa384249(VS.85).aspx
        """
        try:
            wow64 = self.__wow64
        except AttributeError:
            if win32.bits == 32 and not win32.wow64:
                wow64 = False
            else:
                try:
                    if hasattr(win32, "PROCESS_QUERY_LIMITED_INFORMATION"):
                        dwAccess = win32.PROCESS_QUERY_LIMITED_INFORMATION
                    else:
                        dwAccess = win32.PROCESS_QUERY_INFORMATION
                    hProcess = self.get_handle(dwAccess)
                    try:
                        wow64 = win32.IsWow64Process(hProcess)
                    except AttributeError:
                        wow64 = False
                except WindowsError:
                    # Can't access the process, assume not WOW64.
                    wow64 = False
            self.__wow64 = wow64
        return wow64

    def get_arch(self):
        """
        :rtype:  str
        :return: The architecture in which this process believes to be running.
            For example, if running a 32 bit binary in a 64 bit machine, the
            architecture returned by this method will be :data:`~winappdbg.win32.ARCH_I386`,
            but the value of ``System.arch`` will be :data:`~winappdbg.win32.ARCH_AMD64`.

            On ARM64 systems, this can detect:
            - Native ARM64 processes
            - x64 processes running under emulation
            - x86 processes running under emulation
        """

        # Try to use the newer GetProcessInformation API first (Windows 8+).
        # This is the most accurate method and works correctly on ARM64 systems.
        try:
            if hasattr(win32, "PROCESS_QUERY_LIMITED_INFORMATION"):
                dwAccess = win32.PROCESS_QUERY_LIMITED_INFORMATION
            else:
                dwAccess = win32.PROCESS_QUERY_INFORMATION
            hProcess = self.get_handle(dwAccess)

            # Use GetProcessInformation with ProcessMachineTypeInfo.
            if hasattr(win32, "GetProcessInformation") and hasattr(
                win32, "PROCESS_INFORMATION_CLASS_KERNEL32"
            ):
                machine_info = win32.GetProcessInformation(
                    hProcess,
                    win32.PROCESS_INFORMATION_CLASS_KERNEL32.ProcessMachineTypeInfo,
                )

                # Get the process architecture from the machine type.
                process_arch = _machine_to_arch_map.get(machine_info.ProcessMachine)
                if process_arch is not None:
                    return process_arch
                raise NotImplementedError(
                    f"Unsupported process machine type: 0x{machine_info.ProcessMachine:04X}"
                )

        except (AttributeError, WindowsError):
            # Fall back to the older methods if GetProcessInformation fails.
            pass

        # Fall back to the legacy logic for older systems.
        if win32.arch == win32.ARCH_AMD64 or win32.arch == win32.ARCH_I386:
            # Are we in a 32 bit machine?
            if win32.bits == 32 and not win32.wow64:
                return win32.arch

            # Is the process outside of WOW64?
            if not self.is_wow64():
                return win32.arch

            # Traditional x86 on x64.
            return win32.ARCH_I386

        # Add support for more architectures as needed.
        raise NotImplementedError(
            "Architecture detection not implemented for %s" % win32.arch
        )

    def get_bits(self):
        """
        :rtype: int
        :return: The number of bits in which this process believes to be
            running. For example, if running a 32 bit binary in a 64 bit
            machine, the number of bits returned by this method will be ``32``,
            but the value of :attr:`~winappdbg.system.System.arch` will be ``64``.
        """

        # Are we in a 32 bit machine?
        if win32.bits == 32 and not win32.wow64:
            # All processes are 32 bits.
            return 32

        # Is the process inside WOW64?
        if self.is_wow64():
            # The process is 32 bits.
            return 32

        # The process is 64 bits.
        return 64

    # TODO: get_os, to test compatibility run
    # See: http://msdn.microsoft.com/en-us/library/windows/desktop/ms683224(v=vs.85).aspx

    # ------------------------------------------------------------------------------

    def get_start_time(self):
        """
        Determines when has this process started running.

        :rtype: :class:`~win32.SYSTEMTIME`
        :return: Process start time.
        """
        if hasattr(win32, "PROCESS_QUERY_LIMITED_INFORMATION"):
            dwAccess = win32.PROCESS_QUERY_LIMITED_INFORMATION
        else:
            dwAccess = win32.PROCESS_QUERY_INFORMATION
        hProcess = self.get_handle(dwAccess)
        CreationTime = win32.GetProcessTimes(hProcess)[0]
        return win32.FileTimeToSystemTime(CreationTime)

    def get_exit_time(self):
        """
        Determines when has this process finished running.
        If the process is still alive, the current time is returned instead.

        :rtype: :class:`~win32.SYSTEMTIME`
        :return: Process exit time.
        """
        if self.is_alive():
            ExitTime = win32.GetSystemTimeAsFileTime()
        else:
            if hasattr(win32, "PROCESS_QUERY_LIMITED_INFORMATION"):
                dwAccess = win32.PROCESS_QUERY_LIMITED_INFORMATION
            else:
                dwAccess = win32.PROCESS_QUERY_INFORMATION
            hProcess = self.get_handle(dwAccess)
            ExitTime = win32.GetProcessTimes(hProcess)[1]
        return win32.FileTimeToSystemTime(ExitTime)

    def get_running_time(self):
        """
        Determines how long has this process been running.

        :rtype: int
        :return: Process running time in milliseconds.
        """
        if hasattr(win32, "PROCESS_QUERY_LIMITED_INFORMATION"):
            dwAccess = win32.PROCESS_QUERY_LIMITED_INFORMATION
        else:
            dwAccess = win32.PROCESS_QUERY_INFORMATION
        hProcess = self.get_handle(dwAccess)
        (CreationTime, ExitTime, _, _) = win32.GetProcessTimes(hProcess)
        if self.is_alive():
            ExitTime = win32.GetSystemTimeAsFileTime()
        CreationTime = CreationTime.dwLowDateTime + (CreationTime.dwHighDateTime << 32)
        ExitTime = ExitTime.dwLowDateTime + (ExitTime.dwHighDateTime << 32)
        RunningTime = ExitTime - CreationTime
        return RunningTime // 10000  # 100 nanoseconds steps => milliseconds

    # ------------------------------------------------------------------------------

    def __load_System_class(self):
        global System  # delayed import
        if System is None:
            from .system import System

    def get_services(self):
        """
        Retrieves the list of system services that are currently running in
        this process.

        .. seealso:: :meth:`winappdbg.system.System.get_services`

        :rtype: list of :class:`~win32.ServiceStatusProcessEntry`
        :return: List of service status descriptors.
        """
        self.__load_System_class()
        pid = self.get_pid()
        return [d for d in System.get_active_services() if d.ProcessId == pid]

    # ------------------------------------------------------------------------------

    def get_dep_policy(self):
        """
        Retrieves the DEP (Data Execution Prevention) policy for this process.

        .. note::

            This method is only available in Windows XP SP3 and above, and
            only for 32 bit processes. It will fail in any other circumstance.

        .. seealso:: http://msdn.microsoft.com/en-us/library/bb736297(v=vs.85).aspx

        :rtype: tuple(int, int)
        :return:
            The first member of the tuple is the DEP flags. It can be a
            combination of the following values:

            - 0: DEP is disabled for this process.
            - 1: DEP is enabled for this process. (``PROCESS_DEP_ENABLE``)
            - 2: DEP-ATL thunk emulation is disabled for this process.
              (``PROCESS_DEP_DISABLE_ATL_THUNK_EMULATION``)

            The second member of the tuple is the permanent flag. If ``True``
            the DEP settings cannot be changed in runtime for this process.

        :raises WindowsError: On error an exception is raised.
        """
        hProcess = self.get_handle(win32.PROCESS_QUERY_INFORMATION)
        try:
            return win32.kernel32.GetProcessDEPPolicy(hProcess)
        except AttributeError:
            msg = "This method is only available in Windows XP SP3 and above."
            raise NotImplementedError(msg)

    # ------------------------------------------------------------------------------

    def get_peb(self):
        """
        Returns a copy of the PEB.
        To dereference pointers in it call :meth:`.read_structure`.

        :rtype: :class:`~win32.PEB`
        :return: PEB structure.
        :raises WindowsError: An exception is raised on error.
        """
        self.get_handle(win32.PROCESS_VM_READ | win32.PROCESS_QUERY_INFORMATION)
        return self.read_structure(self.get_peb_address(), win32.PEB)

    def get_peb_address(self):
        """
        Returns a remote pointer to the PEB.

        :rtype: int
        :return: Remote pointer to the :class:`~win32.PEB` structure.
            Returns ``None`` on error.
        """
        try:
            return self._peb_ptr
        except AttributeError:
            hProcess = self.get_handle(win32.PROCESS_QUERY_INFORMATION)
            pbi = win32.NtQueryInformationProcess(
                hProcess, win32.ProcessBasicInformation
            )
            address = pbi.PebBaseAddress
            self._peb_ptr = address
            return address

    def get_entry_point(self):
        """
        Alias to ``process.get_main_module().get_entry_point()``.

        :rtype: int
        :return: Address of the entry point of the main module.
        """
        return self.get_main_module().get_entry_point()

    def get_main_module(self):
        """
        :rtype: :class:`~.module.Module`
        :return: Module object for the process main module.
        """
        return self.get_module(self.get_image_base())

    def get_image_base(self):
        """
        :rtype: int
        :return: Image base address for the process main module.
        """
        return self.get_peb().ImageBaseAddress

    def get_image_name(self):
        """
        :rtype: str
        :return: Filename of the process main module.

            This method does it's best to retrieve the filename.
            However sometimes this is not possible, so ``None`` may
            be returned instead.
        """

        # Method 1: Module.fileName
        # It's cached if the filename was already found by the other methods,
        # if it came with the corresponding debug event, or it was found by the
        # toolhelp API.
        mainModule = None
        try:
            mainModule = self.get_main_module()
            name = mainModule.fileName
            if not name:
                name = None
        except (KeyError, AttributeError, WindowsError):
            ##            traceback.print_exc()                               # XXX DEBUG
            name = None

        # Method 2: QueryFullProcessImageName()
        # Not implemented until Windows Vista.
        if not name:
            try:
                hProcess = self.get_handle(win32.PROCESS_QUERY_LIMITED_INFORMATION)
                name = win32.QueryFullProcessImageName(hProcess)
            except (AttributeError, WindowsError):
                ##                traceback.print_exc()                           # XXX DEBUG
                name = None

        # Method 3: GetProcessImageFileName()
        #
        # Not implemented until Windows XP.
        # For more info see:
        # https://voidnish.wordpress.com/2005/06/20/getprocessimagefilenamequerydosdevice-trivia/
        if not name:
            try:
                hProcess = self.get_handle(win32.PROCESS_QUERY_INFORMATION)
                name = win32.GetProcessImageFileName(hProcess)
                if name:
                    name = PathOperations.native_to_win32_pathname(name)
                else:
                    name = None
            except (AttributeError, WindowsError):
                ##                traceback.print_exc()                           # XXX DEBUG
                if not name:
                    name = None

        # Method 4: GetModuleFileNameEx()
        # Not implemented until Windows 2000.
        #
        # May be spoofed by malware, since this information resides
        # in usermode space (see http://www.ragestorm.net/blogs/?p=163).
        if not name:
            try:
                hProcess = self.get_handle(
                    win32.PROCESS_VM_READ | win32.PROCESS_QUERY_INFORMATION
                )
                try:
                    name = win32.GetModuleFileNameEx(hProcess)
                except WindowsError:
                    ##                    traceback.print_exc()                       # XXX DEBUG
                    name = win32.GetModuleFileNameEx(hProcess, self.get_image_base())
                if name:
                    name = PathOperations.native_to_win32_pathname(name)
                else:
                    name = None
            except (AttributeError, WindowsError):
                ##                traceback.print_exc()                           # XXX DEBUG
                if not name:
                    name = None

        # Method 5: PEB.ProcessParameters->ImagePathName
        #
        # May fail since it's using an undocumented internal structure.
        #
        # May be spoofed by malware, since this information resides
        # in usermode space (see http://www.ragestorm.net/blogs/?p=163).
        if not name:
            try:
                peb = self.get_peb()
                pp = self.read_structure(
                    peb.ProcessParameters, win32.RTL_USER_PROCESS_PARAMETERS
                )
                s = pp.ImagePathName
                name = self.peek_string(
                    s.Buffer, dwMaxSize=s.MaximumLength, fUnicode=True
                )
                if name:
                    name = PathOperations.native_to_win32_pathname(name)
                else:
                    name = None
            except (AttributeError, WindowsError):
                ##                traceback.print_exc()                           # XXX DEBUG
                name = None

        # Method 6: Module.get_filename()
        # It tries to get the filename from the file handle.
        #
        # There are currently some problems due to the strange way the API
        # works - it returns the pathname without the drive letter, and I
        # couldn't figure out a way to fix it.
        if not name and mainModule is not None:
            try:
                name = mainModule.get_filename()
                if not name:
                    name = None
            except (AttributeError, WindowsError):
                ##                traceback.print_exc()                           # XXX DEBUG
                name = None

        # Remember the filename.
        if name and mainModule is not None:
            mainModule.fileName = name

        if isinstance(name, bytes):
            name = name.decode("mbcs", "ignore")

        # Return the image filename, or None on error.
        return name

    def get_command_line_block(self):
        """
        Retrieves the command line block memory address and size.

        :rtype: tuple(int, int)
        :return: Tuple with the memory address of the command line block
            and it's maximum size in Unicode characters.

        :raises WindowsError: On error an exception is raised.
        """
        peb = self.get_peb()
        pp = self.read_structure(
            peb.ProcessParameters, win32.RTL_USER_PROCESS_PARAMETERS
        )
        s = pp.CommandLine
        return (s.Buffer, s.MaximumLength)

    def get_environment_block(self):
        """
        Retrieves the environment block memory address for the process.

        .. note::

            The size is always enough to contain the environment data, but
            it may not be an exact size. It's best to read the memory and
            scan for two null wide chars to find the actual size.

        :rtype: tuple(int, int)
        :return: Tuple with the memory address of the environment block
            and it's size.

        :raises WindowsError: On error an exception is raised.
        """
        peb = self.get_peb()
        pp = self.read_structure(
            peb.ProcessParameters, win32.RTL_USER_PROCESS_PARAMETERS
        )
        Environment = pp.Environment
        try:
            EnvironmentSize = pp.EnvironmentSize
        except AttributeError:
            mbi = self.mquery(Environment)
            EnvironmentSize = mbi.RegionSize + mbi.BaseAddress - Environment
        return (Environment, EnvironmentSize)

    def get_command_line(self):
        """
        Retrieves the command line with which the program was started.

        :rtype: str
        :return: Command line string.
        :raises WindowsError: On error an exception is raised.
        """
        (Buffer, MaximumLength) = self.get_command_line_block()
        CommandLine = self.peek_string(Buffer, dwMaxSize=MaximumLength, fUnicode=True)
        return CommandLine

    def get_environment_variables(self):
        """
        Retrieves the environment variables with which the program is running.

        :rtype: list[tuple[str, str]]
        :return: Environment keys and values as found in the process memory.
        :raises WindowsError: On error an exception is raised.
        """

        # TODO: This function should be refactored to be more robust and Pythonic.
        # The current implementation uses manual C-style buffer parsing, which is
        # fragile and hard to maintain. A better approach would be to decode the
        # entire environment block and use standard string operations.

        # Note: the first bytes are garbage and must be skipped. Then the first
        # two environment entries are the current drive and directory as key
        # and value pairs, followed by the ExitCode variable (it's what batch
        # files know as "errorlevel"). After that, the real environment vars
        # are there in alphabetical order. In theory that's where it stops,
        # but I've always seen one more "variable" tucked at the end which
        # may be another environment block but in ANSI. I haven't examined it
        # yet, I'm just skipping it because if it's parsed as Unicode it just
        # renders garbage.

        # Read the environment block contents.
        eb_address, eb_size = self.get_environment_block()
        data = self.peek(eb_address, eb_size)

        # Put them into a Unicode buffer.
        tmp = ctypes.create_string_buffer(data)
        buffer = ctypes.create_unicode_buffer(len(data))
        ctypes.memmove(buffer, tmp, len(data))
        del tmp

        # Skip until the first Unicode null char is found.
        pos = 0
        while buffer[pos] != "\0":
            pos += 1
        pos += 1

        # Loop for each environment variable...
        environment = []
        while buffer[pos] != "\0":
            # Until we find a null char...
            env_name_pos = pos
            env_name = ""
            found_name = False
            while buffer[pos] != "\0":
                # Get the current char.
                char = buffer[pos]

                # Is it an equal sign?
                if char == "=":
                    # Skip leading equal signs.
                    if env_name_pos == pos:
                        env_name_pos += 1
                        pos += 1
                        continue

                    # Otherwise we found the separator equal sign.
                    pos += 1
                    found_name = True
                    break

                # Add the char to the variable name.
                env_name += char

                # Next char.
                pos += 1

            # If the name was not parsed properly, stop.
            if not found_name:
                break

            # Read the variable value until we find a null char.
            env_value = ""
            while buffer[pos] != "\0":
                env_value += buffer[pos]
                pos += 1

            # Skip the null char.
            pos += 1

            # Add to the list of environment variables found.
            environment.append((env_name, env_value))

        # Remove the last entry, it's garbage.
        if environment:
            environment.pop()

        # Return the environment variables.
        return environment

    def get_environment(self, fUnicode=None):
        """
        Retrieves the environment with which the program is running.

        .. note::

            Duplicated keys are joined using null characters.
            To avoid this behavior, call :meth:`get_environment_variables`
            instead and convert the results to a dictionary directly, like this:
            ``dict(process.get_environment_variables())``

        .. seealso:: :class:`~winappdbg.win32.GuessStringType`

        :param bool fUnicode:
            ``True`` to return a list of Unicode strings, ``False``
            to return a list of ANSI strings, or ``None`` to return whatever
            the default is for string types.
        :rtype: dict[str, str]
        :return: Dictionary of environment keys and values.
        :raises WindowsError: On error an exception is raised.
        """

        # Get the environment variables.
        variables = self.get_environment_variables()

        # Convert the strings to ANSI if requested.
        if fUnicode is None:
            gst = win32.GuessStringType
            fUnicode = gst.t_default == gst.t_unicode
        if not fUnicode:
            variables = [
                (key.encode("cp1252"), value.encode("cp1252"))
                for (key, value) in variables
            ]

        # Add the variables to a dictionary, concatenating duplicates.
        environment = dict()
        for key, value in variables:
            if key in environment:
                if isinstance(key, bytes):
                    environment[key] = environment[key] + b"\0" + value
                else:
                    environment[key] = environment[key] + "\0" + value
            else:
                environment[key] = value

        # Return the dictionary.
        return environment

    # ------------------------------------------------------------------------------

    def search(self, pattern, minAddr=None, maxAddr=None):
        """
        Search for the given string or pattern within the process memory.

        .. note::

            This is a more limited version of :meth:`~.search.Search.search_process`.

        :param pattern: String or hexadecimal pattern to search for.
        :type pattern: str or :class:`~.search.Pattern`
        :param int minAddr: Optional. Start the search at this memory address.
        :param int maxAddr: Optional. Stop the search at this memory address.
        :rtype: iterator[tuple[int, str]]
        :return: An iterator of tuples. Each tuple contains the following:

            - The memory address where the pattern was found.
            - The data that matches the pattern.

        :raises WindowsError: An error occurred when querying or reading the
            process memory.
        """
        if isinstance(pattern, str):
            if HexInput.is_pattern(pattern):
                return Search.search_process(self, [pattern], minAddr, maxAddr)
            return self.search_bytes(pattern, minAddr, maxAddr)
        if isinstance(pattern, bytes):
            return self.search_bytes(pattern, minAddr, maxAddr)
        if isinstance(pattern, Pattern):
            return Search.search_process(self, [pattern], minAddr, maxAddr)
        raise TypeError("Unknown pattern type: %r" % type(pattern))

    def search_bytes(self, bytes, minAddr=None, maxAddr=None):
        """
        Search for the given byte pattern within the process memory.

        .. note::

            This is a more limited version of :meth:`~.search.Search.search_process`.

        :param bytes bytes: Bytes to search for.
        :param int minAddr: Optional. Start the search at this memory address.
        :param int maxAddr: Optional. Stop the search at this memory address.
        :rtype: iterator[int]
        :return: An iterator of memory addresses where the pattern was found.
        :raises WindowsError: An error occurred when querying or reading the
            process memory.
        """
        pattern = StringPattern(bytes)
        return (x[0] for x in Search.search_process(self, [pattern], minAddr, maxAddr))

    def search_text(
        self, text, encoding="utf-16le", caseSensitive=False, minAddr=None, maxAddr=None
    ):
        """
        Search for the given text within the process memory.

        .. note::

            This is a more limited version of :meth:`~.search.Search.search_process`.

        :param str text: Text to search for.
        :param str encoding: Optional. Encoding for the text parameter.
            Only used when the text to search for is a Unicode string.
            Don't change unless you know what you're doing!
        :param bool caseSensitive: ``True`` of the search is case sensitive,
            ``False`` otherwise.
        :param int minAddr: Optional. Start the search at this memory address.
        :param int maxAddr: Optional. Stop the search at this memory address.
        :rtype: iterator[tuple[int, str]]
        :return: An iterator of tuples. Each tuple contains the following:

            - The memory address where the pattern was found.
            - The text that matches the pattern.

        :raises WindowsError: An error occurred when querying or reading the
            process memory.
        """
        if isinstance(text, str):
            bytes = text.encode(encoding)
        else:
            bytes = text
        if caseSensitive:
            pattern = StringPattern(bytes)
        else:
            pattern = IStringPattern(bytes)
        return Search.search_process(self, [pattern], minAddr, maxAddr)

    def search_hexa(self, hexa, minAddr=None, maxAddr=None):
        """
        Search for the given hexadecimal pattern within the process memory.

        .. note::

            This is a more limited version of :meth:`~.search.Search.search_process`.

        Hex patterns must be in this form::

            "68 65 6c 6c 6f 20 77 6f 72 6c 64"  # "hello world"

        Spaces are optional. Capitalization of hex digits doesn't matter.
        This is exactly equivalent to the previous example::

            "68656C6C6F20776F726C64"            # "hello world"

        Wildcards are allowed, in the form of a ``?`` sign in any hex digit::

            "5? 5? c3"          # pop register / pop register / ret
            "b8 ?? ?? ?? ??"    # mov eax, immediate value

        :param str hexa: Pattern to search for.
        :param int minAddr: Optional. Start the search at this memory address.
        :param int maxAddr: Optional. Stop the search at this memory address.
        :rtype: iterator[tuple[int, str]]
        :return: An iterator of tuples. Each tuple contains the following:

            - The memory address where the pattern was found.
            - The bytes that match the pattern.

        :raises WindowsError: An error occurred when querying or reading the
            process memory.
        """
        pattern = HexPattern(hexa)
        return Search.search_process(self, [pattern], minAddr, maxAddr)

    # ------------------------------------------------------------------------------

    def __read_c_type(self, address, format, c_type):
        size = ctypes.sizeof(c_type)
        packed = self.read(address, size)
        if len(packed) != size:
            raise ctypes.WinError()
        return struct.unpack(format, packed)[0]

    def __write_c_type(self, address, format, unpacked):
        packed = struct.pack(format, unpacked)
        self.write(address, packed)

    # XXX TODO
    # + Maybe change page permissions before trying to read?
    def read(self, lpBaseAddress, nSize):
        """
        Reads from the memory of the process.

        .. seealso:: :meth:`peek`

        :param int lpBaseAddress: Memory address to begin reading.
        :param int nSize: Number of bytes to read.
        :rtype: bytes
        :return: Bytes read from the process memory.
        :raises WindowsError: On error an exception is raised.
        """
        hProcess = self.get_handle(
            win32.PROCESS_VM_READ | win32.PROCESS_QUERY_INFORMATION
        )
        if not self.is_buffer(lpBaseAddress, nSize):
            raise ctypes.WinError(win32.ERROR_INVALID_ADDRESS)
        data = win32.ReadProcessMemory(hProcess, lpBaseAddress, nSize)
        if len(data) != nSize:
            raise ctypes.WinError()
        return data

    def write(self, lpBaseAddress, lpBuffer):
        """
        Writes to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`poke`

        :param int lpBaseAddress: Memory address to begin writing.
        :param bytes lpBuffer: Bytes to write.
        :raises WindowsError: On error an exception is raised.
        """
        r = self.poke(lpBaseAddress, lpBuffer)
        if r != len(lpBuffer):
            raise ctypes.WinError()

    def read_char(self, lpBaseAddress):
        """
        Reads a single character from the memory of the process.

        .. seealso:: :meth:`peek_char`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: int
        :return: Character value read from the process memory.
        :raises WindowsError: On error an exception is raised.
        """
        return self.read(lpBaseAddress, 1)[0]

    def write_char(self, lpBaseAddress, char):
        """
        Writes a single character to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`poke_char`

        :param int lpBaseAddress: Memory address to begin writing.
        :param int char: Character to write.
        :raises WindowsError: On error an exception is raised.
        """
        self.write(lpBaseAddress, bytes((char,)))

    def read_int(self, lpBaseAddress):
        """
        Reads a signed integer from the memory of the process.

        .. seealso:: :meth:`peek_int`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: int
        :return: Integer value read from the process memory.
        :raises WindowsError: On error an exception is raised.
        """
        return self.__read_c_type(lpBaseAddress, "@l", ctypes.c_int)

    def write_int(self, lpBaseAddress, unpackedValue):
        """
        Writes a signed integer to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`poke_int`

        :param int lpBaseAddress: Memory address to begin writing.
        :param int unpackedValue: Value to write.
        :raises WindowsError: On error an exception is raised.
        """
        self.__write_c_type(lpBaseAddress, "@l", unpackedValue)

    def read_uint(self, lpBaseAddress):
        """
        Reads an unsigned integer from the memory of the process.

        .. seealso:: :meth:`peek_uint`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: int
        :return: Integer value read from the process memory.
        :raises WindowsError: On error an exception is raised.
        """
        return self.__read_c_type(lpBaseAddress, "@L", ctypes.c_uint)

    def write_uint(self, lpBaseAddress, unpackedValue):
        """
        Writes an unsigned integer to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`poke_uint`

        :param int lpBaseAddress: Memory address to begin writing.
        :param int unpackedValue: Value to write.
        :raises WindowsError: On error an exception is raised.
        """
        self.__write_c_type(lpBaseAddress, "@L", unpackedValue)

    def read_float(self, lpBaseAddress):
        """
        Reads a float from the memory of the process.

        .. seealso:: :meth:`peek_float`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: float
        :return: Floating point value read from the process memory.
        :raises WindowsError: On error an exception is raised.
        """
        return self.__read_c_type(lpBaseAddress, "@f", ctypes.c_float)

    def write_float(self, lpBaseAddress, unpackedValue):
        """
        Writes a float to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`poke_float`

        :param int lpBaseAddress: Memory address to begin writing.
        :param float unpackedValue: Floating point value to write.
        :raises WindowsError: On error an exception is raised.
        """
        self.__write_c_type(lpBaseAddress, "@f", unpackedValue)

    def read_double(self, lpBaseAddress):
        """
        Reads a double from the memory of the process.

        .. seealso:: :meth:`peek_double`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: float
        :return: Floating point value read from the process memory.
        :raises WindowsError: On error an exception is raised.
        """
        return self.__read_c_type(lpBaseAddress, "@d", ctypes.c_double)

    def write_double(self, lpBaseAddress, unpackedValue):
        """
        Writes a double to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`poke_double`

        :param int lpBaseAddress: Memory address to begin writing.
        :param float unpackedValue: Floating point value to write.
        :raises WindowsError: On error an exception is raised.
        """
        self.__write_c_type(lpBaseAddress, "@d", unpackedValue)

    def read_pointer(self, lpBaseAddress):
        """
        Reads a pointer value from the memory of the process.

        .. seealso:: :meth:`peek_pointer`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: int
        :return: Pointer value read from the process memory.
        :raises WindowsError: On error an exception is raised.
        """
        return self.__read_c_type(lpBaseAddress, "@P", ctypes.c_void_p)

    def write_pointer(self, lpBaseAddress, unpackedValue):
        """
        Writes a pointer value to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`poke_pointer`

        :param int lpBaseAddress: Memory address to begin writing.
        :param int unpackedValue: Value to write.
        :raises WindowsError: On error an exception is raised.
        """
        self.__write_c_type(lpBaseAddress, "@P", unpackedValue)

    def read_word(self, lpBaseAddress):
        """
        Reads a WORD from the memory of the process.

        .. seealso:: :meth:`peek_word`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: int
        :return: Integer value read from the process memory.
        :raises WindowsError: On error an exception is raised.
        """
        return self.__read_c_type(lpBaseAddress, "=H", win32.WORD)

    def write_word(self, lpBaseAddress, unpackedValue):
        """
        Writes a WORD to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`poke_word`

        :param int lpBaseAddress: Memory address to begin writing.
        :param int unpackedValue: Value to write.
        :raises WindowsError: On error an exception is raised.
        """
        self.__write_c_type(lpBaseAddress, "=H", unpackedValue)

    def read_dword(self, lpBaseAddress):
        """
        Reads a DWORD from the memory of the process.

        .. seealso:: :meth:`peek_dword`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: int
        :return: Integer value read from the process memory.
        :raises WindowsError: On error an exception is raised.
        """
        return self.__read_c_type(lpBaseAddress, "=L", win32.DWORD)

    def write_dword(self, lpBaseAddress, unpackedValue):
        """
        Writes a DWORD to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`poke_dword`

        :param int lpBaseAddress: Memory address to begin writing.
        :param int unpackedValue: Value to write.
        :raises WindowsError: On error an exception is raised.
        """
        self.__write_c_type(lpBaseAddress, "=L", unpackedValue)

    def read_qword(self, lpBaseAddress):
        """
        Reads a QWORD from the memory of the process.

        .. seealso:: :meth:`peek_qword`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: int
        :return: Integer value read from the process memory.
        :raises WindowsError: On error an exception is raised.
        """
        return self.__read_c_type(lpBaseAddress, "=Q", win32.QWORD)

    def write_qword(self, lpBaseAddress, unpackedValue):
        """
        Writes a QWORD to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`poke_qword`

        :param int lpBaseAddress: Memory address to begin writing.
        :param int unpackedValue: Value to write.
        :raises WindowsError: On error an exception is raised.
        """
        self.__write_c_type(lpBaseAddress, "=Q", unpackedValue)

    def read_structure(self, lpBaseAddress, stype):
        """
        Reads a ctypes structure from the memory of the process.

        .. seealso:: :meth:`read`

        :param int lpBaseAddress: Memory address to begin reading.
        :param stype: Structure definition.
        :type stype: ctypes.Structure
        :rtype: ctypes.Structure
        :return: Structure instance filled in with data
            read from the process memory.
        :raises WindowsError: On error an exception is raised.
        """
        if not isinstance(lpBaseAddress, int):
            lpBaseAddress = ctypes.cast(lpBaseAddress, ctypes.c_void_p).value
        data = self.read(lpBaseAddress, ctypes.sizeof(stype))
        buff = ctypes.create_string_buffer(data)
        ptr = ctypes.cast(ctypes.pointer(buff), ctypes.POINTER(stype))
        return ptr.contents

    def read_string(self, lpBaseAddress, nChars, fUnicode=False):
        """
        Reads an ASCII or Unicode string
        from the address space of the process.

        .. seealso:: :meth:`peek_string`

        :param int lpBaseAddress: Memory address to begin reading.
        :param int nChars: String length to read, in characters.
            Remember that Unicode strings have two byte characters.
        :param bool fUnicode: ``True`` is the string is expected to be Unicode,
            ``False`` if it's expected to be ANSI.
        :rtype: str
        :return: String read from the process memory space.
        :raises WindowsError: On error an exception is raised.
        """
        szString = b""
        if nChars is None:
            memory = []
            if not self.is_buffer(lpBaseAddress, 1):
                raise ctypes.WinError(win32.ERROR_INVALID_ADDRESS)

            for mbi in self.get_memory_map(lpBaseAddress):
                if mbi.State == win32.MEM_COMMIT and not mbi.Protect & win32.PAGE_GUARD:
                    memory.append((mbi.BaseAddress, mbi.RegionSize))

            char_size = 2 if fUnicode else 1
            null_char = b"\0\0" if fUnicode else b"\0"

            for lpBasePage, sizePage in memory:
                page = self.read(lpBasePage, sizePage)
                offset_in_page = lpBaseAddress - lpBasePage
                while offset_in_page < sizePage:
                    char = page[offset_in_page : offset_in_page + char_size]
                    if char != null_char:
                        szString += char
                    else:
                        if fUnicode:
                            return szString.decode("utf-16le", "ignore")
                        return szString.decode("latin-1", "ignore")
                    offset_in_page += char_size
                    lpBaseAddress += char_size
        else:
            if fUnicode:
                nBytes = nChars * 2
            else:
                nBytes = nChars
            szString = self.read(lpBaseAddress, nBytes)

        if fUnicode:
            szString = szString.decode("utf-16le", "ignore")
        else:
            szString = szString.decode("latin-1", "ignore")
        return szString

    # ------------------------------------------------------------------------------

    # FIXME this won't work properly with a different endianness!
    def __peek_c_type(self, address, format, c_type):
        size = ctypes.sizeof(c_type)
        packed = self.peek(address, size)

        if len(packed) < size:
            packed = b"\0" * (size - len(packed)) + packed
        elif len(packed) > size:
            packed = packed[:size]
        return struct.unpack(format, packed)[0]

    def __poke_c_type(self, address, format, unpacked):
        packed = struct.pack("@L", unpacked)
        return self.poke(address, packed)

    def peek(self, lpBaseAddress, nSize):
        """
        Reads the memory of the process.

        .. seealso:: :meth:`read`

        :param int lpBaseAddress: Memory address to begin reading.
        :param int nSize: Number of bytes to read.
        :rtype: bytes
        :return: Bytes read from the process memory.
            Returns an empty string on error.
        """
        # XXX TODO
        # + Maybe change page permissions before trying to read?
        # + Maybe use mquery instead of get_memory_map?
        #   (less syscalls if we break out of the loop earlier)
        data = b""
        if nSize > 0:
            try:
                hProcess = self.get_handle(
                    win32.PROCESS_VM_READ | win32.PROCESS_QUERY_INFORMATION
                )
                for mbi in self.get_memory_map(lpBaseAddress, lpBaseAddress + nSize):
                    if not mbi.is_readable():
                        nSize = mbi.BaseAddress - lpBaseAddress
                        break
                if nSize > 0:
                    data = win32.ReadProcessMemory(hProcess, lpBaseAddress, nSize)
            except WindowsError as e:
                msg = "Error reading process %d address %s: %s"
                msg %= (self.get_pid(), HexDump.address(lpBaseAddress), e.strerror)
                warnings.warn(msg)
        return data

    def poke(self, lpBaseAddress, lpBuffer):
        """
        Writes to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`write`

        :param int lpBaseAddress: Memory address to begin writing.
        :param bytes lpBuffer: Bytes to write.
        :rtype: int
        :return: Number of bytes written.
            May be less than the number of bytes to write.
        """
        hProcess = self.get_handle(
            win32.PROCESS_VM_WRITE
            | win32.PROCESS_VM_OPERATION
            | win32.PROCESS_QUERY_INFORMATION
        )
        mbi = self.mquery(lpBaseAddress)
        if not mbi.has_content():
            raise ctypes.WinError(win32.ERROR_INVALID_ADDRESS)
        if mbi.is_image() or mbi.is_mapped():
            prot = win32.PAGE_WRITECOPY
        elif mbi.is_writeable():
            prot = None
        elif mbi.is_executable():
            prot = win32.PAGE_EXECUTE_READWRITE
        else:
            prot = win32.PAGE_READWRITE
        if prot is not None:
            try:
                self.mprotect(lpBaseAddress, len(lpBuffer), prot)
            except Exception:
                prot = None
                msg = (
                    "Failed to adjust page permissions for process %s at address %s: %s"
                )
                msg = msg % (
                    self.get_pid(),
                    HexDump.address(lpBaseAddress, self.get_bits()),
                    traceback.format_exc(),
                )
                warnings.warn(msg, RuntimeWarning)
        try:
            r = win32.WriteProcessMemory(hProcess, lpBaseAddress, lpBuffer)
        finally:
            if prot is not None:
                self.mprotect(lpBaseAddress, len(lpBuffer), mbi.Protect)
        return r

    def peek_char(self, lpBaseAddress):
        """
        Reads a single character from the memory of the process.

        .. seealso:: :meth:`read_char`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: int
        :return: Character read from the process memory.
            Returns zero on error.
        """
        char = self.peek(lpBaseAddress, 1)
        if char:
            return char[0]
        return 0

    def poke_char(self, lpBaseAddress, char):
        """
        Writes a single character to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`write_char`

        :param int lpBaseAddress: Memory address to begin writing.
        :param int char: Character to write.
        :rtype: int
        :return: Number of bytes written.
            May be less than the number of bytes to write.
        """
        return self.poke(lpBaseAddress, bytes((char,)))

    def peek_int(self, lpBaseAddress):
        """
        Reads a signed integer from the memory of the process.

        .. seealso:: :meth:`read_int`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: int
        :return: Integer value read from the process memory.
            Returns zero on error.
        """
        return self.__peek_c_type(lpBaseAddress, "@l", ctypes.c_int)

    def poke_int(self, lpBaseAddress, unpackedValue):
        """
        Writes a signed integer to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`write_int`

        :param int lpBaseAddress: Memory address to begin writing.
        :param int unpackedValue: Value to write.
        :rtype: int
        :return: Number of bytes written.
            May be less than the number of bytes to write.
        """
        return self.__poke_c_type(lpBaseAddress, "@l", unpackedValue)

    def peek_uint(self, lpBaseAddress):
        """
        Reads an unsigned integer from the memory of the process.

        .. seealso:: :meth:`read_uint`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: int
        :return: Integer value read from the process memory.
            Returns zero on error.
        """
        return self.__peek_c_type(lpBaseAddress, "@L", ctypes.c_uint)

    def poke_uint(self, lpBaseAddress, unpackedValue):
        """
        Writes an unsigned integer to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`write_uint`

        :param int lpBaseAddress: Memory address to begin writing.
        :param int unpackedValue: Value to write.
        :rtype: int
        :return: Number of bytes written.
            May be less than the number of bytes to write.
        """
        return self.__poke_c_type(lpBaseAddress, "@L", unpackedValue)

    def peek_float(self, lpBaseAddress):
        """
        Reads a float from the memory of the process.

        .. seealso:: :meth:`read_float`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: float
        :return: Integer value read from the process memory.
            Returns zero on error.
        """
        return self.__peek_c_type(lpBaseAddress, "@f", ctypes.c_float)

    def poke_float(self, lpBaseAddress, unpackedValue):
        """
        Writes a float to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`write_float`

        :param int lpBaseAddress: Memory address to begin writing.
        :param float unpackedValue: Value to write.
        :rtype: int
        :return: Number of bytes written.
            May be less than the number of bytes to write.
        """
        return self.__poke_c_type(lpBaseAddress, "@f", unpackedValue)

    def peek_double(self, lpBaseAddress):
        """
        Reads a double from the memory of the process.

        .. seealso:: :meth:`read_double`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: float
        :return: Integer value read from the process memory.
            Returns zero on error.
        """
        return self.__peek_c_type(lpBaseAddress, "@d", ctypes.c_double)

    def poke_double(self, lpBaseAddress, unpackedValue):
        """
        Writes a double to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`write_double`

        :param int lpBaseAddress: Memory address to begin writing.
        :param float unpackedValue: Value to write.
        :rtype: int
        :return: Number of bytes written.
            May be less than the number of bytes to write.
        """
        return self.__poke_c_type(lpBaseAddress, "@d", unpackedValue)

    def peek_dword(self, lpBaseAddress):
        """
        Reads a DWORD from the memory of the process.

        .. seealso:: :meth:`read_dword`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: int
        :return: Integer value read from the process memory.
            Returns zero on error.
        """
        return self.__peek_c_type(lpBaseAddress, "=L", win32.DWORD)

    def poke_dword(self, lpBaseAddress, unpackedValue):
        """
        Writes a DWORD to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`write_dword`

        :param int lpBaseAddress: Memory address to begin writing.
        :param int unpackedValue: Value to write.
        :rtype: int
        :return: Number of bytes written.
            May be less than the number of bytes to write.
        """
        return self.__poke_c_type(lpBaseAddress, "=L", unpackedValue)

    def peek_qword(self, lpBaseAddress):
        """
        Reads a QWORD from the memory of the process.

        .. seealso:: :meth:`read_qword`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: int
        :return: Integer value read from the process memory.
            Returns zero on error.
        """
        return self.__peek_c_type(lpBaseAddress, "=Q", win32.QWORD)

    def poke_qword(self, lpBaseAddress, unpackedValue):
        """
        Writes a QWORD to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`write_qword`

        :param int lpBaseAddress: Memory address to begin writing.
        :param int unpackedValue: Value to write.
        :rtype: int
        :return: Number of bytes written.
            May be less than the number of bytes to write.
        """
        return self.__poke_c_type(lpBaseAddress, "=Q", unpackedValue)

    def peek_pointer(self, lpBaseAddress):
        """
        Reads a pointer value from the memory of the process.

        .. seealso:: :meth:`read_pointer`

        :param int lpBaseAddress: Memory address to begin reading.
        :rtype: int
        :return: Pointer value read from the process memory.
            Returns zero on error.
        """
        return self.__peek_c_type(lpBaseAddress, "@P", ctypes.c_void_p)

    def poke_pointer(self, lpBaseAddress, unpackedValue):
        """
        Writes a pointer value to the memory of the process.

        .. note::

            Page permissions may be changed temporarily while writing.

        .. seealso:: :meth:`write_pointer`

        :param int lpBaseAddress: Memory address to begin writing.
        :param int unpackedValue: Value to write.
        :rtype: int
        :return: Number of bytes written.
            May be less than the number of bytes to write.
        """
        return self.__poke_c_type(lpBaseAddress, "@P", unpackedValue)

    def peek_string(self, lpBaseAddress, fUnicode=False, dwMaxSize=0x1000):
        """
        Tries to read an ASCII or Unicode string
        from the address space of the process.

        .. seealso:: :meth:`read_string`

        :param int lpBaseAddress: Memory address to begin reading.
        :param bool fUnicode: ``True`` is the string is expected to be Unicode,
            ``False`` if it's expected to be ANSI.
        :param int dwMaxSize: Maximum allowed string length to read, in bytes.
        :rtype: str or bytes
        :return: String read from the process memory space.
            It **doesn't** include the terminating null character.
            Returns an empty string on failure.
        """

        # Validate the parameters.
        if not lpBaseAddress or dwMaxSize == 0:
            if fUnicode:
                return ""
            return b""
        if not dwMaxSize:
            dwMaxSize = 0x1000

        # Read the string.
        szString = self.peek(lpBaseAddress, dwMaxSize)

        # If the string is Unicode...
        if fUnicode:
            # Decode the string.
            if isinstance(szString, bytes):
                szString = szString.decode("utf-16le", "replace")

            # Truncate the string when the first null char is found.
            null_pos = szString.find("\0")
            if null_pos != -1:
                szString = szString[:null_pos]

        # If the string is ANSI...
        else:
            # Truncate the string when the first null char is found.
            null_pos = szString.find(b"\0")
            if null_pos != -1:
                szString = szString[:null_pos]

        # Return the decoded string.
        return szString

    # TODO
    # try to avoid reading the same page twice by caching it
    def peek_pointers_in_data(self, data, peekSize=16, peekStep=1):
        """
        Tries to guess which values in the given data are valid pointers,
        and reads some data from them.

        .. seealso:: :meth:`peek`

        :param bytes data: Binary data to find pointers in.
        :param int peekSize: Number of bytes to read from each pointer found.
        :param int peekStep: Expected data alignment.
            Typically you specify 1 when data alignment is unknown,
            or 4 when you expect data to be DWORD aligned.
            Any other value may be specified.
        :rtype: dict[int, bytes]
        :return: Dictionary mapping stack offsets to the data they point to.
        """
        result = dict()
        ptrSize = win32.sizeof(win32.LPVOID)
        if ptrSize == 4:
            ptrFmt = "<L"
        else:
            ptrFmt = "<Q"
        if len(data) > 0:
            for i in range(0, len(data), peekStep):
                packed = data[i : i + ptrSize]
                if len(packed) == ptrSize:
                    address = struct.unpack(ptrFmt, packed)[0]
                    ##                    if not address & (~0xFFFF): continue
                    peek_data = self.peek(address, peekSize)
                    if peek_data:
                        result[i] = peek_data
        return result

    # ------------------------------------------------------------------------------

    def malloc(self, dwSize, lpAddress=None):
        """
        Allocates memory into the address space of the process.

        .. seealso:: :meth:`free`

        :param int dwSize: Number of bytes to allocate.
        :param int lpAddress:
            Optional. Desired address for the newly allocated memory.
            This is only a hint, the memory could still be allocated somewhere
            else.
        :rtype: int
        :return: Address of the newly allocated memory.
        :raises WindowsError: On error an exception is raised.
        """
        hProcess = self.get_handle(win32.PROCESS_VM_OPERATION)
        return win32.VirtualAllocEx(hProcess, lpAddress, dwSize)

    def mprotect(self, lpAddress, dwSize, flNewProtect):
        """
        Set memory protection in the address space of the process.

        .. seealso:: http://msdn.microsoft.com/en-us/library/aa366899.aspx

        :param int lpAddress: Address of memory to protect.
        :param int dwSize: Number of bytes to protect.
        :param int flNewProtect: New protect flags.
        :rtype: int
        :return: Old protect flags.
        :raises WindowsError: On error an exception is raised.
        """
        hProcess = self.get_handle(win32.PROCESS_VM_OPERATION)
        return win32.VirtualProtectEx(hProcess, lpAddress, dwSize, flNewProtect)

    def mquery(self, lpAddress):
        """
        Query memory information from the address space of the process.
        Returns a :class:`~win32.MemoryBasicInformation` object.

        .. seealso:: http://msdn.microsoft.com/en-us/library/aa366907(VS.85).aspx

        :param int lpAddress: Address of memory to query.
        :rtype: :class:`~win32.MemoryBasicInformation`
        :return: Memory region information.
        :raises WindowsError: On error an exception is raised.
        """
        hProcess = self.get_handle(win32.PROCESS_QUERY_INFORMATION)
        return win32.VirtualQueryEx(hProcess, lpAddress)

    def free(self, lpAddress):
        """
        Frees memory from the address space of the process.

        .. seealso:: http://msdn.microsoft.com/en-us/library/aa366894(v=vs.85).aspx

        :param int lpAddress: Address of memory to free.
            Must be the base address returned by :meth:`malloc`.
        :raises WindowsError: On error an exception is raised.
        """
        hProcess = self.get_handle(win32.PROCESS_VM_OPERATION)
        win32.VirtualFreeEx(hProcess, lpAddress)

    # ------------------------------------------------------------------------------

    def is_pointer(self, address):
        """
        Determines if an address is a valid code or data pointer.

        That is, the address must be valid and must point to code or data in
        the target process.

        :param int address: Memory address to query.
        :rtype: bool
        :return: ``True`` if the address is a valid code or data pointer.
        :raises WindowsError: An exception is raised on error.
        """
        try:
            mbi = self.mquery(address)
        except WindowsError as e:
            if e.winerror == win32.ERROR_INVALID_PARAMETER:
                return False
            raise
        return mbi.has_content()

    def is_address_valid(self, address):
        """
        Determines if an address is a valid user mode address.

        :param int address: Memory address to query.
        :rtype: bool
        :return: ``True`` if the address is a valid user mode address.
        :raises WindowsError: An exception is raised on error.
        """
        try:
            self.mquery(address)
        except WindowsError as e:
            if e.winerror == win32.ERROR_INVALID_PARAMETER:
                return False
            raise
        return True

    def is_address_free(self, address):
        """
        Determines if an address belongs to a free page.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        :param int address: Memory address to query.
        :rtype: bool
        :return: ``True`` if the address belongs to a free page.
        :raises WindowsError: An exception is raised on error.
        """
        try:
            mbi = self.mquery(address)
        except WindowsError as e:
            if e.winerror == win32.ERROR_INVALID_PARAMETER:
                return False
            raise
        return mbi.is_free()

    def is_address_reserved(self, address):
        """
        Determines if an address belongs to a reserved page.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        :param int address: Memory address to query.
        :rtype: bool
        :return: ``True`` if the address belongs to a reserved page.
        :raises WindowsError: An exception is raised on error.
        """
        try:
            mbi = self.mquery(address)
        except WindowsError as e:
            if e.winerror == win32.ERROR_INVALID_PARAMETER:
                return False
            raise
        return mbi.is_reserved()

    def is_address_commited(self, address):
        """
        Determines if an address belongs to a commited page.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        :param int address: Memory address to query.
        :rtype: bool
        :return: ``True`` if the address belongs to a commited page.
        :raises WindowsError: An exception is raised on error.
        """
        try:
            mbi = self.mquery(address)
        except WindowsError as e:
            if e.winerror == win32.ERROR_INVALID_PARAMETER:
                return False
            raise
        return mbi.is_commited()

    def is_address_guard(self, address):
        """
        Determines if an address belongs to a guard page.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        :param int address: Memory address to query.
        :rtype: bool
        :return: ``True`` if the address belongs to a guard page.
        :raises WindowsError: An exception is raised on error.
        """
        try:
            mbi = self.mquery(address)
        except WindowsError as e:
            if e.winerror == win32.ERROR_INVALID_PARAMETER:
                return False
            raise
        return mbi.is_guard()

    def is_address_readable(self, address):
        """
        Determines if an address belongs to a commited and readable page.
        The page may or may not have additional permissions.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        :param int address: Memory address to query.
        :rtype: bool
        :return:
            ``True`` if the address belongs to a commited and readable page.
        :raises WindowsError: An exception is raised on error.
        """
        try:
            mbi = self.mquery(address)
        except WindowsError as e:
            if e.winerror == win32.ERROR_INVALID_PARAMETER:
                return False
            raise
        return mbi.is_readable()

    def is_address_writeable(self, address):
        """
        Determines if an address belongs to a commited and writeable page.
        The page may or may not have additional permissions.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        :param int address: Memory address to query.
        :rtype: bool
        :return:
            ``True`` if the address belongs to a commited and writeable page.
        :raises WindowsError: An exception is raised on error.
        """
        try:
            mbi = self.mquery(address)
        except WindowsError as e:
            if e.winerror == win32.ERROR_INVALID_PARAMETER:
                return False
            raise
        return mbi.is_writeable()

    def is_address_copy_on_write(self, address):
        """
        Determines if an address belongs to a commited, copy-on-write page.
        The page may or may not have additional permissions.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        :param int address: Memory address to query.
        :rtype: bool
        :return:
            ``True`` if the address belongs to a commited, copy-on-write page.
        :raises WindowsError: An exception is raised on error.
        """
        try:
            mbi = self.mquery(address)
        except WindowsError as e:
            if e.winerror == win32.ERROR_INVALID_PARAMETER:
                return False
            raise
        return mbi.is_copy_on_write()

    def is_address_executable(self, address):
        """
        Determines if an address belongs to a commited and executable page.
        The page may or may not have additional permissions.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        :param int address: Memory address to query.
        :rtype: bool
        :return:
            ``True`` if the address belongs to a commited and executable page.
        :raises WindowsError: An exception is raised on error.
        """
        try:
            mbi = self.mquery(address)
        except WindowsError as e:
            if e.winerror == win32.ERROR_INVALID_PARAMETER:
                return False
            raise
        return mbi.is_executable()

    def is_address_executable_and_writeable(self, address):
        """
        Determines if an address belongs to a commited, writeable and
        executable page. The page may or may not have additional permissions.

        Looking for writeable and executable pages is important when
        exploiting a software vulnerability.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        :param int address: Memory address to query.
        :rtype: bool
        :return:
            ``True`` if the address belongs to a commited, writeable and
            executable page.
        :raises WindowsError: An exception is raised on error.
        """
        try:
            mbi = self.mquery(address)
        except WindowsError as e:
            if e.winerror == win32.ERROR_INVALID_PARAMETER:
                return False
            raise
        return mbi.is_executable_and_writeable()

    def is_buffer(self, address, size):
        """
        Determines if the given memory area is a valid code or data buffer.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        .. seealso:: :meth:`mquery`

        :param int address: Memory address.
        :param int size: Number of bytes. Must be greater than zero.
        :rtype: bool
        :return: ``True`` if the memory area is a valid code or data buffer,
            ``False`` otherwise.
        :raises ValueError: The size argument must be greater than zero.
        :raises WindowsError: On error an exception is raised.
        """
        if size <= 0:
            raise ValueError("The size argument must be greater than zero")
        while size > 0:
            try:
                mbi = self.mquery(address)
            except WindowsError as e:
                if e.winerror == win32.ERROR_INVALID_PARAMETER:
                    return False
                raise
            if not mbi.has_content():
                return False
            size = size - mbi.RegionSize
        return True

    def is_buffer_readable(self, address, size):
        """
        Determines if the given memory area is readable.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        .. seealso:: :meth:`mquery`

        :param int address: Memory address.
        :param int size: Number of bytes. Must be greater than zero.
        :rtype: bool
        :return: ``True`` if the memory area is readable, ``False`` otherwise.
        :raises ValueError: The size argument must be greater than zero.
        :raises WindowsError: On error an exception is raised.
        """
        if size <= 0:
            raise ValueError("The size argument must be greater than zero")
        while size > 0:
            try:
                mbi = self.mquery(address)
            except WindowsError as e:
                if e.winerror == win32.ERROR_INVALID_PARAMETER:
                    return False
                raise
            if not mbi.is_readable():
                return False
            size = size - mbi.RegionSize
        return True

    def is_buffer_writeable(self, address, size):
        """
        Determines if the given memory area is writeable.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        .. seealso:: :meth:`mquery`

        :param int address: Memory address.
        :param int size: Number of bytes. Must be greater than zero.
        :rtype: bool
        :return: ``True`` if the memory area is writeable, ``False`` otherwise.
        :raises ValueError: The size argument must be greater than zero.
        :raises WindowsError: On error an exception is raised.
        """
        if size <= 0:
            raise ValueError("The size argument must be greater than zero")
        while size > 0:
            try:
                mbi = self.mquery(address)
            except WindowsError as e:
                if e.winerror == win32.ERROR_INVALID_PARAMETER:
                    return False
                raise
            if not mbi.is_writeable():
                return False
            size = size - mbi.RegionSize
        return True

    def is_buffer_copy_on_write(self, address, size):
        """
        Determines if the given memory area is marked as copy-on-write.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        .. seealso:: :meth:`mquery`

        :param int address: Memory address.
        :param int size: Number of bytes. Must be greater than zero.
        :rtype: bool
        :return: ``True`` if the memory area is marked as copy-on-write,
            ``False`` otherwise.
        :raises ValueError: The size argument must be greater than zero.
        :raises WindowsError: On error an exception is raised.
        """
        if size <= 0:
            raise ValueError("The size argument must be greater than zero")
        while size > 0:
            try:
                mbi = self.mquery(address)
            except WindowsError as e:
                if e.winerror == win32.ERROR_INVALID_PARAMETER:
                    return False
                raise
            if not mbi.is_copy_on_write():
                return False
            size = size - mbi.RegionSize
        return True

    def is_buffer_executable(self, address, size):
        """
        Determines if the given memory area is executable.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        .. seealso:: :meth:`mquery`

        :param int address: Memory address.
        :param int size: Number of bytes. Must be greater than zero.
        :rtype: bool
        :return: ``True`` if the memory area is executable, ``False`` otherwise.
        :raises ValueError: The size argument must be greater than zero.
        :raises WindowsError: On error an exception is raised.
        """
        if size <= 0:
            raise ValueError("The size argument must be greater than zero")
        while size > 0:
            try:
                mbi = self.mquery(address)
            except WindowsError as e:
                if e.winerror == win32.ERROR_INVALID_PARAMETER:
                    return False
                raise
            if not mbi.is_executable():
                return False
            size = size - mbi.RegionSize
        return True

    def is_buffer_executable_and_writeable(self, address, size):
        """
        Determines if the given memory area is writeable and executable.

        Looking for writeable and executable pages is important when
        exploiting a software vulnerability.

        .. note::

            Returns always ``False`` for kernel mode addresses.

        .. seealso:: :meth:`mquery`

        :param int address: Memory address.
        :param int size: Number of bytes. Must be greater than zero.
        :rtype: bool
        :return: ``True`` if the memory area is writeable and executable,
            ``False`` otherwise.
        :raises ValueError: The size argument must be greater than zero.
        :raises WindowsError: On error an exception is raised.
        """
        if size <= 0:
            raise ValueError("The size argument must be greater than zero")
        while size > 0:
            try:
                mbi = self.mquery(address)
            except WindowsError as e:
                if e.winerror == win32.ERROR_INVALID_PARAMETER:
                    return False
                raise
            if not mbi.is_executable():
                return False
            size = size - mbi.RegionSize
        return True

    def get_memory_map(self, minAddr=None, maxAddr=None):
        """
        Produces a memory map to the process address space.

        Optionally restrict the map to the given address range.

        .. seealso:: :meth:`mquery`

        :param int minAddr: Optional. Starting address in address range to query.
        :param int maxAddr: Optional. Ending address in address range to query.
        :rtype: list[win32.MemoryBasicInformation]
        :return: List of memory region information objects.
        """
        return list(self.iter_memory_map(minAddr, maxAddr))

    def generate_memory_map(self, minAddr=None, maxAddr=None):
        """
        Returns a :class:`Regenerator` that can iterate indefinitely over the memory
        map to the process address space.

        Optionally restrict the map to the given address range.

        .. seealso:: :meth:`mquery`

        :param int minAddr: Optional. Starting address in address range to query.
        :param int maxAddr: Optional. Ending address in address range to query.
        :rtype: Regenerator[win32.MemoryBasicInformation]
        :return: List of memory region information objects.
        """
        return Regenerator(self.iter_memory_map, minAddr, maxAddr)

    def iter_memory_map(self, minAddr=None, maxAddr=None):
        """
        Produces an iterator over the memory map to the process address space.

        Optionally restrict the map to the given address range.

        .. seealso:: :meth:`mquery`

        :param int minAddr: Optional. Starting address in address range to query.
        :param int maxAddr: Optional. Ending address in address range to query.
        :rtype: iterator[win32.MemoryBasicInformation]
        :return: List of memory region information objects.
        """
        minAddr, maxAddr = MemoryAddresses.align_address_range(minAddr, maxAddr)
        prevAddr = minAddr - 1
        currentAddr = minAddr
        while prevAddr < currentAddr < maxAddr:
            try:
                mbi = self.mquery(currentAddr)
            except WindowsError as e:
                if e.winerror == win32.ERROR_INVALID_PARAMETER:
                    break
                raise
            yield mbi
            prevAddr = currentAddr
            currentAddr = mbi.BaseAddress + mbi.RegionSize

    def get_mapped_filenames(self, memoryMap=None):
        """
        Retrieves the filenames for memory mapped files in the debugee.

        :param list[win32.MemoryBasicInformation] memoryMap:
            Optional. Memory map returned by :meth:`get_memory_map`.
            If not given, the current memory map is used.
        :rtype: dict[int, str]
        :return: Dictionary mapping memory addresses to file names.
            Native filenames are converted to Win32 filenames when possible.
        """
        hProcess = self.get_handle(
            win32.PROCESS_VM_READ | win32.PROCESS_QUERY_INFORMATION
        )
        if not memoryMap:
            memoryMap = self.get_memory_map()
        mappedFilenames = dict()
        for mbi in memoryMap:
            if mbi.Type not in (win32.MEM_IMAGE, win32.MEM_MAPPED):
                continue
            baseAddress = mbi.BaseAddress
            fileName = ""
            try:
                fileName = win32.GetMappedFileName(hProcess, baseAddress)
                fileName = PathOperations.native_to_win32_pathname(fileName)
            except WindowsError as e:  # NOQA
                # try:
                #    msg = "Can't get mapped file name at address %s in process " \
                #          "%d, reason: %s" % (HexDump.address(baseAddress),
                #                              self.get_pid(),
                #                              e.strerror)
                #    warnings.warn(msg, Warning)
                # except Exception:
                pass
            mappedFilenames[baseAddress] = fileName
        return mappedFilenames

    def generate_memory_snapshot(self, minAddr=None, maxAddr=None):
        """
        Returns a :class:`Regenerator` that allows you to iterate through the memory
        contents of a process indefinitely.

        It's basically the same as the :meth:`take_memory_snapshot` method, but it
        takes the snapshot of each memory region as it goes, as opposed to
        taking the whole snapshot at once. This allows you to work with very
        large snapshots without a significant performance penalty.

        Example::

            # print(the memory contents of a process.)
            process.suspend()
            try:
                snapshot = process.generate_memory_snapshot()
                for mbi in snapshot:
                    print(HexDump.hexblock(mbi.content, mbi.BaseAddress))
            finally:
                process.resume()

        The downside of this is the process must remain suspended while
        iterating the snapshot, otherwise strange things may happen.

        The snapshot can be iterated more than once. Each time it's iterated
        the memory contents of the process will be fetched again.

        You can also iterate the memory of a dead process, just as long as the
        last open handle to it hasn't been closed.

        .. seealso:: :meth:`take_memory_snapshot`

        :param int minAddr: Optional. Starting address in address range to query.
        :param int maxAddr: Optional. Ending address in address range to query.
        :rtype: Regenerator[win32.MemoryBasicInformation]
        :return: Generator that when iterated returns memory region information
            objects. Two extra properties are added to these objects:

            - ``filename``: Mapped filename, or ``None``.
            - ``content``: Memory contents, or ``None``.
        """
        return Regenerator(self.iter_memory_snapshot, minAddr, maxAddr)

    def iter_memory_snapshot(self, minAddr=None, maxAddr=None):
        """
        Returns an iterator that allows you to go through the memory contents
        of a process.

        It's basically the same as the :meth:`take_memory_snapshot` method, but it
        takes the snapshot of each memory region as it goes, as opposed to
        taking the whole snapshot at once. This allows you to work with very
        large snapshots without a significant performance penalty.

        Example::

            # print(the memory contents of a process.)
            process.suspend()
            try:
                snapshot = process.generate_memory_snapshot()
                for mbi in snapshot:
                    print(HexDump.hexblock(mbi.content, mbi.BaseAddress))
            finally:
                process.resume()

        The downside of this is the process must remain suspended while
        iterating the snapshot, otherwise strange things may happen.

        The snapshot can only iterated once. To be able to iterate indefinitely
        call the :meth:`generate_memory_snapshot` method instead.

        You can also iterate the memory of a dead process, just as long as the
        last open handle to it hasn't been closed.

        .. seealso:: :meth:`take_memory_snapshot`

        :param int minAddr: Optional. Starting address in address range to query.
        :param int maxAddr: Optional. Ending address in address range to query.
        :rtype: iterator[win32.MemoryBasicInformation]
        :return: Iterator of memory region information objects.
            Two extra properties are added to these objects:

            - ``filename``: Mapped filename, or ``None``.
            - ``content``: Memory contents, or ``None``.
        """

        # One may feel tempted to include calls to self.suspend() and
        # self.resume() here, but that wouldn't work on a dead process.
        # It also wouldn't be needed when debugging since the process is
        # already suspended when the debug event arrives. So it's up to
        # the user to suspend the process if needed.

        # Get the memory map.
        memory = self.get_memory_map(minAddr, maxAddr)

        # Abort if the map couldn't be retrieved.
        if not memory:
            return

        # Get the mapped filenames.
        # Don't fail on access denied errors.
        try:
            filenames = self.get_mapped_filenames(memory)
        except WindowsError as e:
            if e.winerror != win32.ERROR_ACCESS_DENIED:
                raise
            filenames = dict()

        # Trim the first memory information block if needed.
        if minAddr is not None:
            minAddr = MemoryAddresses.align_address_to_page_start(minAddr)
            mbi = memory[0]
            if mbi.BaseAddress < minAddr:
                mbi.RegionSize = mbi.BaseAddress + mbi.RegionSize - minAddr
                mbi.BaseAddress = minAddr

        # Trim the last memory information block if needed.
        if maxAddr is not None:
            if maxAddr != MemoryAddresses.align_address_to_page_start(maxAddr):
                maxAddr = MemoryAddresses.align_address_to_page_end(maxAddr)
            mbi = memory[-1]
            if mbi.BaseAddress + mbi.RegionSize > maxAddr:
                mbi.RegionSize = maxAddr - mbi.BaseAddress

        # Read the contents of each block and yield it.
        while memory:
            mbi = memory.pop(0)  # so the garbage collector can take it
            mbi.filename = filenames.get(mbi.BaseAddress, None)
            if mbi.has_content():
                mbi.content = self.read(mbi.BaseAddress, mbi.RegionSize)
            else:
                mbi.content = None
            yield mbi

    def take_memory_snapshot(self, minAddr=None, maxAddr=None):
        """
        Takes a snapshot of the memory contents of the process.

        It's best if the process is suspended (if alive) when taking the
        snapshot. Execution can be resumed afterwards.

        Example::

            # print(the memory contents of a process.)
            process.suspend()
            try:
                snapshot = process.take_memory_snapshot()
                for mbi in snapshot:
                    print(HexDump.hexblock(mbi.content, mbi.BaseAddress))
            finally:
                process.resume()

        You can also iterate the memory of a dead process, just as long as the
        last open handle to it hasn't been closed.

        .. warning::

            If the target process has a very big memory footprint, the
            resulting snapshot will be equally big. This may result in a severe
            performance penalty.

        .. seealso:: :meth:`generate_memory_snapshot`

        :param int minAddr: Optional. Starting address in address range to query.
        :param int maxAddr: Optional. Ending address in address range to query.
        :rtype: list[win32.MemoryBasicInformation]
        :return: List of memory region information objects.
            Two extra properties are added to these objects:

            - ``filename``: Mapped filename, or ``None``.
            - ``content``: Memory contents, or ``None``.
        """
        return list(self.iter_memory_snapshot(minAddr, maxAddr))

    def restore_memory_snapshot(
        self, snapshot, bSkipMappedFiles=True, bSkipOnError=False
    ):
        """
        Attempts to restore the memory state as it was when the given snapshot
        was taken.

        .. warning::

            Currently only the memory contents, state and protect bits
            are restored. Under some circumstances this method may fail (for
            example if memory was freed and then reused by a mapped file).

        :param list[win32.MemoryBasicInformation] snapshot:
            Memory snapshot returned by :meth:`take_memory_snapshot`.
            Snapshots returned by :meth:`generate_memory_snapshot` don't work here.
        :param bool bSkipMappedFiles:
            ``True`` to avoid restoring the contents of
            memory mapped files, ``False`` otherwise. Use with care! Setting
            this to ``True`` can cause undesired side effects - changes to
            memory mapped files may be written to disk by the OS. Also note
            that most mapped files are typically executables and don't change,
            so trying to restore their contents is usually a waste of time.
        :param bool bSkipOnError:
            ``True`` to issue a warning when an error occurs
            during the restoration of the snapshot, ``False`` to stop and raise
            an exception instead. Use with care! Setting this to ``True`` will
            cause the debugger to falsely believe the memory snapshot has been
            correctly restored.
        :raises WindowsError: An error occured while restoring the snapshot.
        :raises RuntimeError: An error occured while restoring the snapshot.
        :raises TypeError: A snapshot of the wrong type was passed.
        """
        if (
            not snapshot
            or not isinstance(snapshot, list)
            or not isinstance(snapshot[0], win32.MemoryBasicInformation)
        ):
            raise TypeError(
                "Only snapshots returned by take_memory_snapshot() can be used here."
            )

        # Get the process handle.
        hProcess = self.get_handle(
            win32.PROCESS_VM_WRITE
            | win32.PROCESS_VM_OPERATION
            | win32.PROCESS_SUSPEND_RESUME
            | win32.PROCESS_QUERY_INFORMATION
        )

        # Freeze the process.
        self.suspend()
        try:
            # For each memory region in the snapshot...
            for old_mbi in snapshot:
                # If the region matches, restore it directly.
                new_mbi = self.mquery(old_mbi.BaseAddress)
                if (
                    new_mbi.BaseAddress == old_mbi.BaseAddress
                    and new_mbi.RegionSize == old_mbi.RegionSize
                ):
                    self.__restore_mbi(
                        hProcess, new_mbi, old_mbi, bSkipMappedFiles, bSkipOnError
                    )

                # If the region doesn't match, restore it page by page.
                else:
                    # We need a copy so we don't corrupt the snapshot.
                    old_mbi = win32.MemoryBasicInformation(old_mbi)

                    # Get the overlapping range of pages.
                    old_start = old_mbi.BaseAddress
                    old_end = old_start + old_mbi.RegionSize
                    new_start = new_mbi.BaseAddress
                    new_end = new_start + new_mbi.RegionSize
                    if old_start > new_start:
                        start = old_start
                    else:
                        start = new_start
                    if old_end < new_end:
                        end = old_end
                    else:
                        end = new_end

                    # Restore each page in the overlapping range.
                    step = MemoryAddresses.pageSize
                    old_mbi.RegionSize = step
                    new_mbi.RegionSize = step
                    address = start
                    while address < end:
                        old_mbi.BaseAddress = address
                        new_mbi.BaseAddress = address
                        self.__restore_mbi(
                            hProcess, new_mbi, old_mbi, bSkipMappedFiles, bSkipOnError
                        )
                        address = address + step

        # Resume execution.
        finally:
            self.resume()

    def __restore_mbi(self, hProcess, new_mbi, old_mbi, bSkipMappedFiles, bSkipOnError):
        """
        Used internally by :meth:`restore_memory_snapshot`.
        """

        ##        print("Restoring %s-%s" % ()
        ##            HexDump.address(old_mbi.BaseAddress, self.get_bits()),
        ##            HexDump.address(old_mbi.BaseAddress + old_mbi.RegionSize,
        ##                            self.get_bits()))

        try:
            # Restore the region state.
            if new_mbi.State != old_mbi.State:
                if new_mbi.is_free():
                    if old_mbi.is_reserved():
                        # Free -> Reserved
                        address = win32.VirtualAllocEx(
                            hProcess,
                            old_mbi.BaseAddress,
                            old_mbi.RegionSize,
                            win32.MEM_RESERVE,
                            old_mbi.Protect,
                        )
                        if address != old_mbi.BaseAddress:
                            self.free(address)
                            msg = "Error restoring region at address %s"
                            msg = msg % HexDump(old_mbi.BaseAddress, self.get_bits())
                            raise RuntimeError(msg)
                        # permissions already restored
                        new_mbi.Protect = old_mbi.Protect

                    else:  # elif old_mbi.is_commited():
                        # Free -> Commited
                        address = win32.VirtualAllocEx(
                            hProcess,
                            old_mbi.BaseAddress,
                            old_mbi.RegionSize,
                            win32.MEM_RESERVE | win32.MEM_COMMIT,
                            old_mbi.Protect,
                        )
                        if address != old_mbi.BaseAddress:
                            self.free(address)
                            msg = "Error restoring region at address %s"
                            msg = msg % HexDump(old_mbi.BaseAddress, self.get_bits())
                            raise RuntimeError(msg)
                        # permissions already restored
                        new_mbi.Protect = old_mbi.Protect

                elif new_mbi.is_reserved():
                    if old_mbi.is_commited():
                        # Reserved -> Commited
                        address = win32.VirtualAllocEx(
                            hProcess,
                            old_mbi.BaseAddress,
                            old_mbi.RegionSize,
                            win32.MEM_COMMIT,
                            old_mbi.Protect,
                        )
                        if address != old_mbi.BaseAddress:
                            self.free(address)
                            msg = "Error restoring region at address %s"
                            msg = msg % HexDump(old_mbi.BaseAddress, self.get_bits())
                            raise RuntimeError(msg)
                        # permissions already restored
                        new_mbi.Protect = old_mbi.Protect

                    else:  # elif old_mbi.is_free():
                        # Reserved -> Free
                        win32.VirtualFreeEx(
                            hProcess,
                            old_mbi.BaseAddress,
                            old_mbi.RegionSize,
                            win32.MEM_RELEASE,
                        )

                else:  # elif new_mbi.is_commited():
                    if old_mbi.is_reserved():
                        # Commited -> Reserved
                        win32.VirtualFreeEx(
                            hProcess,
                            old_mbi.BaseAddress,
                            old_mbi.RegionSize,
                            win32.MEM_DECOMMIT,
                        )

                    else:  # elif old_mbi.is_free():
                        # Commited -> Free
                        win32.VirtualFreeEx(
                            hProcess,
                            old_mbi.BaseAddress,
                            old_mbi.RegionSize,
                            win32.MEM_DECOMMIT | win32.MEM_RELEASE,
                        )

            new_mbi.State = old_mbi.State

            # Restore the region permissions.
            if old_mbi.is_commited() and old_mbi.Protect != new_mbi.Protect:
                win32.VirtualProtectEx(
                    hProcess, old_mbi.BaseAddress, old_mbi.RegionSize, old_mbi.Protect
                )
                new_mbi.Protect = old_mbi.Protect

            # Restore the region data.
            # Ignore write errors when the region belongs to a mapped file.
            if old_mbi.has_content():
                if old_mbi.Type != 0:
                    if not bSkipMappedFiles:
                        self.poke(old_mbi.BaseAddress, old_mbi.content)
                else:
                    self.write(old_mbi.BaseAddress, old_mbi.content)
                new_mbi.content = old_mbi.content

        # On error, skip this region or raise an exception.
        except Exception:
            if not bSkipOnError:
                raise
            msg = "Error restoring region at address %s: %s"
            msg = msg % (
                HexDump(old_mbi.BaseAddress, self.get_bits()),
                traceback.format_exc(),
            )
            warnings.warn(msg, RuntimeWarning)

    # ------------------------------------------------------------------------------

    def inject_code(self, payload, lpParameter=0):
        """
        Injects relocatable code into the process memory and executes it.

        .. warning::

            Don't forget to free the memory when you're done with it!
            Otherwise you'll be leaking memory in the target process.

        .. seealso:: :meth:`inject_dll`

        :param str payload: Relocatable code to run in a new thread.
        :param int lpParameter:
            Optional parameter to be pushed in the stack.
        :rtype: tuple[Thread, int]
        :return:
            The injected :class:`Thread` object
            and the memory address where the code was written.
        :raises WindowsError: An exception is raised on error.
        """

        # Uncomment for debugging...
        ##        payload = '\xCC' + payload

        # Allocate the memory for the shellcode.
        lpStartAddress = self.malloc(len(payload))

        # Catch exceptions so we can free the memory on error.
        try:
            # Write the shellcode to our memory location.
            self.write(lpStartAddress, payload)

            # Start a new thread for the shellcode to run.
            aThread = self.start_thread(lpStartAddress, lpParameter, bSuspended=False)

            # Remember the shellcode address.
            #  It will be freed ONLY by the Thread.kill() method
            #  and the EventHandler class, otherwise you'll have to
            #  free it in your code, or have your shellcode clean up
            #  after itself (recommended).
            aThread.pInjectedMemory = lpStartAddress

        # Free the memory on error.
        except Exception:
            self.free(lpStartAddress)
            raise

        # Return the Thread object and the shellcode address.
        return aThread, lpStartAddress

    # TODO
    # The shellcode should check for errors, otherwise it just crashes
    # when the DLL can't be loaded or the procedure can't be found.
    # On error the shellcode should execute an int3 instruction.
    def inject_dll(
        self, dllname, procname=None, lpParameter=0, bWait=True, dwTimeout=None
    ):
        """
        Injects a DLL into the process memory.

        .. warning::

            Setting ``bWait`` to ``True`` when the process is frozen by a
            debug event will cause a deadlock in your debugger.

        .. warning::

            This involves allocating memory in the target process.
            This is how the freeing of this memory is handled:

             - If the ``bWait`` flag is set to ``True`` the memory will be freed
               automatically before returning from this method.
             - If the ``bWait`` flag is set to ``False``, the memory address is
               set as the :attr:`Thread.pInjectedMemory` property of the returned
               thread object.
             - :class:`Debug` objects free :attr:`Thread.pInjectedMemory` automatically
               both when it detaches from a process and when the injected
               thread finishes its execution.
             - The :meth:`Thread.kill` method also frees :attr:`Thread.pInjectedMemory`
               automatically, even if you're not attached to the process.

            You could still be leaking memory if not careful. For example, if
            you inject a dll into a process you're not attached to, you don't
            wait for the thread's completion and you don't kill it either, the
            memory would be leaked.

        .. seealso:: :meth:`inject_code`

        :param str dllname: Name of the DLL module to load.
        :param str procname:
            Optional procedure to call when the DLL is loaded.
        :param int lpParameter:
            Optional parameter to the ``procname`` procedure.
        :param bool bWait:
            ``True`` to wait for the process to finish.
            ``False`` to return immediately.
        :param int dwTimeout:
            Optional timeout value in milliseconds.
            Ignored if ``bWait`` is ``False``.
        :rtype: Thread
        :return:
            Newly created thread object. If ``bWait`` is set to ``True`` the
            thread will be dead, otherwise it will be alive.
        :raises NotImplementedError:
            The target platform is not supported.
            Currently calling a procedure in the library is only supported in
            the i386 architecture.
        :raises WindowsError: An exception is raised on error.
        """

        # Resolve kernel32.dll
        aModule = self.get_module_by_name("kernel32.dll")
        if aModule is None:
            self.scan_modules()
            aModule = self.get_module_by_name("kernel32.dll")
        if aModule is None:
            raise RuntimeError("Cannot resolve kernel32.dll in the remote process")

        # Old method, using shellcode.
        if procname:
            if self.get_arch() != win32.ARCH_I386:
                raise NotImplementedError()
            dllname = str(dllname)

            # Resolve kernel32.dll!LoadLibraryA
            pllib = aModule.resolve("LoadLibraryA")
            if not pllib:
                raise RuntimeError(
                    "Cannot resolve kernel32.dll!LoadLibraryA in the remote process"
                )

            # Resolve kernel32.dll!GetProcAddress
            pgpad = aModule.resolve("GetProcAddress")
            if not pgpad:
                raise RuntimeError(
                    "Cannot resolve kernel32.dll!GetProcAddress in the remote process"
                )

            # Resolve kernel32.dll!VirtualFree
            pvf = aModule.resolve("VirtualFree")
            if not pvf:
                raise RuntimeError(
                    "Cannot resolve kernel32.dll!VirtualFree in the remote process"
                )

            # Shellcode follows...
            code = b""

            # push dllname
            code += (
                b"\xe8"
                + struct.pack("<L", len(dllname) + 1)
                + dllname.encode("ascii")
                + b"\0"
            )

            # mov eax, LoadLibraryA
            code += b"\xb8" + struct.pack("<L", pllib)

            # call eax
            code += b"\xff\xd0"

            if procname:
                # push procname
                code += b"\xe8" + struct.pack("<L", len(procname) + 1)
                code += procname.encode("ascii") + b"\0"

                # push eax
                code += b"\x50"

                # mov eax, GetProcAddress
                code += b"\xb8" + struct.pack("<L", pgpad)

                # call eax
                code += b"\xff\xd0"

                # mov ebp, esp      ; preserve stack pointer
                code += b"\x8b\xec"

                # push lpParameter
                code += b"\x68" + struct.pack("<L", lpParameter)

                # call eax
                code += b"\xff\xd0"

                # mov esp, ebp      ; restore stack pointer
                code += b"\x8b\xe5"

            # pop edx       ; our own return address
            code += b"\x5a"

            # push MEM_RELEASE  ; dwFreeType
            code += b"\x68" + struct.pack("<L", win32.MEM_RELEASE)

            # push 0x1000       ; dwSize, shellcode max size 4096 bytes
            code += b"\x68" + struct.pack("<L", 0x1000)

            # call $+5
            code += b"\xe8\x00\x00\x00\x00"

            # and dword ptr [esp], 0xFFFFF000   ; align to page boundary
            code += b"\x81\x24\x24\x00\xf0\xff\xff"

            # mov eax, VirtualFree
            code += b"\xb8" + struct.pack("<L", pvf)

            # push edx      ; our own return address
            code += b"\x52"

            # jmp eax   ; VirtualFree will return to our own return address
            code += b"\xff\xe0"

            # Inject the shellcode.
            # There's no need to free the memory,
            # because the shellcode will free it itself.
            aThread, lpStartAddress = self.inject_code(code, lpParameter)

        # New method, not using shellcode.
        else:
            # Resolve kernel32.dll!LoadLibrary (A/W)
            if isinstance(dllname, str):
                pllibname = "LoadLibraryW"
                dllname_bytes = dllname.encode("utf-16le") + b"\0\0"
            else:
                pllibname = "LoadLibraryA"
                dllname_bytes = dllname + b"\0"
            bufferlen = len(dllname_bytes)

            pllib = aModule.resolve(pllibname)
            if not pllib:
                msg = "Cannot resolve kernel32.dll!%s in the remote process"
                raise RuntimeError(msg % pllibname)

            # Copy the library name into the process memory space.
            pbuffer = self.malloc(bufferlen)
            try:
                self.write(pbuffer, dllname_bytes)

                # Create a new thread to load the library.
                try:
                    aThread = self.start_thread(pllib, pbuffer)
                except WindowsError as e:
                    if e.winerror != win32.ERROR_NOT_ENOUGH_MEMORY:
                        raise

                    # This specific error is caused by trying to spawn a new
                    # thread in a process belonging to a different Terminal
                    # Services session (for example a service).
                    raise NotImplementedError(
                        "Target process belongs to a different"
                        " Terminal Services session, cannot inject!"
                    )

                # Remember the buffer address.
                #  It will be freed ONLY by the Thread.kill() method
                #  and the EventHandler class, otherwise you'll have to
                #  free it in your code.
                aThread.pInjectedMemory = pbuffer

            # Free the memory on error.
            except Exception:
                self.free(pbuffer)
                raise

        # Wait for the thread to finish.
        if bWait:
            aThread.wait(dwTimeout)
            self.free(aThread.pInjectedMemory)
            del aThread.pInjectedMemory

        # Return the thread object.
        return aThread

    def clean_exit(self, dwExitCode=0, bWait=False, dwTimeout=None):
        """
        Injects a new thread to call ExitProcess().
        Optionally waits for the injected thread to finish.

        .. warning::

            Setting ``bWait`` to ``True`` when the process is frozen by a
            debug event will cause a deadlock in your debugger.

        :param int dwExitCode: Process exit code.
        :param bool bWait:
            ``True`` to wait for the process to finish.
            ``False`` to return immediately.
        :param int dwTimeout:
            Optional timeout value in milliseconds.
            Ignored if ``bWait`` is ``False``.
        :raises WindowsError: An exception is raised on error.
        """
        if not dwExitCode:
            dwExitCode = 0
        pExitProcess = self.resolve_label("kernel32!ExitProcess")
        aThread = self.start_thread(pExitProcess, dwExitCode)
        if bWait:
            aThread.wait(dwTimeout)

    # ------------------------------------------------------------------------------

    def _notify_create_process(self, event):
        """
        Notify the creation of a new process.

        This is done automatically by the :class:`Debug` class, you shouldn't need
        to call it yourself.

        :param CreateProcessEvent event: Create process event.
        :rtype: bool
        :return: ``True`` to call the user-defined handle, ``False`` otherwise.
        """
        # Do not use super() here.
        bCallHandler = _ThreadContainer._notify_create_process(self, event)
        bCallHandler = bCallHandler and _ModuleContainer._notify_create_process(
            self, event
        )
        return bCallHandler


# ==============================================================================


class _ProcessContainer:
    """
    Encapsulates the capability to contain Process objects.

    .. rubric:: Instrumentation

    .. automethod:: start_process
    .. automethod:: argv_to_cmdline
    .. automethod:: cmdline_to_argv
    .. automethod:: get_explorer_pid

    .. rubric:: Processes snapshot

    .. automethod:: scan
    .. automethod:: scan_processes
    .. automethod:: scan_processes_fast
    .. automethod:: get_process
    .. automethod:: get_process_count
    .. automethod:: get_process_ids
    .. automethod:: has_process
    .. automethod:: iter_processes
    .. automethod:: iter_process_ids
    .. automethod:: find_processes_by_filename
    .. automethod:: get_pid_from_tid
    .. automethod:: get_windows
    .. automethod:: scan_process_filenames
    .. automethod:: clear
    .. automethod:: clear_processes
    .. automethod:: clear_dead_processes
    .. automethod:: clear_unattached_processes
    .. automethod:: close_process_handles
    .. automethod:: close_process_and_thread_handles

    .. rubric:: Threads snapshots

    .. automethod:: scan_processes_and_threads
    .. automethod:: get_thread
    .. automethod:: get_thread_count
    .. automethod:: get_thread_ids
    .. automethod:: has_thread

    .. rubric:: Modules snapshots

    .. automethod:: scan_modules
    .. automethod:: find_modules_by_address
    .. automethod:: find_modules_by_base
    .. automethod:: find_modules_by_name
    .. automethod:: get_module_count
    """

    def __init__(self):
        self.__processDict = dict()

    def __initialize_snapshot(self):
        """
        Private method to automatically initialize the snapshot
        when you try to use it without calling any of the scan_*
        methods first. You don't need to call this yourself.
        """
        if not self.__processDict:
            try:
                self.scan_processes()  # remote desktop api (relative fn)
            except Exception:
                self.scan_processes_fast()  # psapi (no filenames)
            self.scan_process_filenames()  # get the pathnames when possible

    def __contains__(self, anObject):
        """
        :param anObject:
             - int: Global ID of the process to look for.
             - int: Global ID of the thread to look for.
             - Process: Process object to look for.
             - Thread: Thread object to look for.
        :rtype: bool
        :return:
            ``True`` if the snapshot contains
            a :class:`Process` or :class:`Thread` object with the same ID.
        """
        if isinstance(anObject, Process):
            anObject = anObject.dwProcessId
        if self.has_process(anObject):
            return True
        for aProcess in self.iter_processes():
            if anObject in aProcess:
                return True
        return False

    def __iter__(self):
        """
        .. seealso:: :meth:`iter_processes`
        :rtype: dictionary-valueiterator
        :return: Iterator of :class:`Process` objects in this snapshot.
        """
        return self.iter_processes()

    def __len__(self):
        """
        .. seealso:: :meth:`get_process_count`
        :rtype: int
        :return: Count of :class:`Process` objects in this snapshot.
        """
        return self.get_process_count()

    def has_process(self, dwProcessId):
        """
        :param int dwProcessId: Global ID of the process to look for.
        :rtype: bool
        :return:
            ``True`` if the snapshot contains a
            :class:`Process` object with the given global ID.
        """
        self.__initialize_snapshot()
        return dwProcessId in self.__processDict

    def get_process(self, dwProcessId):
        """
        :param int dwProcessId: Global ID of the process to look for.
        :rtype: Process
        :return: Process object with the given global ID.
        :raises KeyError: The process does not exist.
        """
        self.__initialize_snapshot()
        if dwProcessId not in self.__processDict:
            msg = "Unknown process ID %d" % dwProcessId
            raise KeyError(msg)
        return self.__processDict[dwProcessId]

    def iter_process_ids(self):
        """
        .. seealso:: :meth:`iter_processes`
        :rtype: dictionary-keyiterator
        :return: Iterator of global process IDs in this snapshot.
        """
        self.__initialize_snapshot()
        return self.__processDict.keys()

    def iter_processes(self):
        """
        .. seealso:: :meth:`iter_process_ids`
        :rtype: dictionary-valueiterator
        :return: Iterator of :class:`Process` objects in this snapshot.
        """
        self.__initialize_snapshot()
        return self.__processDict.values()

    def get_process_ids(self):
        """
        .. seealso:: :meth:`iter_process_ids`
        :rtype: list[int]
        :return: List of global process IDs in this snapshot.
        """
        self.__initialize_snapshot()
        return self.__processDict.keys()

    def get_process_count(self):
        """
        :rtype: int
        :return: Count of :class:`Process` objects in this snapshot.
        """
        self.__initialize_snapshot()
        return len(self.__processDict)

    # ------------------------------------------------------------------------------

    # XXX TODO
    # Support for string searches on the window captions.

    def get_windows(self):
        """
        :rtype: list[Window]
        :return: Returns a list of windows
            handled by all processes in this snapshot.
        """
        """window_list = dict()
        for process in self.iter_processes():
            window_list.extend( process.get_windows() )
        return window_list"""

        hCurWnd = None
        window_list = list()
        while True:
            hCurWnd = win32.FindWindowEx(None, hCurWnd, None, None)
            if hCurWnd is None:
                break
            window_list.append(Window(hCurWnd))
        return window_list

    def get_pid_from_tid(self, dwThreadId):
        """
        Retrieves the global ID of the process that owns the thread.

        :param int dwThreadId: Thread global ID.
        :rtype: int
        :return: Process global ID.
        :raises KeyError: The thread does not exist.
        """
        try:
            # No good, because in XP and below it tries to get the PID
            # through the toolhelp API, and that's slow. We don't want
            # to scan for threads over and over for each call.
            ##            dwProcessId = Thread(dwThreadId).get_pid()

            # This API only exists in Windows 2003, Vista and above.
            try:
                hThread = win32.OpenThread(
                    win32.THREAD_QUERY_LIMITED_INFORMATION, False, dwThreadId
                )
            except (WindowsError, AttributeError) as e:
                try:
                    # Fallback for older systems
                    hThread = win32.OpenThread(
                        win32.THREAD_QUERY_INFORMATION, False, dwThreadId
                    )
                except WindowsError as e2:
                    if (
                        hasattr(e, "winerror")
                        and e.winerror != win32.ERROR_ACCESS_DENIED
                    ):
                        raise
                    raise e2

            try:
                return win32.GetProcessIdOfThread(hThread)
            finally:
                hThread.close()

        # If all else fails, go through all processes in the snapshot
        # looking for the one that owns the thread we're looking for.
        # If the snapshot was empty the iteration should trigger an
        # automatic scan. Otherwise, it'll look for the thread in what
        # could possibly be an outdated snapshot.
        except Exception:
            for aProcess in self.iter_processes():
                if aProcess.has_thread(dwThreadId):
                    return aProcess.get_pid()

        # The thread wasn't found, so let's refresh the snapshot and retry.
        # Normally this shouldn't happen since this function is only useful
        # for the debugger, so the thread should already exist in the snapshot.
        self.scan_processes_and_threads()
        for aProcess in self.iter_processes():
            if aProcess.has_thread(dwThreadId):
                return aProcess.get_pid()

        # No luck! It appears to be the thread doesn't exist after all.
        msg = "Unknown thread ID %d" % dwThreadId
        raise KeyError(msg)

    # ------------------------------------------------------------------------------

    @staticmethod
    def argv_to_cmdline(argv):
        """
        Convert a list of arguments to a single command line string.

        :param list[str] argv: List of argument strings.
            The first element is the program to execute.
        :rtype: str
        :return: Command line string.
        """
        cmdline = list()
        for token in argv:
            if not token:
                token = '""'
            else:
                if '"' in token:
                    token = token.replace('"', '\\"')
                if " " in token or "\t" in token or "\n" in token or "\r" in token:
                    token = '"%s"' % token
            cmdline.append(token)
        return " ".join(cmdline)

    @staticmethod
    def cmdline_to_argv(lpCmdLine):
        """
        Convert a single command line string to a list of arguments.

        :param str lpCmdLine: Command line string.
            The first token is the program to execute.
        :rtype: list[str]
        :return: List of argument strings.
        """
        if not lpCmdLine:
            return []
        return win32.CommandLineToArgv(lpCmdLine)

    def start_process(self, lpCmdLine, **kwargs):
        """
        Starts a new process for instrumenting (or debugging).

        :param str lpCmdLine: Command line to execute. Can't be an empty string.
        :param bool bConsole:
            True to inherit the console of the debugger.
            Defaults to False.
        :param bool bDebug:
            True to attach to the new process.
            To debug a process it's best to use the :class:`Debug` class instead.
            Defaults to False.
        :param bool bFollow:
            True to automatically attach to the child
            processes of the newly created process. Ignored unless ``bDebug`` is
            True. Defaults to False.
        :param bool bInheritHandles:
            True if the new process should inherit
            it's parent process' handles. Defaults to False.
        :param bool bSuspended:
            True to suspend the main thread before any code
            is executed in the debugee. Defaults to False.
        :param int|None dwParentProcessId:
            None if the debugger process should be
            the parent process (default), or a process ID to forcefully set as
            the debugee's parent (only available for Windows Vista and above).
        :param int iTrustLevel: Trust level.
            Must be one of the following values:
             - 0: No trust. May not access certain resources, such as
                  cryptographic keys and credentials. Only available since
                  Windows XP and 2003, desktop editions.
             - 1: Normal trust. Run with the same privileges as a normal
                  user, that is, one that doesn't have the *Administrator* or
                  *Power User* user rights. Only available since Windows XP
                  and 2003, desktop editions.
             - 2: Full trust. Run with the exact same privileges as the
                  current user. This is the default value.
        :param bool bAllowElevation:
            True to allow the child process to keep
            UAC elevation, if the debugger itself is running elevated. False
            to ensure the child process doesn't run with elevation. Defaults to
            True.

            This flag is only meaningful on Windows Vista and above, and if the
            debugger itself is running with elevation. It can be used to make
            sure the child processes don't run elevated as well.

            This flag DOES NOT force an elevation prompt when the debugger is
            not running with elevation.

            Note that running the debugger with elevation (or the Python
            interpreter at all for that matter) is not normally required.
            You should only need to if the target program requires elevation
            to work properly (for example if you try to debug an installer).

        :rtype: Process
        :return: Process object.
        :raises TypeError: Unknown keyword arguments.
        :raises ValueError: Missing command line to execute.
        """

        # Get the flags.
        bConsole = kwargs.pop("bConsole", False)
        bDebug = kwargs.pop("bDebug", False)
        bFollow = kwargs.pop("bFollow", False)
        bSuspended = kwargs.pop("bSuspended", False)
        bInheritHandles = kwargs.pop("bInheritHandles", False)
        dwParentProcessId = kwargs.pop("dwParentProcessId", None)
        iTrustLevel = kwargs.pop("iTrustLevel", 2)
        bAllowElevation = kwargs.pop("bAllowElevation", True)
        if kwargs:
            raise TypeError("Unknown keyword arguments: %s" % list(kwargs.keys()))
        if not lpCmdLine:
            raise ValueError("Missing command line to execute!")

        # Sanitize the trust level flag.
        if iTrustLevel is None:
            iTrustLevel = 2

        # The UAC elevation flag is only meaningful if we're running elevated.
        try:
            bAllowElevation = bAllowElevation or not self.is_admin()
        except AttributeError:
            bAllowElevation = True
            warnings.warn(
                "UAC elevation is only available in Windows Vista and above",
                RuntimeWarning,
            )

        # Calculate the process creation flags.
        dwCreationFlags = 0
        dwCreationFlags |= win32.CREATE_DEFAULT_ERROR_MODE
        dwCreationFlags |= win32.CREATE_BREAKAWAY_FROM_JOB
        ##dwCreationFlags |= win32.CREATE_UNICODE_ENVIRONMENT
        if not bConsole:
            dwCreationFlags |= win32.DETACHED_PROCESS
            # dwCreationFlags |= win32.CREATE_NO_WINDOW   # weird stuff happens
        if bSuspended:
            dwCreationFlags |= win32.CREATE_SUSPENDED
        if bDebug:
            dwCreationFlags |= win32.DEBUG_PROCESS
            if not bFollow:
                dwCreationFlags |= win32.DEBUG_ONLY_THIS_PROCESS

        # Change the parent process if requested.
        # May fail on old versions of Windows.
        lpStartupInfo = None
        if dwParentProcessId is not None:
            myPID = win32.GetCurrentProcessId()
            if dwParentProcessId != myPID:
                if self.has_process(dwParentProcessId):
                    ParentProcess = self.get_process(dwParentProcessId)
                else:
                    ParentProcess = Process(dwParentProcessId)
                ParentProcessHandle = ParentProcess.get_handle(
                    win32.PROCESS_CREATE_PROCESS
                )
                AttributeListData = (
                    (
                        win32.PROC_THREAD_ATTRIBUTE_PARENT_PROCESS,
                        ParentProcessHandle._as_parameter_,
                    ),
                )
                AttributeList = win32.ProcThreadAttributeList(AttributeListData)
                StartupInfoEx = win32.STARTUPINFOEX()
                StartupInfo = StartupInfoEx.StartupInfo
                StartupInfo.cb = win32.sizeof(win32.STARTUPINFOEX)
                StartupInfo.lpReserved = 0
                StartupInfo.lpDesktop = 0
                StartupInfo.lpTitle = 0
                StartupInfo.dwFlags = 0
                StartupInfo.cbReserved2 = 0
                StartupInfo.lpReserved2 = 0
                StartupInfoEx.lpAttributeList = AttributeList.value
                lpStartupInfo = StartupInfoEx
                dwCreationFlags |= win32.EXTENDED_STARTUPINFO_PRESENT

        pi = None
        try:
            # Create the process the easy way.
            if iTrustLevel >= 2 and bAllowElevation:
                pi = win32.CreateProcess(
                    None,
                    lpCmdLine,
                    bInheritHandles=bInheritHandles,
                    dwCreationFlags=dwCreationFlags,
                    lpStartupInfo=lpStartupInfo,
                )

            # Create the process the hard way...
            else:
                # If we allow elevation, use the current process token.
                # If not, get the token from the current shell process.
                hToken = None
                try:
                    if not bAllowElevation:
                        if bFollow:
                            msg = (
                                "Child processes can't be autofollowed"
                                " when dropping UAC elevation."
                            )
                            raise NotImplementedError(msg)
                        if bConsole:
                            msg = (
                                "Child processes can't inherit the debugger's"
                                " console when dropping UAC elevation."
                            )
                            raise NotImplementedError(msg)
                        if bInheritHandles:
                            msg = (
                                "Child processes can't inherit the debugger's"
                                " handles when dropping UAC elevation."
                            )
                            raise NotImplementedError(msg)
                        try:
                            hWnd = self.get_shell_window()
                        except WindowsError:
                            hWnd = self.get_desktop_window()
                        shell = hWnd.get_process()
                        try:
                            hShell = shell.get_handle(win32.PROCESS_QUERY_INFORMATION)
                            with win32.OpenProcessToken(hShell) as hShellToken:
                                hToken = win32.DuplicateTokenEx(hShellToken)
                        finally:
                            shell.close_handle()

                    # Lower trust level if requested.
                    if iTrustLevel < 2:
                        if iTrustLevel > 0:
                            dwLevelId = win32.SAFER_LEVELID_NORMALUSER
                        else:
                            dwLevelId = win32.SAFER_LEVELID_UNTRUSTED
                        with win32.SaferCreateLevel(dwLevelId=dwLevelId) as hSafer:
                            hSaferToken = win32.SaferComputeTokenFromLevel(
                                hSafer, hToken
                            )[0]
                            try:
                                if hToken is not None:
                                    hToken.close()
                            except:
                                hSaferToken.close()
                                raise
                            hToken = hSaferToken

                    # If we have a computed token, call CreateProcessAsUser().
                    if bAllowElevation:
                        pi = win32.CreateProcessAsUser(
                            hToken=hToken,
                            lpCommandLine=lpCmdLine,
                            bInheritHandles=bInheritHandles,
                            dwCreationFlags=dwCreationFlags,
                            lpStartupInfo=lpStartupInfo,
                        )

                    # If we have a primary token call CreateProcessWithToken().
                    # The problem is, there are many flags CreateProcess() and
                    # CreateProcessAsUser() accept but CreateProcessWithToken()
                    # and CreateProcessWithLogonW() don't, so we need to work
                    # around them.
                    else:
                        # Remove the debug flags.
                        dwCreationFlags &= ~win32.DEBUG_PROCESS
                        dwCreationFlags &= ~win32.DEBUG_ONLY_THIS_PROCESS

                        # Remove the console flags.
                        dwCreationFlags &= ~win32.DETACHED_PROCESS

                        # The process will be created suspended.
                        dwCreationFlags |= win32.CREATE_SUSPENDED

                        # Create the process using the new primary token.
                        pi = win32.CreateProcessWithToken(
                            hToken=hToken,
                            dwLogonFlags=win32.LOGON_WITH_PROFILE,
                            lpCommandLine=lpCmdLine,
                            dwCreationFlags=dwCreationFlags,
                            lpStartupInfo=lpStartupInfo,
                        )

                        # Attach as a debugger, if requested.
                        if bDebug:
                            win32.DebugActiveProcess(pi.dwProcessId)

                        # Resume execution, if requested.
                        if not bSuspended:
                            win32.ResumeThread(pi.hThread)

                # Close the token when we're done with it.
                finally:
                    if hToken is not None:
                        hToken.close()

            # Wrap the new process and thread in Process and Thread objects,
            # and add them to the corresponding snapshots.
            aProcess = Process(pi.dwProcessId, pi.hProcess)
            aThread = Thread(pi.dwThreadId, pi.hThread)
            aProcess._add_thread(aThread)
            self._add_process(aProcess)

        # Clean up on error.
        except:
            if pi is not None:
                try:
                    win32.TerminateProcess(pi.hProcess)
                except WindowsError:
                    pass
                pi.hThread.close()
                pi.hProcess.close()
            raise

        # Return the new Process object.
        return aProcess

    def get_explorer_pid(self):
        """
        Tries to find the process ID for "explorer.exe".

        :rtype: int|None
        :return: Returns the process ID, or None on error.
        """
        try:
            exp = win32.SHGetFolderPath(win32.CSIDL_WINDOWS)
        except Exception:
            exp = None
        if not exp:
            exp = getenv("SystemRoot")
        if exp:
            exp = ntpath.join(exp, "explorer.exe")
            exp_list = self.find_processes_by_filename(exp)
            if exp_list:
                return exp_list[0][0].get_pid()
        return None

    # ------------------------------------------------------------------------------

    # XXX this methods musn't end up calling __initialize_snapshot by accident!

    def scan(self):
        """
        Populates the snapshot with running processes and threads,
        and loaded modules.

        Typically this is the first method called after instantiating a
        :class:`System` object, as it makes a best effort approach to gathering
        information on running processes.

        :rtype: bool
        :return:
            True if the snapshot is complete, False if the debugger
            doesn't have permission to scan some processes. In either case, the
            snapshot is complete for all processes the debugger has access to.
        """
        has_threads = True
        try:
            try:
                # Try using the Toolhelp API
                # to scan for processes and threads.
                self.scan_processes_and_threads()

            except Exception:
                # On error, try using the PSAPI to scan for process IDs only.
                self.scan_processes_fast()

                # Now try using the Toolhelp again to get the threads.
                for aProcess in self.__processDict.values():
                    if aProcess._get_thread_ids():
                        try:
                            aProcess.scan_threads()
                        except WindowsError:
                            has_threads = False

        finally:
            # Try using the Remote Desktop API to scan for processes only.
            # This will update the filenames when it's not possible
            # to obtain them from the Toolhelp API.
            self.scan_processes()

        # When finished scanning for processes, try modules too.
        has_modules = self.scan_modules()

        # Try updating the process filenames when possible.
        has_full_names = self.scan_process_filenames()

        # Return the completion status.
        return has_threads and has_modules and has_full_names

    def scan_processes_and_threads(self):
        """
        Populates the snapshot with running processes and threads.

        Typically you don't need to call this method directly, if unsure use
        :meth:`scan` instead.

        .. note::

            This method uses the Toolhelp API.

        .. seealso:: :meth:`scan_modules`

        :raises WindowsError:
            An error occured while updating the snapshot.
            The snapshot was not modified.
        """

        # The main module filename may be spoofed by malware,
        # since this information resides in usermode space.
        # See: http://www.ragestorm.net/blogs/?p=163

        our_pid = win32.GetCurrentProcessId()
        dead_pids = set(self.__processDict.keys())
        found_tids = set()

        # Ignore our own process if it's in the snapshot for some reason
        if our_pid in dead_pids:
            dead_pids.remove(our_pid)

        # Take a snapshot of all processes and threads
        dwFlags = win32.TH32CS_SNAPPROCESS | win32.TH32CS_SNAPTHREAD
        with win32.CreateToolhelp32Snapshot(dwFlags) as hSnapshot:
            # Add all the processes (excluding our own)
            pe = win32.Process32First(hSnapshot)
            while pe is not None:
                dwProcessId = pe.th32ProcessID
                if dwProcessId != our_pid:
                    if dwProcessId in dead_pids:
                        dead_pids.remove(dwProcessId)
                    fileName = pe.szExeFile
                    if isinstance(fileName, bytes):
                        fileName = fileName.decode("mbcs", "ignore")
                    if dwProcessId not in self.__processDict:
                        aProcess = Process(dwProcessId, fileName=fileName)
                        self._add_process(aProcess)
                    elif fileName:
                        aProcess = self.get_process(dwProcessId)
                        if not aProcess.fileName:
                            aProcess.fileName = fileName
                pe = win32.Process32Next(hSnapshot)

            # Add all the threads
            te = win32.Thread32First(hSnapshot)
            while te is not None:
                dwProcessId = te.th32OwnerProcessID
                if dwProcessId != our_pid:
                    if dwProcessId in dead_pids:
                        dead_pids.remove(dwProcessId)
                    if dwProcessId in self.__processDict:
                        aProcess = self.get_process(dwProcessId)
                    else:
                        aProcess = Process(dwProcessId)
                        self._add_process(aProcess)
                    dwThreadId = te.th32ThreadID
                    found_tids.add(dwThreadId)
                    if not aProcess._has_thread_id(dwThreadId):
                        aThread = Thread(dwThreadId, process=aProcess)
                        aProcess._add_thread(aThread)
                te = win32.Thread32Next(hSnapshot)

        # Remove dead processes
        for pid in dead_pids:
            self._del_process(pid)

        # Remove dead threads
        for aProcess in self.__processDict.values():
            dead_tids = set(aProcess._get_thread_ids())
            dead_tids.difference_update(found_tids)
            for tid in dead_tids:
                aProcess._del_thread(tid)

    def scan_modules(self):
        """
        Populates the snapshot with loaded modules.

        Typically you don't need to call this method directly, if unsure use
        :meth:`scan` instead.

        .. note::

            This method uses the Toolhelp API.

        .. seealso:: :meth:`scan_processes_and_threads`

        :rtype: bool
        :return:
            True if the snapshot is complete, False if the debugger
            doesn't have permission to scan some processes. In either case, the
            snapshot is complete for all processes the debugger has access to.
        """
        complete = True
        for aProcess in self.__processDict.values():
            try:
                aProcess.scan_modules()
            except WindowsError:
                complete = False
        return complete

    def scan_processes(self):
        """
        Populates the snapshot with running processes.

        Typically you don't need to call this method directly, if unsure use
        :meth:`scan` instead.

        .. note::

            This method uses the Remote Desktop API instead of the Toolhelp
            API. It might give slightly different results, especially if the
            current process does not have full privileges.

        .. note::

            This method will only retrieve process filenames. To get the
            process pathnames instead, after this method call
            :meth:`scan_process_filenames`.

        :raises WindowsError:
            An error occured while updating the snapshot.
            The snapshot was not modified.
        """

        # Get the previous list of PIDs.
        # We'll be removing live PIDs from it as we find them.
        our_pid = win32.GetCurrentProcessId()
        dead_pids = set(self.__processDict.keys())

        # Ignore our own PID.
        if our_pid in dead_pids:
            dead_pids.remove(our_pid)

        # Get the list of processes from the Remote Desktop API.
        pProcessInfo = None
        try:
            pProcessInfo, dwCount = win32.WTSEnumerateProcesses(
                win32.WTS_CURRENT_SERVER_HANDLE
            )

            # For each process found...
            for index in range(dwCount):
                sProcessInfo = pProcessInfo[index]

                ##                # Ignore processes belonging to other sessions.
                ##                if sProcessInfo.SessionId != win32.WTS_CURRENT_SESSION:
                ##                    continue

                # Ignore our own PID.
                pid = sProcessInfo.ProcessId
                if pid == our_pid:
                    continue

                # Remove the PID from the dead PIDs list.
                if pid in dead_pids:
                    dead_pids.remove(pid)

                # Get the "process name".
                # Empirically, this seems to be the filename without the path.
                # (The MSDN docs aren't very clear about this API call).
                fileName = sProcessInfo.pProcessName
                if isinstance(fileName, bytes):
                    fileName = fileName.decode("mbcs", "ignore")

                # If the process is new, add a new Process object.
                if pid not in self.__processDict:
                    aProcess = Process(pid, fileName=fileName)
                    self._add_process(aProcess)

                # If the process was already in the snapshot, and the
                # filename is missing, update the Process object.
                elif fileName:
                    aProcess = self.__processDict.get(pid)
                    if not aProcess.fileName:
                        aProcess.fileName = fileName

        # Free the memory allocated by the Remote Desktop API.
        finally:
            if pProcessInfo is not None:
                try:
                    win32.WTSFreeMemory(pProcessInfo)
                except WindowsError:
                    pass

        # At this point the only remaining PIDs from the old list are dead.
        # Remove them from the snapshot.
        for pid in dead_pids:
            self._del_process(pid)

    def scan_processes_fast(self):
        """
        Populates the snapshot with running processes.
        Only the PID is retrieved for each process.

        Dead processes are removed.
        Threads and modules of living processes are ignored.

        Typically you don't need to call this method directly, if unsure use
        :meth:`scan` instead.

        .. note::

            This method uses the PSAPI. It may be faster for scanning,
            but some information may be missing, outdated or slower to obtain.
            This could be a good tradeoff under some circumstances.
        """

        # Get the new and old list of pids
        new_pids = set(win32.EnumProcesses())
        old_pids = set(self.__processDict.keys())

        # Ignore our own pid
        our_pid = win32.GetCurrentProcessId()
        if our_pid in new_pids:
            new_pids.remove(our_pid)
        if our_pid in old_pids:
            old_pids.remove(our_pid)

        # Add newly found pids
        for pid in new_pids.difference(old_pids):
            self._add_process(Process(pid))

        # Remove missing pids
        for pid in old_pids.difference(new_pids):
            self._del_process(pid)

    def scan_process_filenames(self):
        """
        Update the filename for each process in the snapshot when possible.

        .. note::

            Typically you don't need to call this method. It's called
            automatically by :meth:`scan` to get the full pathname for each process
            when possible, since some scan methods only get filenames without
            the path component.

            If unsure, use :meth:`scan` instead.

        .. seealso:: :meth:`scan`, :meth:`Process.get_filename`

        :rtype: bool
        :return:
            True if all the pathnames were retrieved, False if the
            debugger doesn't have permission to scan some processes. In either
            case, all processes the debugger has access to have a full pathname
            instead of just a filename.
        """
        complete = True
        for aProcess in self.__processDict.values():
            try:
                new_name = None
                old_name = aProcess.fileName
                try:
                    aProcess.fileName = None
                    new_name = aProcess.get_filename()
                finally:
                    if not new_name:
                        aProcess.fileName = old_name
                        complete = False
            except Exception:
                complete = False
        return complete

    # ------------------------------------------------------------------------------

    def clear_dead_processes(self):
        """
        Removes Process objects from the snapshot
        referring to processes no longer running.
        """
        for pid in list(self.get_process_ids()):
            aProcess = self.get_process(pid)
            if not aProcess.is_alive():
                self._del_process(pid)

    def clear_unattached_processes(self):
        """
        Removes Process objects from the snapshot
        referring to processes not being debugged.
        """
        for pid in list(self.get_process_ids()):
            aProcess = self.get_process(pid)
            if not aProcess.is_being_debugged():
                self._del_process(pid)

    def close_process_handles(self):
        """
        Closes all open handles to processes in this snapshot.
        """
        for pid in list(self.get_process_ids()):
            aProcess = self.get_process(pid)
            try:
                aProcess.close_handle()
            except Exception as e:
                try:
                    msg = "Cannot close process handle %s, reason: %s"
                    msg %= (aProcess.hProcess.value, str(e))
                    warnings.warn(msg)
                except Exception:
                    pass

    def close_process_and_thread_handles(self):
        """
        Closes all open handles to processes and threads in this snapshot.
        """
        for aProcess in list(self.iter_processes()):
            aProcess.close_thread_handles()
            try:
                aProcess.close_handle()
            except Exception as e:
                try:
                    msg = "Cannot close process handle %s, reason: %s"
                    msg %= (aProcess.hProcess.value, str(e))
                    warnings.warn(msg)
                except Exception:
                    pass

    def clear_processes(self):
        """
        Removes all :class:`Process`, :class:`Thread` and :class:`Module` objects in this snapshot.
        """
        # self.close_process_and_thread_handles()
        for aProcess in list(self.iter_processes()):
            aProcess.clear()
        self.__processDict = dict()

    def clear(self):
        """
        Clears this snapshot.

        .. seealso:: :meth:`clear_processes`
        """
        self.clear_processes()

    # ------------------------------------------------------------------------------

    # Docs for these methods are taken from the _ThreadContainer class.

    def has_thread(self, dwThreadId):
        dwProcessId = self.get_pid_from_tid(dwThreadId)
        if dwProcessId is None:
            return False
        return self.has_process(dwProcessId)

    def get_thread(self, dwThreadId):
        dwProcessId = self.get_pid_from_tid(dwThreadId)
        if dwProcessId is None:
            msg = "Unknown thread ID %d" % dwThreadId
            raise KeyError(msg)
        return self.get_process(dwProcessId).get_thread(dwThreadId)

    def get_thread_ids(self):
        ids = list()
        for aProcess in self.iter_processes():
            ids += aProcess.get_thread_ids()
        return ids

    def get_thread_count(self):
        count = 0
        for aProcess in self.iter_processes():
            count += aProcess.get_thread_count()
        return count

    has_thread.__doc__ = _ThreadContainer.has_thread.__doc__
    get_thread.__doc__ = _ThreadContainer.get_thread.__doc__
    get_thread_ids.__doc__ = _ThreadContainer.get_thread_ids.__doc__
    get_thread_count.__doc__ = _ThreadContainer.get_thread_count.__doc__

    # ------------------------------------------------------------------------------

    # Docs for these methods are taken from the _ModuleContainer class.

    def get_module_count(self):
        count = 0
        for aProcess in self.iter_processes():
            count += aProcess.get_module_count()
        return count

    get_module_count.__doc__ = _ModuleContainer.get_module_count.__doc__

    # ------------------------------------------------------------------------------

    def find_modules_by_base(self, lpBaseOfDll):
        """
        :param int lpBaseOfDll: Base address of the module.
        :rtype: list[tuple[Process, Module]]
        :return: List of tuples, each containing a :class:`Process` object and a :class:`Module` object.
        """
        found = list()
        for aProcess in self.iter_processes():
            if aProcess.has_module(lpBaseOfDll):
                aModule = aProcess.get_module(lpBaseOfDll)
                found.append((aProcess, aModule))
        return found

    def find_modules_by_name(self, fileName):
        """
        :param str fileName: Name of the module to search for.
        :rtype: list[tuple[Process, Module]]
        :return: List of tuples, each containing a :class:`Process` object and a :class:`Module` object.
        """
        found = list()
        for aProcess in self.iter_processes():
            aModule = aProcess.get_module_by_name(fileName)
            if aModule is not None:
                found.append((aProcess, aModule))
        return found

    def find_modules_by_address(self, address):
        """
        :param int address: Memory address to search for.
        :rtype: list[tuple[Process, Module]]
        :return: List of tuples, each containing a :class:`Process` object and a :class:`Module` object.
        """
        found = list()
        for aProcess in self.iter_processes():
            aModule = aProcess.get_module_at_address(address)
            if aModule is not None:
                found.append((aProcess, aModule))
        return found

    def __find_processes_by_filename(self, filename):
        """
        Internally used by :meth:`find_processes_by_filename`.
        """
        found = list()
        filename = filename.lower()
        if PathOperations.path_is_absolute(filename):
            for aProcess in self.iter_processes():
                imagename = aProcess.get_filename()
                if imagename and imagename.lower() == filename:
                    found.append((aProcess, imagename))
        else:
            for aProcess in self.iter_processes():
                imagename = aProcess.get_filename()
                if imagename:
                    imagename = PathOperations.pathname_to_filename(imagename)
                    if imagename.lower() == filename:
                        found.append((aProcess, imagename))
        return found

    def find_processes_by_filename(self, fileName):
        """
        :param str fileName: Filename to search for.
            If it's a full pathname, the match must be exact.
            If it's a base filename only, the file part is matched,
            regardless of the directory where it's located.

        :rtype: list[tuple[Process, str]]
        :return: List of processes matching the given main module filename.
            Each tuple contains a :class:`Process` object and its filename.
        """
        found = self.__find_processes_by_filename(fileName)
        if not found:
            fn, ext = PathOperations.split_extension(fileName)
            if not ext:
                fileName = "%s.exe" % fn
                found = self.__find_processes_by_filename(fileName)
        return found

    # ------------------------------------------------------------------------------

    # XXX _notify_* methods should not trigger a scan

    def _add_process(self, aProcess):
        """
        Private method to add a process object to the snapshot.

        :param Process aProcess: Process object.
        """
        dwProcessId = aProcess.dwProcessId
        self.__processDict[dwProcessId] = aProcess

    def _del_process(self, dwProcessId):
        """
        Private method to remove a process object from the snapshot.

        :param int dwProcessId: Global process ID.
        """
        try:
            aProcess = self.__processDict.pop(dwProcessId)
        except KeyError:
            aProcess = None
            msg = "Unknown process ID %d" % dwProcessId
            warnings.warn(msg, RuntimeWarning)
        if aProcess is not None:
            aProcess.clear()  # remove circular references
        return aProcess

    # Notify the creation of a new process.
    def _notify_create_process(self, event):
        """
        Notify the creation of a new process.

        This is done automatically by the :class:`Debug` class, you shouldn't need
        to call it yourself.

        :param CreateProcessEvent event: Create process event.
        :rtype: bool
        :return: ``True`` to call the user-defined handle, ``False`` otherwise.
        """
        dwProcessId = event.get_pid()
        hProcess = event.get_process_handle()
        ##        if not self.has_process(dwProcessId): # XXX this would trigger a scan
        if dwProcessId not in self.__processDict:
            aProcess = Process(dwProcessId, hProcess)
            self._add_process(aProcess)
            aProcess.fileName = event.get_filename()
        else:
            aProcess = self.get_process(dwProcessId)
            # if hProcess != win32.INVALID_HANDLE_VALUE:
            #    aProcess.hProcess = hProcess    # may have more privileges
            if not aProcess.fileName:
                fileName = event.get_filename()
                if fileName:
                    aProcess.fileName = fileName
        return aProcess._notify_create_process(event)  # pass it to the process

    def _notify_exit_process(self, event):
        """
        Notify the termination of a process.

        This is done automatically by the :class:`Debug` class, you shouldn't need
        to call it yourself.

        :param ExitProcessEvent event: Exit process event.
        :rtype: bool
        :return: ``True`` to call the user-defined handle, ``False`` otherwise.
        """
        dwProcessId = event.get_pid()
        ##        if self.has_process(dwProcessId): # XXX this would trigger a scan
        if dwProcessId in self.__processDict:
            self._del_process(dwProcessId)
        return True
