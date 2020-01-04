# ovirt-imageio
# Copyright (C) 2019 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
"""
http - HTTP backend.
"""

from __future__ import absolute_import

import json
import logging
import os
import socket
import ssl

# TODO: needed since we run common tests with python 2.
from six.moves import http_client

from .. import errors
from . import image

log = logging.getLogger("backends.http")


def open(url, mode, sparse=True, dirty=False, buffer_size=None, cafile=None,
         secure=True):
    """
    Open a HTTP backend.

    Arguments:
        url (url): parsed HTTPS URL.
        mode (str): ignored, http backend is always read-write.
        sparse (bool): ignored, http backend does not support sparseness.
        dirty (bool): ignored, http backend does not require configuration for
            getting dirty extents.
        buffer_size (int): ignored, not used by http backend.
        cafile (str): path to CA certificates to trust for certificate
            verification. If not set, trust system's default CA certificates
            instead.
        secure (bool): If True, verify server certificate.
    """
    assert url.scheme == "https"
    return Backend(url, cafile, secure=secure)


class Backend(object):

    def __init__(self, url, cafile, secure=True):
        log.debug("Open backend netloc=%s path=%s cafile=%s secure=%s",
                  url.netloc, url.path, cafile, secure)
        self.url = url
        self._position = 0
        self._size = None
        self._extents = {}

        self._con = self._create_connection(cafile, secure)
        try:
            options = self._options()
            log.debug("Server options: %s", options)
            self._can_extents = options.get("extents", False)
            self._can_zero = options.get("zero", False)
            self._can_flush = options.get("flush", False)

            self._optimize_connection(options.get("unix_socket"))
        except Exception:
            self._con.close()
            raise

    def readinto(self, buf):
        """
        Send GET request, reading bytes at current position into buf.
        """
        length = len(buf)
        res = self._get(length)
        self._read_all(res, buf)

        self._position += length
        return length

    def write(self, buf):
        """
        Send PUT request, writing buf contents at current position.
        """
        length = len(buf)
        self._put_header(length)
        self._con.send(buf)
        res = self._con.getresponse()

        if res.status != http_client.OK:
            error = res.read(512)
            raise RuntimeError(
                "Error PUT offset={} length={}: {}"
                .format(self._position, length, error))

        res.read()
        self._position += length
        return length

    def zero(self, length):
        """
        Send PATCH/zero request, writing zeroes at current position.
        """
        msg = {
            "op": "zero",
            "offset": self._position,
            "size": length,
            "flush": not self._can_flush
        }
        self._patch(msg)

        self._position += length
        return length

    def flush(self):
        """
        Send a PATCH/flush request, flushing changes to storage.
        """
        self._patch({"op": "flush"})

    def extents(self, context="zero"):
        """
        Get image extents, return iterator over received extents.
        """
        if context not in ("zero", "dirty"):
            raise RuntimeError("Invalid context: {}".format(context))

        if not self._can_extents:
            if context == "zero":
                yield image.ZeroExtent(0, self.size(), False)
                return
            else:
                raise errors.UnsupportedOperation(
                    "Server does not support dirty extents")

        if context not in self._extents:
            self._extents[context] = list(self._get_extents(context))

        for ext in self._extents[context]:
            yield ext

    def tell(self):
        return self._position

    def seek(self, n, how=os.SEEK_SET):
        if how == os.SEEK_SET:
            self._position = n
        elif how == os.SEEK_CUR:
            self._position += n
        elif how == os.SEEK_END:
            self._position = self.size() + n
        return self._position

    def size(self):
        # We have 2 bad options:
        # - Get last extent, may be slow, and may not be neded otherwise.
        # - Emulate HEAD request, logging tracebacks in the remote server.
        # Getting extents is more polite, so lets use it if we can.
        if self._size is None:
            if self._can_extents:
                last = list(self.extents())[-1]
                self._size = last.start + last.length
            else:
                self._size = self._emulate_head()

        return self._size

    def close(self):
        log.debug("Close backend netloc=%s path=%s",
                  self.url.netloc, self.url.path)
        self._con.close()

    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        try:
            self.close()
        except Exception:
            # Do not hide the original error.
            if t is None:
                raise
            log.exception("Error closing backend")

    # Debugging interface

    @property
    def server_address(self):
        return self._con.server_address

    # Private

    def _create_connection(self, cafile, secure):
        context = ssl.create_default_context(
            purpose=ssl.Purpose.SERVER_AUTH, cafile=cafile)

        if not secure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        return HTTPSConnection(self.url.netloc, context=context)

    def _optimize_connection(self, unix_socket):
        """
        Try to switch to Unix socket for improved performane. If we fail to
        switch continue to use HTTPS.
        """
        if not (self._con.is_local() and unix_socket):
            return

        try:
            con = UnixHTTPConnection(unix_socket)
            try:
                con.connect()
            except Exception:
                con.close()
                raise
        except Exception as e:
            log.warning("Cannot use unix socket: %s", e)
        else:
            log.debug("Using unix socket: %r", unix_socket)
            self._con.close()
            self._con = con

    def _get(self, length):
        headers = {}
        headers["range"] = "bytes={}-{}".format(
            self._position, self._position + length - 1)

        self._con.request("GET", self.url.path, headers=headers)
        res = self._con.getresponse()

        if res.status != http_client.PARTIAL_CONTENT:
            error = res.read(512)
            raise RuntimeError(
                "Error GET offset={} length={}: {}"
                .format(self._position, length, error))

        content_length = int(res.getheader("content-length"))
        if content_length != length:
            raise RuntimeError(
                "Unexpected content_length={} expected={}"
                .format(content_length, length))

        return res

    def _put_header(self, length):
        path = self.url.path
        if self._can_flush:
            path += "?flush=n"

        self._con.putrequest("PUT", path)

        self._con.putheader("content-length", length)
        self._con.putheader("content-type", "application/octet-stream")
        self._con.putheader("content-range", "bytes {}-{}/*".format(
                self._position, self._position + length - 1))

        self._con.endheaders()

    def _patch(self, msg):
        body = json.dumps(msg).encode("utf-8")
        headers = {"content-type": "application/json"}

        self._con.request("PATCH", self.url.path, body=body, headers=headers)
        res = self._con.getresponse()

        if res.status != http_client.OK:
            error = res.read(512)
            raise RuntimeError("Error PATCH msg={}: {}" .format(msg, error))

        res.read()

    def _options(self):
        self._con.request("OPTIONS", self.url.path)
        res = self._con.getresponse()
        body = res.read()

        options = {}

        if res.status == http_client.METHOD_NOT_ALLOWED:
            # Older daemon did not implement OPTIONS
            return options
        elif res.status == http_client.NO_CONTENT:
            # Older proxy did implement OPTIONS but does not return any
            # content.
            return options
        elif res.status != http_client.OK:
            raise RuntimeError("Error OPTIONS: {}".format(body))

        # New daemon or proxy provides options dict.
        try:
            options = json.loads(body.decode("utf-8"))
        except ValueError:
            # Bad response, we must assume we don't support any features or
            # unix socket.
            return options

        # Flaten features into options dict to make it easier to consume.  If
        # we get invalid response without feature list, assume the server does
        # not support any feature.
        for feature in options.pop("features", []):
            options[feature] = True

        return options

    def _get_extents(self, context):
        self._con.request("GET", self.url.path + "/extents?context=" + context)
        res = self._con.getresponse()
        data = res.read()

        if res.status == http_client.NOT_FOUND:
            raise errors.UnsupportedOperation(
                "Server does not support {} extents: {}"
                .format(context, data[:512]))

        if res.status != http_client.OK:
            raise RuntimeError("Error EXTENTS: {}".format(data[:512]))

        extents = json.loads(data.decode("utf-8"))

        cls = image.ZeroExtent if context == "zero" else image.DirtyExtent
        for ext in extents:
            yield cls(ext["start"], ext["length"], ext[context])

    def _emulate_head(self):
        """
        Emulate HEAD request by sending GET and closing the connction without
        reading anything. This is not very polite, but we don't have another
        choice if the server does not support extents.

        NOTE: Logs noisy tracebacks in the daemon logs.
        """
        self._con.request("GET", self.url.path)
        res = self._con.getresponse()

        if res.status != http_client.OK:
            error = res.read(512)
            raise RuntimeError("Error GET: {}".format(error))

        size = int(res.getheader("content-length"))

        # The connection will automaticlaly reconnect on the next request.
        self._con.close()

        return size

    def _read_all(self, res, buf):
        with memoryview(buf) as view:
            length = len(view)
            pos = 0
            while pos < length:
                n = res.readinto(view[pos:])
                if n == 0:
                    raise RuntimeError(
                        "Expected {} byes, got {} bytes".format(length, pos))
                pos += n


class HTTPSConnection(http_client.HTTPSConnection):
    """
    Enhanced HTTPS connection.
    """

    def is_local(self):
        """
        Return True if connected to the local host.
        """
        # Hack for daemon versions 1.4.0 and 1.4.1 that supported unix
        # socket but not keep alive connections. With these versions the
        # socket is closed after calling getresponse().
        if self.sock is None:
            self.connect()

        return self.sock.getsockname()[0] == self.sock.getpeername()[0]

    @property
    def server_address(self):
        return self.sock.getpeername()


class UnixHTTPConnection(http_client.HTTPConnection):
    """
    HTTP connection over unix domain socket.
    """

    def __init__(self, path, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        self.path = path
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)

    @property
    def server_address(self):
        return self.path
