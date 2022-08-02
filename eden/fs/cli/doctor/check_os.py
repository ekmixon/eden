#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

import platform
import re
from typing import Tuple

from eden.fs.cli import ui
from eden.fs.cli.config import EdenInstance
from eden.fs.cli.doctor.problem import Problem, ProblemTracker


class OSProblem(Problem):
    pass


def _parse_os_kernel_version(version: str) -> Tuple[int, ...]:
    """Parses kernel version string.
    Example version string: 4.11.3-67_fbk17_4093_g2bf19e7a0b95
    Returns integer representations of the version, eg. (4, 11, 3, 67).
    """
    version = re.sub(r"[_-]", ".", version)
    split_version = version.split(".")[:4]
    parsed_kernel_version = tuple(map(int, split_version))
    if len(parsed_kernel_version) < 4:
        # right pad with zeros if the kernel version isn't 4 numbers
        parsed_kernel_version = (
            *parsed_kernel_version,
            *[0] * (4 - len(parsed_kernel_version)),
        )
    # pyre-fixme[7]: Expected `Tuple[int, ...]` but got `Tuple[object, ...]`.
    return parsed_kernel_version


def _os_is_kernel_version_too_old(instance: EdenInstance, release: str) -> bool:
    min_kernel_version = instance.get_config_value(
        "doctor.minimum-kernel-version", default=""
    )
    if not min_kernel_version:
        return False
    try:
        return _parse_os_kernel_version(release) < _parse_os_kernel_version(
            min_kernel_version
        )
    except ValueError:
        # If the kernel version failed to parse because one of the
        # components wasn't an int, whatever.
        return False


def _os_is_bad_release(instance: EdenInstance, release: str) -> bool:
    known_bad_kernel_versions = instance.get_config_value(
        "doctor.known-bad-kernel-versions", default=""
    )
    return (
        any(
            re.search(regex, release)
            for regex in known_bad_kernel_versions.split(",")
        )
        if known_bad_kernel_versions
        else False
    )


def run_operating_system_checks(
    tracker: ProblemTracker, instance: EdenInstance, out: ui.Output
) -> None:
    if platform.system() != "Linux":
        return

    # get kernel version string; same as "uname -r"
    current_kernel_release = platform.release()

    if result := _os_is_kernel_version_too_old(
        instance, current_kernel_release
    ):
        tracker.add_problem(
            OSProblem(
                description=f"Kernel version {current_kernel_release} too low.",
                remediation="Reboot to upgrade kernel version.",
            )
        )

        # if the kernel version is too low, return here as continuing to
        # further checks has no benefit
        return

    if result := _os_is_bad_release(instance, current_kernel_release):
        tracker.add_problem(
            OSProblem(
                # TODO: Reword these messages prior to public release
                description=f"Kernel {current_kernel_release} is a known "
                + "bad kernel.",
                remediation="Reboot to upgrade kernel version.",
            )
        )
        return
