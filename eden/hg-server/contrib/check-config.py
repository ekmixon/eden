#!/usr/bin/env python
# Portions Copyright (c) Facebook, Inc. and its affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

# Copyright 2015 Matt Mackall <mpm@selenic.com>
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

# check-config - a config flag documentation checker for Mercurial

from __future__ import absolute_import, print_function

import re
import sys


foundopts = {}
documented = {}
allowinconsistent = set()

configre = re.compile(
    r"""
    # Function call
    ui\.config(?P<ctype>|int|bool|list)\(
        # First argument.
        ['"](?P<section>\S+)['"],\s*
        # Second argument
        ['"](?P<option>\S+)['"](,\s+
        (?:default=)?(?P<default>\S+?))?
    \)""",
    re.VERBOSE | re.MULTILINE,
)

configwithre = re.compile(
    """
    ui\.config(?P<ctype>with)\(
        # First argument is callback function. This doesn't parse robustly
        # if it is e.g. a function call.
        [^,]+,\s*
        ['"](?P<section>\S+)['"],\s*
        ['"](?P<option>\S+)['"](,\s+
        (?:default=)?(?P<default>\S+?))?
    \)""",
    re.VERBOSE | re.MULTILINE,
)

configpartialre = r"""ui\.config"""

ignorere = re.compile(
    r"""
    \#\s(?P<reason>internal|experimental|deprecated|developer|inconsistent)\s
    config:\s(?P<config>\S+\.\S+)$
    """,
    re.VERBOSE | re.MULTILINE,
)


def main(args):
    for f in args:
        sect = ""
        prevname = ""
        confsect = ""
        carryover = ""
        linenum = 0
        for l in open(f):
            linenum += 1

            if m := re.match("\s*``(\S+)``", l):
                prevname = m[1]
            if re.match("^\s*-+$", l):
                sect = prevname
                prevname = ""

            if sect and prevname:
                name = f"{sect}.{prevname}"
                documented[name] = 1

            if m := re.match(r"^\s+\[(\S+)\]", l):
                confsect = m[1]
                continue
            if m := re.match(r"^\s+(?:#\s*)?(\S+) = ", l):
                name = f"{confsect}." + m[1]
                documented[name] = 1

            if m := re.match(r"^\s*(\S+\.\S+)$", l):
                documented[m[1]] = 1

            if m := re.match(r"^\s*:(\S+\.\S+):\s+", l):
                documented[m[1]] = 1

            if m := re.match(r".*?``(\S+\.\S+)``", l):
                documented[m[1]] = 1

            if m := ignorere.search(l):
                if m.group("reason") == "inconsistent":
                    allowinconsistent.add(m.group("config"))
                else:
                    documented[m.group("config")] = 1

            # look for code-like bits
            line = carryover + l
            if m := configre.search(line) or configwithre.search(line):
                ctype = m.group("ctype")
                if not ctype:
                    ctype = "str"
                name = m.group("section") + "." + m.group("option")
                default = m.group("default")
                if default in (None, "False", "None", "0", "[]", '""', "''"):
                    default = ""
                if re.match("[a-z.]+$", default):
                    default = "<variable>"
                if (
                    name in foundopts
                    and (ctype, default) != foundopts[name]
                    and name not in allowinconsistent
                ):
                    print(l.rstrip())
                    print(
                        "conflict on %s: %r != %r"
                        % (name, (ctype, default), foundopts[name])
                    )
                    print("at %s:%d:" % (f, linenum))
                foundopts[name] = (ctype, default)
                carryover = ""
            else:
                carryover = line if (m := re.search(configpartialre, line)) else ""
    for name in sorted(foundopts):
        if name not in documented and not (
            name.startswith("devel.")
            or name.startswith("experimental.")
            or name.startswith("debug.")
        ):
            ctype, default = foundopts[name]
            if default:
                default = f" [{default}]"
                # config name starting with "_" are considered as internal.
            if "._" not in name:
                print(f"undocumented: {name} ({ctype}){default}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(main(sys.argv[1:]))
    else:
        sys.exit(main([l.rstrip() for l in sys.stdin]))
