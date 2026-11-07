"""Take one local frame from a monitor shared via the ScreenCast portal."""

import logging
import os
from time import monotonic
from uuid import uuid4

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

from .desktop import CaptureCancelled  # noqa: E402

LOG = logging.getLogger(__name__)
BUS_NAME = "org.freedesktop.portal.Desktop"
DESKTOP_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"
ELEMENTS = ("pipewiresrc", "videoconvert", "pngenc", "appsink")


def check_gstreamer():
    Gst.init(None)
    missing = [name for name in ELEMENTS if not Gst.ElementFactory.find(name)]
    if missing:
        raise RuntimeError("Не найдены элементы GStreamer: " + ", ".join(missing))


def check_portal():
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    (types,) = bus.call_sync(
        BUS_NAME, DESKTOP_PATH, "org.freedesktop.DBus.Properties", "Get",
        GLib.Variant("(ss)", (SCREENCAST, "AvailableSourceTypes")), None,
        Gio.DBusCallFlags.NONE, 5000, None,
    ).unpack()
    if not types & 1:
        raise RuntimeError("Портал ScreenCast не поддерживает захват монитора")


class PortalCapture:
    def __init__(self, context, stop):
        self.context = context
        self.stop = stop
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")

    def close(self, path, interface):
        try:
            self.bus.call_sync(
                BUS_NAME, path, interface, "Close", None, None,
                Gio.DBusCallFlags.NONE, 2000, None,
            )
        except GLib.Error as error:
            LOG.debug("Portal Close: %s", error)

    def request(self, method, signature, *args, timeout=10, **options):
        if self.stop.is_set():
            raise CaptureCancelled
        token = "waylookup_" + uuid4().hex
        options["handle_token"] = GLib.Variant("s", token)
        path = f"{DESKTOP_PATH}/request/{self.sender}/{token}"
        response = None

        def on_response(_bus, _sender, _path, _interface, _signal, parameters):
            nonlocal response
            response = parameters.unpack()

        # Subscribe before calling the method: the response can precede the method reply.
        subscription = self.bus.signal_subscribe(
            BUS_NAME, REQUEST, "Response", path, None, Gio.DBusSignalFlags.NONE, on_response
        )
        try:
            (handle,) = self.bus.call_sync(
                BUS_NAME, DESKTOP_PATH, SCREENCAST, method,
                GLib.Variant(signature, (*args, options)), GLib.VariantType.new("(o)"),
                Gio.DBusCallFlags.NONE, 5000, None,
            ).unpack()
            if handle != path:
                self.close(handle, REQUEST)
                raise RuntimeError("Портал вернул неожиданный адрес запроса")
            deadline = monotonic() + timeout
            while response is None:
                if self.stop.is_set():
                    raise CaptureCancelled
                if monotonic() >= deadline:
                    raise RuntimeError("Истекло время ожидания ответа портала ScreenCast")
                self.context.iteration(False)
                if response is None:
                    self.stop.wait(0.05)
            status, result = response
            if status == 1:
                raise CaptureCancelled
            if status != 0:
                raise RuntimeError(f"Портал ScreenCast отклонил запрос {method}")
            return result
        finally:
            self.bus.signal_unsubscribe(subscription)
            if response is None:
                self.close(path, REQUEST)

    def frame(self, session, node, serial):
        reply, fds = self.bus.call_with_unix_fd_list_sync(
            BUS_NAME, DESKTOP_PATH, SCREENCAST, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (session, {})), GLib.VariantType.new("(h)"),
            Gio.DBusCallFlags.NONE, 5000, None, None,
        )
        fd = fds.get(reply.unpack()[0])
        try:
            return read_frame(fd, node, serial, self.stop)
        finally:
            os.close(fd)


def read_frame(fd, node, serial, stop):
    pipeline = Gst.parse_launch(
        "pipewiresrc name=source ! videoconvert ! video/x-raw,format=RGB ! "
        "pngenc ! appsink name=frame sync=false max-buffers=1"
    )
    source = pipeline.get_by_name("source")
    source.set_property("fd", fd)
    if serial is not None and source.find_property("target-object"):
        source.set_property("target-object", str(serial))
    else:
        source.set_property("path", str(node))
    sink = pipeline.get_by_name("frame")
    bus = pipeline.get_bus()
    try:
        pipeline.set_state(Gst.State.PLAYING)
        deadline = monotonic() + 10
        while not stop.is_set() and monotonic() < deadline:
            sample = sink.emit("try-pull-sample", Gst.SECOND // 5)
            if sample:
                buffer = sample.get_buffer()
                return buffer.extract_dup(0, buffer.get_size())
            message = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message and message.type == Gst.MessageType.ERROR:
                error, _debug = message.parse_error()
                raise RuntimeError(f"PipeWire: {error.message}")
            if message:
                break
        if stop.is_set():
            raise CaptureCancelled
        raise RuntimeError("PipeWire не передал кадр")
    finally:
        pipeline.set_state(Gst.State.NULL)


def capture_screen(stop):
    check_gstreamer()
    context = GLib.MainContext.new()
    context.push_thread_default()
    try:
        portal = PortalCapture(context, stop)
        token = "waylookup_" + uuid4().hex
        result = portal.request("CreateSession", "(a{sv})", session_handle_token=GLib.Variant("s", token))
        session = result["session_handle"]
        try:
            portal.request("SelectSources", "(oa{sv})", session, types=GLib.Variant("u", 1))
            result = portal.request("Start", "(osa{sv})", session, "", timeout=90)
            streams = result.get("streams", [])
            if len(streams) != 1:
                raise RuntimeError("Портал ScreenCast не вернул один монитор")
            node, properties = streams[0]
            position = properties.get("position")
            monitor = None if position is None else dict(zip(("x", "y"), position))
            return portal.frame(session, node, properties.get("pipewire-serial")), monitor
        finally:
            portal.close(session, SESSION)
    finally:
        context.pop_thread_default()
