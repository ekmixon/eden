# Copyright (c) Facebook, Inc. and its affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

from __future__ import absolute_import


def _fixsys():
    """Fix sys.path so core edenscm modules (edenscmnative, and 3rd party
    libraries) are in sys.path

    Fix sys.stdin if it's None.
    """
    import os

    # Do not expose those modules to edenscm.__dict__
    import sys

    dirname = os.path.dirname

    # __file__ is "hg/edenscm/__init__.py"
    # libdir is "hg/"
    # Do not follow symlinks (ex. do not use "realpath"). It breaks buck build.
    libdir = dirname(dirname(os.path.abspath(__file__)))

    # Make "edenscmdeps.zip" available in sys.path. It includes 3rd party
    # pure-Python libraries like IPython, thrift runtime, etc.
    #
    # Note: On Windows, the released version of hg uses python27.zip for all
    # pure Python modules including edenscm and everything in edenscmdeps.zip,
    # so not being able to locate edenscmdeps.zip is not fatal.
    name = "edenscmdeps3.zip" if sys.version_info[0] == 3 else "edenscmdeps.zip"
    for candidate in [libdir, os.path.join(libdir, "build")]:
        depspath = os.path.join(candidate, name)
        if os.path.exists(depspath) and depspath not in sys.path:
            sys.path.insert(0, depspath)

    # Make sure "edenscmnative" can be imported. Error early.
    import edenscmnative

    edenscmnative.__name__

    # stdin can be None if the parent process unset the stdin file descriptor.
    # Replace it early, since it may be read in layer modules, like pycompat.
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r")


_fixsys()


# Keep the module clean
del globals()["_fixsys"]


def run(args=None, fin=None, fout=None, ferr=None):
    import sys

    if args is None:
        args = sys.argv

    # chgserver code path

    # no demandimport, since chgserver wants to preimport everything.
    from .mercurial import dispatch

    if args[1:4] == ["serve", "--cmdserver", "chgunix2"]:
        dispatch.runchgserver()
    else:
        # non-chgserver code path
        # - no chg in use: hgcommands::run -> HgPython::run_hg -> here
        # - chg client: chgserver.runcommand -> bindings.commands.run ->
        #               hgcommands::run -> HgPython::run_hg -> here

        from . import traceimport

        traceimport.enable()

        # enable demandimport after enabling traceimport
        from . import hgdemandimport

        hgdemandimport.enable()

        dispatch.run(args, fin, fout, ferr)
