#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

import os.path
import tempfile
import unittest

from eden.thrift.client import EdenNotRunningError, create_thrift_client


class EdenClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_raise_EdenNotRunningError_when_no_socket(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sockname = os.path.join(td, "sock")
            with self.assertRaises(EdenNotRunningError) as cm:
                async with create_thrift_client(socket_path=sockname):
                    pass
            ex = cm.exception
            self.assertEqual(sockname, ex.socket_path)
            self.assertEqual(
                f"edenfs daemon does not appear to be running: tried {sockname}",
                str(ex),
            )
