#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

# pyre-strict

import os
import stat
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from . import daemon_util, proc_utils as proc_utils_mod
from .config import EdenInstance
from .util import ShutdownError, poll_until, print_stderr


# The amount of time to wait for the edenfs process to exit after we send SIGKILL.
# We normally expect the process to be killed and reaped fairly quickly in this
# situation.  However, in rare cases on very heavily loaded systems it can take a while
# for init/systemd to wait on the process and for everything to be fully cleaned up.
# Therefore we wait up to 30 seconds by default.  (I've seen it take up to a couple
# minutes on systems with extremely high disk I/O load.)
#
# If this timeout does expire this can cause `edenfsctl restart` to fail after
# killing the old process but without starting the new process, which is
# generally undesirable if we can avoid it.
DEFAULT_SIGKILL_TIMEOUT = 30.0


def wait_for_process_exit(pid: int, timeout: float) -> bool:
    """Wait for the specified process ID to exit.

    Returns True if the process exits within the specified timeout, and False if the
    timeout expires while the process is still alive.
    """
    proc_utils: proc_utils_mod.ProcUtils = proc_utils_mod.new()

    def process_exited() -> Optional[bool]:
        return None if proc_utils.is_process_alive(pid) else True

    try:
        poll_until(process_exited, timeout=timeout)
        return True
    except TimeoutError:
        return False


def wait_for_shutdown(
    pid: int, timeout: float, kill_timeout: float = DEFAULT_SIGKILL_TIMEOUT
) -> bool:
    """Wait for a process to exit.

    If it does not exit within `timeout` seconds kill it with SIGKILL.
    Returns True if the process exited on its own or False if it only exited
    after SIGKILL.

    Throws a ShutdownError if we failed to kill the process with SIGKILL
    (either because we failed to send the signal, or if the process still did
    not exit within kill_timeout seconds after sending SIGKILL).
    """
    # Wait until the process exits on its own.
    if wait_for_process_exit(pid, timeout):
        return True

    # client.shutdown() failed to terminate the process within the specified
    # timeout.  Take a more aggressive approach by sending SIGKILL.
    print_stderr(
        "error: sent shutdown request, but edenfs did not exit "
        "within {} seconds. Attempting SIGKILL.",
        timeout,
    )
    sigkill_process(pid, timeout=kill_timeout)
    return False


def sigkill_process(pid: int, timeout: float = DEFAULT_SIGKILL_TIMEOUT) -> None:
    """Send SIGKILL to a process, and wait for it to exit.

    If timeout is greater than 0, this waits for the process to exit after sending the
    signal.  Throws a ShutdownError exception if the process does not exit within the
    specified timeout.

    Returns successfully if the specified process did not exist in the first place.
    This is done to handle situations where the process exited on its own just before we
    could send SIGKILL.
    """
    proc_utils: proc_utils_mod.ProcUtils = proc_utils_mod.new()
    try:
        proc_utils.kill_process(pid)
    except PermissionError:
        raise ShutdownError(
            "Received a permissions when attempting to kill edenfs. "
            "Perhaps edenfs failed to drop root privileges properly?"
        )

    if timeout <= 0:
        return

    if not wait_for_process_exit(pid, timeout):
        raise ShutdownError(
            f"edenfs process {pid} did not terminate within {timeout} seconds of sending SIGKILL."
        )


async def start_edenfs_service(
    instance: EdenInstance,
    daemon_binary: Optional[str] = None,
    edenfs_args: Optional[List[str]] = None,
) -> int:
    """Start the edenfs daemon."""
    if instance.should_use_experimental_systemd_mode():
        from . import systemd_service

        return await systemd_service.start_systemd_service(
            instance=instance, daemon_binary=daemon_binary, edenfs_args=edenfs_args
        )

    return _start_edenfs_service(
        instance=instance,
        daemon_binary=daemon_binary,
        edenfs_args=edenfs_args,
        takeover=False,
    )


def gracefully_restart_edenfs_service(
    instance: EdenInstance,
    daemon_binary: Optional[str] = None,
    edenfs_args: Optional[List[str]] = None,
) -> int:
    """Gracefully restart the EdenFS service"""
    if instance.should_use_experimental_systemd_mode():
        raise NotImplementedError("TODO(T33122320): Implement 'eden start --takeover'")

    return _start_edenfs_service(
        instance=instance,
        daemon_binary=daemon_binary,
        edenfs_args=edenfs_args,
        takeover=True,
    )


def _start_edenfs_service(
    instance: EdenInstance,
    daemon_binary: Optional[str] = None,
    edenfs_args: Optional[List[str]] = None,
    takeover: bool = False,
) -> int:
    """Get the command and environment to use to start edenfs."""
    daemon_binary = daemon_util.find_daemon_binary(daemon_binary)
    cmd = get_edenfs_cmd(instance, daemon_binary)

    if takeover:
        cmd.append("--takeover")
    if edenfs_args:
        cmd.extend(edenfs_args)

    eden_env = get_edenfs_environment()

    # Wrap the command in sudo, if necessary
    cmd, eden_env = prepare_edenfs_privileges(daemon_binary, cmd, eden_env)

    creation_flags = 0
    if sys.platform == "win32":
        CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        creation_flags = CREATE_NO_WINDOW

    return subprocess.call(
        cmd, stdin=subprocess.DEVNULL, env=eden_env, creationflags=creation_flags
    )


def get_edenfs_cmd(instance: EdenInstance, daemon_binary: str) -> List[str]:
    """Get the command line arguments to use to start the edenfs daemon."""
    return [
        daemon_binary,
        "--edenfs",
        "--edenfsctlPath",
        os.environ.get("EDENFS_CLI_PATH", os.path.abspath(sys.argv[0])),
        "--edenDir",
        str(instance.state_dir),
        "--etcEdenDir",
        str(instance.etc_eden_dir),
        "--configPath",
        str(instance.user_config_path),
    ]


def prepare_edenfs_privileges(
    daemon_binary: str, cmd: List[str], env: Dict[str, str]
) -> Tuple[List[str], Dict[str, str]]:
    """Update the EdenFS command and environment settings in order to run it as root.

    This wraps the command using sudo, if necessary.
    """
    # Nothing to do on Windows
    if sys.platform == "win32":
        return (cmd, env)

    # If we already have root privileges we don't need to do anything.
    if os.geteuid() == 0:
        return (cmd, env)

    # If the EdenFS binary is installed as setuid root we don't need to use sudo.
    s = os.stat(daemon_binary)
    if s.st_uid == 0 and (s.st_mode & stat.S_ISUID):
        return (cmd, env)

    # If we're still here we need to run edenfs under sudo
    sudo_cmd = ["/usr/bin/sudo"]
    # Add environment variable settings
    # Depending on the sudo configuration, these may not
    # necessarily get passed through automatically even when
    # using "sudo -E".
    sudo_cmd.extend(f"{key}={value}" for key, value in env.items())
    cmd = sudo_cmd + cmd
    return cmd, env


def get_edenfs_environment() -> Dict[str, str]:
    """Get the environment to use to start the edenfs daemon."""
    eden_env = {}

    if sys.platform != "win32":
        # Reset $PATH to the following contents, so that everyone has the
        # same consistent settings.
        path_dirs = ["/opt/facebook/hg/bin", "/usr/local/bin", "/bin", "/usr/bin"]

        eden_env["PATH"] = ":".join(path_dirs)
    else:
        # On Windows, copy the existing PATH as it's not clear what locations
        # are needed.
        eden_env["PATH"] = os.environ["PATH"]

    if sys.platform == "darwin":
        # Prevent warning on mac, which will crash eden:
        # +[__NSPlaceholderDate initialize] may have been in progress in
        # another thread when fork() was called.
        eden_env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"

    # Preserve the following environment settings
    preserve = [
        "USER",
        "LOGNAME",
        "HOME",
        "EMAIL",
        "NAME",
        "ASAN_OPTIONS",
        # When we import data from mercurial, the remotefilelog extension
        # may need to SSH to a remote mercurial server to get the file
        # contents.  Preserve SSH environment variables needed to do this.
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "KRB5CCNAME",
        "SANDCASTLE_ALIAS",
        "SANDCASTLE_INSTANCE_ID",
        "SCRATCH_CONFIG_PATH",
        # These environment variables are used by Corp2Prod (C2P) Secure Thrift
        # clients to get the user certificates for authentication. (We use
        # C2P Secure Thrift to fetch metadata from SCS).
        "THRIFT_TLS_CL_CERT_PATH",
        "THRIFT_TLS_CL_KEY_PATH",
        # This helps with rust debugging
        "MISSING_FILES",
        "EDENSCM_LOG",
        "EDENSCM_EDENAPI",
        "RUST_BACKTRACE",
        "RUST_LIB_BACKTRACE",
    ]

    if sys.platform == "win32":
        preserve += [
            "APPDATA",
            "SYSTEMROOT",
            "USERPROFILE",
            "USERNAME",
            "PROGRAMDATA",
            "LOCALAPPDATA",
        ]

    for name, value in os.environ.items():
        # Preserve any environment variable starting with "TESTPILOT_".
        # TestPilot uses a few environment variables to keep track of
        # processes started during test runs, so it can track down and kill
        # runaway processes that weren't cleaned up by the test itself.
        # We want to make sure this behavior works during the eden
        # integration tests.
        # Similarly, we want to preserve EDENFS_ env vars which are
        # populated by our own test infra to relay paths to important
        # build artifacts in our build tree.
        if name.startswith("TESTPILOT_") or name.startswith("EDENFS_"):
            eden_env[name] = value
        elif name in preserve:
            eden_env[name] = value
    return eden_env
