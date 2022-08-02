#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

# pyre-strict

import configparser
from typing import Dict, Mapping, MutableMapping


class EdenConfigInterpolator(configparser.Interpolation):
    """Python provides a couple of interpolation options but neither
    of them quite match the simplicity that we want.  This class
    will interpolate the keys of the provided map and replace
    those tokens with the values from the map.  There is no
    recursion or referencing of values from other sections of
    the config.
    Limiting the scope interpolation makes it easier to replicate
    this approach in the C++ implementation of the parser.
    """

    def __init__(self, defaults: Dict[str, str]) -> None:
        """ pre-construct the token name that we're going to substitute.
            eg: {"foo": "bar"} is stored as {"${foo}": "bar"} internally
        """
        self._defaults: Dict[str, str] = {
            "${" + k + "}": v for k, v in defaults.items()
        }

    def _interpolate(self, value: str) -> str:
        """simple brute force replacement using the defaults that were
        provided to us during construction"""
        for k, v in self._defaults.items():
            value = value.replace(k, v)
        return value

    def before_get(
        self,
        parser: MutableMapping[str, Mapping[str, str]],
        section: str,
        option: str,
        value: str,
        defaults: Mapping[str, str],
    ) -> str:
        return self._interpolate(value)

    def before_read(
        self,
        parser: MutableMapping[str, Mapping[str, str]],
        section: str,
        option: str,
        value: str,
    ) -> str:
        return self._interpolate(value)
