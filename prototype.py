"""
Kompanion — Digital Prototype
==================================
Run:  python prototype.py

No external broker or hardware required.  An in-process message bus
simulates MQTT so both devices talk to each other instantly.
All constants (topics, pH thresholds) mirror companion_device.py so the
same logic will work on real Raspberry Pis with a real broker.
"""

import threading
import time
import datetime
import tkinter as tk
from tkinter import ttk, scrolledtext

# ─── Constants (identical to companion_device.py) ────────────────────────────

PH_SAFE_LOW      = 4.0
PH_SAFE_HIGH     = 8.0
PRESENCE_TIMEOUT = 10.0     # seconds before a friend is considered gone

TOPIC_PRESENCE        = "kompanion/{id}/presence"
TOPIC_PRESENCE_BINARY = "kompanion/{id}/presence_binary"
TOPIC_HEALTH          = "kompanion/{id}/health"
TOPIC_FEED            = "kompanion/{id}/feed"

# ─── Palette ─────────────────────────────────────────────────────────────

C_BG       = "#111827"
C_PANEL    = "#1f2937"
C_BORDER   = "#374151"
C_TEXT     = "#f3f4f6"
C_DIM      = "#6b7280"

C_LED_OFF      = "#1f2937"
C_LED_AMBER    = "#f59e0b"
C_GLOW_AMBER   = "#fde68a"
C_LED_RED      = "#ef4444"
C_GLOW_RED     = "#fca5a5"

C_HEALTHY  = "#10b981"
C_SICK     = "#f97316"

C_LOG_A    = "#93c5fd"   # Device A log entries (blue)
C_LOG_B    = "#fda4af"   # Device B log entries (rose)
C_LOG_BUS  = "#4b5563"   # raw MQTT bus messages (dimmed)
C_LOG_SYS  = "#a3e635"   # system messages (lime)

# ─── In-Process Message Bus (simulates MQTT broker) ──────────────────────────

class _Msg:
    """Minimal stand-in for a paho MQTT message object."""
    def __init__(self, topic: str, payload: bytes):
        self.topic   = topic
        self.payload = payload if isinstance(payload, bytes) else str(payload).encode()


class InProcessBus:
    """
    Synchronous pub/sub bus that supports the MQTT '+' single-level wildcard.
    On real hardware this is replaced by a live MQTT broker (mosquitto, etc.).
    """

    def __init__(self):
        self._subs: list[tuple[str, callable]] = []
        self._lock  = threading.Lock()
        self._log_cb = None

    def set_log(self, cb: callable):
        self._log_cb = cb

    def subscribe(self, pattern: str, cb: callable):
        with self._lock:
            self._subs.append((pattern, cb))

    def publish(self, topic: str, payload, **_):
        if self._log_cb:
            self._log_cb(f"[BUS] {topic}  {payload!r}", "bus")
        with self._lock:
            matched = [(p, cb) for p, cb in self._subs if self._match(p, topic)]
        for _, cb in matched:
            try:
                cb(_Msg(topic, payload))
            except Exception as exc:
                print(f"[BUS ERROR] subscriber raised: {exc}")

    @staticmethod
    def _match(pattern: str, topic: str) -> bool:
        pp, tp = pattern.split("/"), topic.split("/")
        if len(pp) != len(tp):
            return False
        return all(p == "+" or p == t for p, t in zip(pp, tp))


# ─── Mock MQTT Client (drop-in for paho.mqtt.client) ─────────────────────────

class MockMQTTClient:
    """
    Implements the paho.mqtt.client surface used by CompanionDevice.
    Routes through InProcessBus instead of a network socket.
    Swap this out for a real paho.Client to go live.
    """

    def __init__(self, client_id: str, bus: InProcessBus):
        self.client_id  = client_id
        self._bus       = bus
        self.on_connect = None
        self.on_message = None

    def connect(self, broker, port=1883, keepalive=60):
        if self.on_connect:
            self.on_connect(self, None, {}, 0)

    def subscribe(self, topic_pattern, qos=0):
        self._bus.subscribe(topic_pattern, self._dispatch)

    def publish(self, topic, payload=b"", qos=0, retain=False):
        self._bus.publish(topic, payload, qos=qos, retain=retain)

    def loop_forever(self):
        pass 

    def _dispatch(self, msg: _Msg):
        if self.on_message:
            self.on_message(self, None, msg)


# ─── Hardware (replaces FakeHardware / real Hardware) ────────────────────

class Hardware:

    def __init__(self):
        self._presence   = False
        self._ph         = 7.0
        self._btn_cb     = None
        self.led_state   = "off"      # "off" | "amber" | "red"
        self.on_led_change = None     # fn(state: str)  — set by panel
        self.on_dispense   = None     # fn()            — set by panel
        self.log_cb        = None     # fn(msg: str, tag: str)

    # ── sensor reads ─────────────────────────────────────────────────────────
    def read_presence(self): return self._presence
    def read_ph(self):       return self._ph

    # ── actuator writes ───────────────────────────────────────────────────────
    def set_led_amber(self):
        self.led_state = "amber"
        self._fire_led("amber")
        self._log("LED → amber (presence sync)")

    def set_led_red(self):
        self.led_state = "red"
        self._fire_led("red")
        self._log("LED → red (SCOBY SOS)")

    def clear_led(self):
        self.led_state = "off"
        self._fire_led("off")
        self._log("LED → off")

    def wait_for_tap(self, cb):
        self._btn_cb = cb

    def trigger_tap(self):
        if self._btn_cb:
            self._btn_cb()

    def _fire_led(self, state):
        if self.on_led_change:
            self.on_led_change(state)

    def _log(self, msg):
        if self.log_cb:
            self.log_cb(msg, "hw")


# ─── Companion Device (mirrors companion_device.py logic exactly) ─────────────

class CompanionDevice:
    """
    Hardware abstraction layer controlled by the GUI instead of GPIO pins.

    The GUI panel sets on_led_change / on_dispense callbacks after creating
    this object.  CompanionDevice calls the same read_*/set_led_*/wait_for_tap
    interface it would use on a real Pi.
    """

    def __init__(self, device_id: str, friend_id: str,
                 hw: Hardware, client: MockMQTTClient):
        self.id        = device_id
        self.friend_id = friend_id
        self.hw        = hw
        self.client    = client

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

        # local state
        self.present                  = False
        self.friend_present           = False
        self.friend_unhealthy         = False
        self.friend_last_presence_ts  = 0.0
        self.friend_binary_seen       = False

        # optional external log hook (set by app)
        self.log_cb = None
        # Guard: suppress stale-pH publishes while dispenser recovery is running
        self._recovering = False

    # ── startup ──────────────────────────────────────────────────────────────

    def start(self):
        self.client.connect("in-process")
        threading.Thread(target=self._sensor_loop, daemon=True).start()
        self.hw.wait_for_tap(self._on_button)

    # ── MQTT callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        self._log("connected to broker")
        client.subscribe("kompanion/+/+")

    def _on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        if len(parts) != 3:
            return
        _, peer, kind = parts
        if peer == self.id:
            return

        payload = msg.payload

        if kind == "presence":
            try:
                ts = float(payload.decode())
            except Exception:
                ts = 0.0
            self.friend_last_presence_ts = ts

        elif kind == "presence_binary":
            self.friend_present     = (payload == b"1")
            self.friend_binary_seen = True
            self._log(f"friend {'present' if self.friend_present else 'away'}")

        elif kind == "health":
            try:
                ph = float(payload.decode())
            except ValueError:
                return
            was_unhealthy = self.friend_unhealthy
            self.friend_unhealthy = not (PH_SAFE_LOW <= ph <= PH_SAFE_HIGH)
            if self.friend_unhealthy and not was_unhealthy:
                self._log(f"FRIEND SOS — SCOBY pH {ph:.2f} out of range!")
            elif not self.friend_unhealthy and was_unhealthy:
                self._log(f"Friend SCOBY recovered (pH {ph:.2f})")

        elif kind == "feed":
            self._log(f"feed command received from {peer} — dispensing sugar")
            self._on_feed_received()

        self._update_led()

    # ── sensor loop (runs every 2 s in prototype) ────────────

    def _sensor_loop(self):
        while True:
            prev = self.present
            self.present = self.hw.read_presence()
            if self.present != prev:
                ts_payload  = str(time.time()).encode() if self.present else b"0"
                bin_payload = b"1" if self.present else b"0"
                self._publish(TOPIC_PRESENCE.format(id=self.id), ts_payload)
                self._publish(TOPIC_PRESENCE_BINARY.format(id=self.id), bin_payload)

            if not self._recovering:
                ph = self.hw.read_ph()
                self._publish(TOPIC_HEALTH.format(id=self.id), str(ph).encode())
            self._update_led()
            time.sleep(2)

    # ── user button ───────────────────────────────────────────────────────────

    def _on_button(self):
        if self.friend_unhealthy:
            self._log(f"tapping — sending feed to {self.friend_id}")
            self._publish(TOPIC_FEED.format(id=self.friend_id), b"")

    # ── feed received: activate dispenser, then simulate pH recovery ──────────

    def _on_feed_received(self):
        # Immediately reset pH and publish so the friend's SOS clears at once.
        # (Mirrors the original companion_device.py dispense_sugar() behaviour.)
        recovered_ph = (PH_SAFE_LOW + PH_SAFE_HIGH) / 2   # 6.0
        self.hw._ph  = recovered_ph
        self._recovering = True   # block sensor loop from re-publishing stale pH
        self._log(f"dispensing sugar — SCOBY pH resetting to {recovered_ph:.1f}")
        self._publish(TOPIC_HEALTH.format(id=self.id), str(recovered_ph).encode())
        self._update_led()
        if self.hw.on_dispense:
            self.hw.on_dispense()

        def _finish():
            time.sleep(2.5)        # dispenser animation
            self._recovering = False
            self._log("dispenser sequence complete")
        threading.Thread(target=_finish, daemon=True).start()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _update_led(self):
        if not self.friend_binary_seen and self.friend_last_presence_ts:
            self.friend_present = (
                (time.time() - self.friend_last_presence_ts) < PRESENCE_TIMEOUT
            )
        if self.friend_unhealthy:
            self.hw.set_led_red()
        elif self.present and self.friend_present:
            self.hw.set_led_amber()
        else:
            self.hw.clear_led()

    def _publish(self, topic, payload):
        self.client.publish(topic, payload, qos=1, retain=True)

    def _log(self, msg):
        if self.log_cb:
            tag = "a" if self.id == "A" else "b"
            self.log_cb(f"[Device {self.id}] {msg}", tag)


# ─── Panel (one per device) ───────────────────────────────────────────────

class DevicePanel(tk.Frame):

    def __init__(self, parent, device: CompanionDevice, root: tk.Tk, **kw):
        super().__init__(parent, bg=C_PANEL, **kw)
        self.device   = device
        self.root     = root
        self._syncing = False
        self._ph_timer = None

        # Wire hardware → callbacks (thread-safe via root.after)
        hw = device.hw
        hw.on_led_change = lambda s: root.after(0, self._on_led_change, s)
        hw.on_dispense   = lambda: root.after(0, self._show_dispenser)

        self._build_ui()
        self._on_led_change("off")     # set initial state

    # ── layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        lbl    = self.device.id
        friend = "B" if lbl == "A" else "A"

        # header
        hdr = tk.Frame(self, bg=C_BORDER)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text=f"  Device {lbl}  |  User {lbl}",
                 font=("Helvetica", 13, "bold"),
                 bg=C_BORDER, fg=C_TEXT, pady=8).pack(side=tk.LEFT)

        # LED
        led_fr = tk.Frame(self, bg=C_PANEL)
        led_fr.pack(pady=12)
        self._cv   = tk.Canvas(led_fr, width=130, height=130,
                               bg=C_PANEL, highlightthickness=0)
        self._cv.pack()
        self._glow = self._cv.create_oval(8, 8, 122, 122,
                                          fill=C_LED_OFF, outline="")
        self._led  = self._cv.create_oval(22, 22, 108, 108,
                                          fill=C_LED_OFF,
                                          outline=C_BORDER, width=2)
        self._led_lbl = tk.Label(led_fr, text="OFF",
                                 font=("Courier", 10, "bold"),
                                 bg=C_PANEL, fg=C_DIM)
        self._led_lbl.pack(pady=3)

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=8, pady=4)

        ctrl = tk.Frame(self, bg=C_PANEL)
        ctrl.pack(fill=tk.X, padx=14)

        # presence toggle
        pr = tk.Frame(ctrl, bg=C_PANEL)
        pr.pack(fill=tk.X, pady=5)
        tk.Label(pr, text="Proximity:", width=12, anchor="w",
                 bg=C_PANEL, fg=C_TEXT, font=("Helvetica", 10)).pack(side=tk.LEFT)
        self._pres_var = tk.BooleanVar(value=False)
        self._pres_btn = tk.Button(pr, text="AWAY", width=9,
                                   command=self._toggle_presence,
                                   bg="#1a2030", fg=C_DIM,
                                   relief=tk.FLAT,
                                   font=("Helvetica", 9, "bold"),
                                   cursor="hand2", pady=3)
        self._pres_btn.pack(side=tk.LEFT)

        # pH slider
        ph_row = tk.Frame(ctrl, bg=C_PANEL)
        ph_row.pack(fill=tk.X, pady=4)
        tk.Label(ph_row, text="pH Sensor:", width=12, anchor="w",
                 bg=C_PANEL, fg=C_TEXT, font=("Helvetica", 10)).pack(side=tk.LEFT)
        self._ph_var = tk.DoubleVar(value=self.device.hw._ph)
        ttk.Scale(ph_row, from_=1.0, to=14.0,
                  variable=self._ph_var, orient=tk.HORIZONTAL,
                  command=self._ph_moved).pack(side=tk.LEFT, fill=tk.X, expand=True)

        rng = tk.Frame(ctrl, bg=C_PANEL)
        rng.pack(fill=tk.X)
        tk.Label(rng, text="pH 1", bg=C_PANEL, fg=C_DIM,
                 font=("Helvetica", 7)).pack(side=tk.LEFT)
        tk.Label(rng, text=f"safe: {PH_SAFE_LOW}–{PH_SAFE_HIGH}",
                 bg=C_PANEL, fg=C_DIM, font=("Helvetica", 7)).pack(side=tk.LEFT, expand=True)
        tk.Label(rng, text="pH 14", bg=C_PANEL, fg=C_DIM,
                 font=("Helvetica", 7)).pack(side=tk.RIGHT)

        self._ph_lbl = tk.Label(ctrl, font=("Courier", 10, "bold"),
                                bg=C_PANEL, fg=C_HEALTHY)
        self._ph_lbl.pack(anchor="w", pady=3)
        self._refresh_ph_label(self.device.hw._ph)

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=8, pady=4)

        # dispenser status
        self._disp_lbl = tk.Label(self, text="",
                                  font=("Helvetica", 9, "bold"),
                                  bg=C_PANEL, fg=C_LED_AMBER)
        self._disp_lbl.pack(pady=2)

        # tap-to-feed button
        self._tap_btn = tk.Button(
            self,
            text=f"TAP TO FEED\nDevice {friend}'s SCOBY",
            command=self._tap,
            bg="#1c0000", fg=C_DIM,
            relief=tk.FLAT, font=("Helvetica", 10, "bold"),
            pady=10, padx=14, state=tk.DISABLED, cursor="hand2")
        self._tap_btn.pack(fill=tk.X, padx=14, pady=8)

        self._status_lbl = tk.Label(self, text="Idle",
                                    font=("Helvetica", 9), wraplength=230,
                                    bg=C_PANEL, fg=C_DIM)
        self._status_lbl.pack(pady=(0, 10))

    # ── user controls ─────────────────────────────────────────────────────────

    def _toggle_presence(self):
        new = not self._pres_var.get()
        self._pres_var.set(new)
        self.device.hw._presence = new
        self.device.present = new
        ts  = str(time.time()).encode() if new else b"0"
        bin = b"1" if new else b"0"
        self.device._publish(TOPIC_PRESENCE.format(id=self.device.id), ts)
        self.device._publish(TOPIC_PRESENCE_BINARY.format(id=self.device.id), bin)
        if new:
            self._pres_btn.config(text="PRESENT", bg="#052e16", fg="#34d399")
        else:
            self._pres_btn.config(text="AWAY", bg="#1a2030", fg=C_DIM)

    def _ph_moved(self, _=None):
        if self._syncing:
            return
        self._refresh_ph_label(self._ph_var.get())
        if self._ph_timer:
            self.root.after_cancel(self._ph_timer)
        self._ph_timer = self.root.after(120, self._apply_ph)

    def _apply_ph(self):
        ph = self._ph_var.get()
        self.device.hw._ph = ph
        # Publish immediately so friend sees without waiting for sensor loop
        self.device._publish(TOPIC_HEALTH.format(id=self.device.id),
                             str(ph).encode())

    def _tap(self):
        self.device.hw.trigger_tap()

    # ── hardware callbacks (always scheduled on main thread) ──────────────────

    def _on_led_change(self, state: str):
        configs = {
            "off":   (C_LED_OFF,    C_LED_OFF,    "OFF",           C_DIM),
            "amber": (C_GLOW_AMBER, C_LED_AMBER,  "PRESENCE SYNC", C_LED_AMBER),
            "red":   (C_GLOW_RED,   C_LED_RED,    "SOS",           C_LED_RED),
        }
        glow, led, txt, tc = configs.get(state, configs["off"])
        self._cv.itemconfig(self._glow, fill=glow)
        self._cv.itemconfig(self._led,  fill=led)
        self._led_lbl.config(text=txt, fg=tc)

        friend = "B" if self.device.id == "A" else "A"
        if self.device.friend_unhealthy:
            self._tap_btn.config(state=tk.NORMAL, bg="#4c0519", fg="#fda4af",
                                 text=f"TAP TO FEED\nDevice {friend}'s SCOBY  ►")
        else:
            self._tap_btn.config(state=tk.DISABLED, bg="#1c0000", fg=C_DIM,
                                 text=f"TAP TO FEED\nDevice {friend}'s SCOBY")

        status_map = {
            "off":   "Idle — waiting for presence or SCOBY alert",
            "amber": "Passive togetherness — both users present",
            "red":   "Friend's SCOBY needs care!  Tap to feed.",
        }
        self._status_lbl.config(text=status_map.get(state, ""))

        # Sync pH slider if hardware value changed (e.g. after feed recovery)
        actual = self.device.hw._ph
        if abs(self._ph_var.get() - actual) > 0.05:
            self._syncing = True
            self._ph_var.set(actual)
            self._syncing = False
            self._refresh_ph_label(actual)

    def _show_dispenser(self):
        self._disp_lbl.config(text="DISPENSER ACTIVE — delivering sugar...")
        self.root.after(3500, lambda: self._disp_lbl.config(text=""))

    def _refresh_ph_label(self, ph: float):
        if ph < PH_SAFE_LOW:
            txt, col = f"pH {ph:.2f}  — TOO ACIDIC  ↓", C_SICK
        elif ph > PH_SAFE_HIGH:
            txt, col = f"pH {ph:.2f}  — TOO ALKALINE ↑", C_SICK
        else:
            txt, col = f"pH {ph:.2f}  — HEALTHY", C_HEALTHY
        self._ph_lbl.config(text=txt, fg=col)


# ─── Main Application ─────────────────────────────────────────────────────────

class KompanionApp:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Kompanion — Digital Prototype")
        self.root.configure(bg=C_BG)
        self.root.resizable(False, False)
        self._build()

    def _build(self):
        bus = InProcessBus()
        bus.set_log(lambda msg, tag: self.root.after(0, self._log, msg, tag))

        # hardware
        hw_a = Hardware()
        hw_b = Hardware()

        # mock devices
        cli_a = MockMQTTClient("kompanion-A", bus)
        cli_b = MockMQTTClient("kompanion-B", bus)

        # devices
        self.dev_a = CompanionDevice("A", "B", hw_a, cli_a)
        self.dev_b = CompanionDevice("B", "A", hw_b, cli_b)
        for dev in (self.dev_a, self.dev_b):
            dev.log_cb = lambda msg, tag: self.root.after(0, self._log, msg, tag)
            hw_a.log_cb = hw_b.log_cb = dev.log_cb

        # ── title ────────────────────────────────────────────────────────────
        title_fr = tk.Frame(self.root, bg=C_BG)
        title_fr.pack(fill=tk.X, pady=10)
        tk.Label(title_fr, text="KOMPANION",
                 font=("Helvetica", 22, "bold"), bg=C_BG, fg=C_TEXT).pack()
        tk.Label(title_fr,
                 text="A Living Symbol of Long-Distance Friendship",
                 font=("Helvetica", 10), bg=C_BG, fg=C_DIM).pack()

        # ── device panels ────────────────────────────────────────────────────
        panels = tk.Frame(self.root, bg=C_BG)
        panels.pack(padx=12, pady=4)

        self.panel_a = DevicePanel(panels, self.dev_a, self.root, width=270)
        self.panel_a.pack(side=tk.LEFT, padx=6, pady=4, fill=tk.Y)

        mid = tk.Frame(panels, bg=C_BG, width=54)
        mid.pack(side=tk.LEFT, fill=tk.Y)
        mid.pack_propagate(False)
        tk.Label(mid, text="←\nMQTT\n→", bg=C_BG, fg=C_DIM,
                 font=("Courier", 9)).place(relx=0.5, rely=0.42, anchor="center")

        self.panel_b = DevicePanel(panels, self.dev_b, self.root, width=270)
        self.panel_b.pack(side=tk.LEFT, padx=6, pady=4, fill=tk.Y)

        # ── LED legend ───────────────────────────────────────────────────────
        leg = tk.Frame(self.root, bg=C_BG)
        leg.pack(pady=2)
        for color, label in [(C_LED_OFF,   "Off — idle"),
                              (C_LED_AMBER, "Amber — both present"),
                              (C_LED_RED,   "Red — SCOBY SOS")]:
            it = tk.Frame(leg, bg=C_BG)
            it.pack(side=tk.LEFT, padx=14)
            c = tk.Canvas(it, width=14, height=14, bg=C_BG, highlightthickness=0)
            c.pack(side=tk.LEFT)
            c.create_oval(1, 1, 13, 13, fill=color, outline="")
            tk.Label(it, text=f"  {label}", bg=C_BG, fg=C_DIM,
                     font=("Helvetica", 8)).pack(side=tk.LEFT)

        # ── log panel ────────────────────────────────────────────────────────
        log_fr = tk.LabelFrame(self.root, text="  MQTT Bus Log  ",
                               bg=C_BG, fg=C_DIM, font=("Helvetica", 8),
                               padx=4, pady=4)
        log_fr.pack(fill=tk.X, padx=12, pady=(4, 12))

        self._log_box = scrolledtext.ScrolledText(
            log_fr, height=9, bg="#0a0f1a", fg="#4ade80",
            font=("Courier", 8), state=tk.DISABLED, wrap=tk.WORD)
        self._log_box.pack(fill=tk.X)
        self._log_box.tag_config("a",   foreground=C_LOG_A)
        self._log_box.tag_config("b",   foreground=C_LOG_B)
        self._log_box.tag_config("bus", foreground=C_LOG_BUS)
        self._log_box.tag_config("sys", foreground=C_LOG_SYS)
        self._log_box.tag_config("hw",  foreground="#9ca3af")

        # ── start devices ────────────────────────────────────────────────────
        self.dev_a.start()
        self.dev_b.start()

        self._log("[SYSTEM] Kompanion prototype online — both devices started", "sys")
        self._log(f"[SYSTEM] Healthy SCOBY pH range: {PH_SAFE_LOW} – {PH_SAFE_HIGH}", "sys")
        self._log("[SYSTEM] Use the controls above to simulate sensors.", "sys")
        self._log("[SYSTEM] Bus messages below are the raw MQTT traffic.", "sys")

    def _log(self, msg: str, tag: str = "sys"):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_box.config(state=tk.NORMAL)
        self._log_box.insert(tk.END, f"[{ts}] {msg}\n", tag)
        self._log_box.see(tk.END)
        self._log_box.config(state=tk.DISABLED)

    def run(self):
        self.root.mainloop()


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    KompanionApp().run()
