import ctypes
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from xr18rec import config as config_mod  # noqa: E402
from xr18rec import gpio, oled, panel  # noqa: E402


class FakeLink:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))

    def close(self):
        pass


def lit(canvas):
    return sum(bin(b).count("1") for b in canvas.buf)


def region_text(canvas, x, y, w, h):
    return [[canvas.buf[((y + j) >> 3) * oled.WIDTH + x + i] >> ((y + j) & 7) & 1 for i in range(w)] for j in range(h)]


class GpioTests(unittest.TestCase):
    def test_structures_match_the_kernel_abi(self):
        self.assertEqual(ctypes.sizeof(gpio._Request), 592)
        self.assertEqual(ctypes.sizeof(gpio._Config), 272)
        self.assertEqual(ctypes.sizeof(gpio._ConfigAttr), 24)
        self.assertEqual(ctypes.sizeof(gpio._Values), 16)
        self.assertEqual((gpio.GET_LINE, gpio.GET_VALUES, gpio.SET_VALUES), (0xC250B407, 0xC010B40E, 0xC010B40F))

    def test_missing_chip_is_a_clean_error(self):
        with self.assertRaises(gpio.GpioError):
            gpio.Lines([17], gpio.FLAG_INPUT, chip="/dev/gpiochip-inexistant")


class OledTests(unittest.TestCase):
    def test_font_covers_ascii_and_strips_accents(self):
        self.assertTrue(all(c in oled.FONTS["6x12"][2] for c in "ABCxyz09 éèêàçôûÉ"))
        self.assertEqual(oled.ascii_text("Prêt à enregistrer ●"), "Prêt à enregistrer ")  # accents conservés ; ● ignoré
        self.assertEqual(oled.ascii_text("prêt @", "16x32"), "PRÊT ")  # titres : majuscules, accents compris ; caractère inconnu ignoré
        self.assertEqual(oled.Canvas.text_width("ABC"), 18)
        self.assertEqual(oled.Canvas.text_width("PRET", "16x32"), 64)

    def test_text_and_pixels(self):
        c = oled.Canvas()
        end = c.text(0, 0, "I")
        self.assertEqual(end, 6)  # une cellule de 6 pixels
        self.assertTrue(any(c.buf[x] for x in range(6)))
        self.assertFalse(any(c.buf[x] for x in range(6, 128)))  # rien au-delà de la cellule
        d = oled.Canvas()
        d.text(0, 0, "g")  # descente : la lettre déborde sous la ligne de base mais reste dans la cellule de 12 lignes
        self.assertTrue(any(d.buf[oled.WIDTH + x] for x in range(6)))
        c.pixel(127, 63)
        self.assertTrue(c.buf[7 * 128 + 127] & 0x80)
        c.pixel(200, 5)  # hors de l'écran : ignoré
        c.clear()
        self.assertEqual(lit(c), 0)

    def test_driver_sends_only_changed_pages(self):
        link = FakeLink()
        d = oled.Oled(link)
        d.init()
        self.assertIn(0xAF, link.writes[-1])
        c = oled.Canvas()
        c.text(0, 0, "A")
        self.assertEqual(d.show(c), 8)  # première image : tout part
        link.writes.clear()
        self.assertEqual(d.show(c), 0)  # rien n'a changé
        self.assertEqual(link.writes, [])
        c.pixel(5, 40)  # page 5
        self.assertEqual(d.show(c), 1)
        self.assertEqual(link.writes[0], bytes([0x00, 0xB5, 0x00, 0x10]))
        self.assertEqual(sum(len(w) - 1 for w in link.writes[1:]), 128)

    def test_sh1106_column_offset_and_rotation(self):
        link = FakeLink()
        d = oled.Oled(link, "sh1106", rotate=True)
        d.init()
        self.assertIn(0xC0, link.writes[0] + link.writes[1])
        link.writes.clear()
        d.show(oled.Canvas())
        self.assertEqual(link.writes[0], bytes([0x00, 0xB0, 0x02, 0x10]))

    def test_detect_tries_addresses(self):
        tried = []

        def opener(bus, addr):
            tried.append(addr)
            if addr == 0x3C:
                raise OSError("absent")
            return FakeLink()
        self.assertIsNotNone(oled.detect(1, (0x3C, 0x3D), opener))
        self.assertEqual(tried, [0x3C, 0x3D])
        self.assertIsNone(oled.detect(1, (0x3C,), opener))


class ButtonTests(unittest.TestCase):
    def press(self, logic, start, duration, recording, step=0.05):
        """Appui de `duration` s à partir de `start` ; renvoie les actions et les surimpressions vues."""
        actions, overlays, t = [], [], start
        while t < start + duration:
            a, o = logic.update(t, True, recording)
            actions += [a] if a else []
            overlays += [o] if o else []
            t += step
        a, o = logic.update(t, False, recording)
        actions += [a] if a else []
        return actions, overlays

    def test_tap_when_idle_starts(self):
        self.assertEqual(self.press(panel.ButtonLogic(), 0, 0.3, False)[0], ["start"])

    def test_tap_while_recording_only_hints(self):
        self.assertEqual(self.press(panel.ButtonLogic(), 0, 0.3, True)[0], ["hint"])

    def test_hold_stops_recording_once(self):
        actions, overlays = self.press(panel.ButtonLogic(), 0, 3.0, True)
        self.assertEqual(actions, ["stop"])  # une seule fois, et pas de « start » au relâchement
        self.assertTrue(overlays and overlays[0][0].startswith("Maintenir"))
        self.assertLess(overlays[0][1], 0.5)

    def test_long_hold_when_idle_powers_off(self):
        actions, overlays = self.press(panel.ButtonLogic(), 0, 6.0, False)
        self.assertEqual(actions, ["poweroff"])
        self.assertTrue(any("EXTINCTION" in o[0] for o in overlays))

    def test_medium_press_when_idle_does_nothing(self):
        self.assertEqual(self.press(panel.ButtonLogic(), 0, 2.0, False)[0], [])

    def test_release_before_stop_threshold_cancels(self):
        self.assertEqual(self.press(panel.ButtonLogic(), 0, 1.5, True)[0], [])


STATUS_IDLE = {"state": "idle", "version": "0.3.3", "remaining_s": 380000, "free_gb": 900, "alerts": [],
               "storage": {"kind": "usb"}, "overruns": 0}
STATUS_REC = {**STATUS_IDLE, "state": "recording", "session": "20261024-152332", "elapsed_s": 3725, "remaining_s": 5400}


class RenderTests(unittest.TestCase):
    def render(self, st, now=0.0, overlay=None):
        c = oled.Canvas()
        panel.render(c, st, now, ("192.168.1.23", "xr18rec.local"), overlay)
        return c

    def test_formats(self):
        self.assertEqual([panel.fmt_clock(s) for s in (0, 65, 3725, 36000 + 5)], ["00:00", "01:05", "1:02:05", "10:00:05"])
        self.assertEqual([panel.fmt_remaining(s) for s in (None, 600, 5400, 380000)], ["?", "10 min", "1h30", "105h33"])

    def test_screens_differ_and_fit(self):
        idle, rec = self.render(STATUS_IDLE), self.render(STATUS_REC)
        self.assertNotEqual(idle.buf, rec.buf)
        self.assertNotEqual(self.render(None).buf, idle.buf)
        longest = self.render({**STATUS_REC, "elapsed_s": 10 * 3600 + 23 * 60 + 45})  # 10:23:45 doit tenir
        for edge in (0, 127):  # rien ne touche les bords : le chronomètre n'est pas rogné
            self.assertFalse(any(any(row) for row in region_text(longest, edge, 13, 1, 36)))
        self.assertTrue(any(any(row) for row in region_text(longest, 1, 13, 14, 36)))  # les 8 caractères occupent bien la largeur
        short = self.render({**STATUS_REC, "elapsed_s": 65})  # 01:05 : chiffres plus grands que 10:23:45
        self.assertGreater(lit(short), lit(longest) * 0.8)

    def test_record_dot_blinks(self):
        on, off = self.render(STATUS_REC, 0.0), self.render(STATUS_REC, 0.6)
        self.assertNotEqual(on.buf, off.buf)

    def test_alert_bar_cycles_and_is_inverted(self):
        alerts = [{"code": "overruns", "message": "x"}, {"code": "take_undervoltage", "message": "y"}]
        a, b = self.render({**STATUS_REC, "overruns": 3, "alerts": alerts}, 0.0), self.render({**STATUS_REC, "overruns": 3, "alerts": alerts}, 3.5)
        self.assertNotEqual(a.buf, b.buf)
        self.assertEqual(region_text(a, 0, 0, 1, 1)[0][0], 1)  # barre allumée en haut de l'écran
        self.assertEqual(panel.short_alert(alerts[0], {"overruns": 3}), "AUDIO PERDU x3")
        self.assertEqual(panel.short_alert({"code": "inconnu", "message": "bizarre"}, {}), "BIZARRE")

    def test_overlay_draws_progress(self):
        base = self.render(STATUS_REC)
        c = self.render(STATUS_REC, overlay=("Maintenir : ARRET", 0.5))
        self.assertNotEqual(base.buf, c.buf)


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class PanelLoopTests(unittest.TestCase):
    def make(self, status=None, sleep_s=300, with_display=True):
        self.cfg = config_mod.Config(panel_sleep_s=sleep_s)
        self.link = FakeLink()
        self.display = oled.Oled(self.link) if with_display else None
        if self.display:
            self.display.init()
        self.clock = FakeClock()
        self.pressed = False
        self.leds = []
        self.posts = []
        p = panel.Panel(self.cfg, self.display, lambda: self.pressed, lambda rec, ok: self.leds.append((rec, ok)), None,
                        lambda path, body: self.posts.append((path, body)), self.clock, ("1.2.3.4", "h.local"))
        p.status = status
        return p

    def run_press(self, p, seconds):
        self.pressed = True
        end = self.clock.t + seconds
        while self.clock.t < end:
            p.step()
            self.clock.t += 0.05
        self.pressed = False
        for _ in range(3):  # le relâchement est confirmé sur deux lectures
            p.step()
            self.clock.t += 0.05
        time.sleep(0.05)  # les appels HTTP partent dans un fil

    def test_tap_starts_a_recording(self):
        p = self.make(STATUS_IDLE)
        self.run_press(p, 0.3)
        self.assertEqual(self.posts, [("/api/start", {})])

    def test_hold_stops_a_recording(self):
        p = self.make(STATUS_REC)
        self.run_press(p, 2.5)
        self.assertEqual(self.posts, [("/api/stop", {})])

    def test_long_hold_powers_off_when_idle(self):
        p = self.make(STATUS_IDLE)
        self.run_press(p, 5.5)
        self.assertEqual(self.posts, [("/api/system/power", {"action": "poweroff"})])

    def test_leds_follow_the_state(self):
        p = self.make(STATUS_IDLE)
        p.step()
        self.assertEqual(self.leds[-1], (False, True))  # prêt : verte
        p.status = STATUS_REC
        p.step()
        self.assertEqual(self.leds[-1], (True, False))  # prise : rouge
        p.status = {**STATUS_REC, "alerts": [{"code": "overruns", "message": "x"}]}
        seen = set()
        for _ in range(8):
            p.step()
            seen.add(self.leds[-1][0])
            self.clock.t += 0.13
        self.assertEqual(seen, {True, False})  # clignote en cas d'alerte

    def test_display_sleeps_when_idle_but_never_while_recording(self):
        p = self.make(STATUS_IDLE, sleep_s=60)
        p.step()
        self.assertTrue(self.display._on)
        self.clock.t += 120
        p.step()
        self.assertFalse(self.display._on)
        p.status = STATUS_REC
        p.step()
        self.assertTrue(self.display._on)  # une prise réveille l'écran
        self.clock.t += 7200
        p.step()
        self.assertTrue(self.display._on)  # et il reste allumé pendant toute la prise

    def test_identical_screens_are_not_redrawn(self):
        p = self.make(STATUS_IDLE)
        p.step()
        self.link.writes.clear()
        for _ in range(12):  # 12 tours de 20 ms : même affichage, rien n'est renvoyé à l'écran
            self.clock.t += 0.02
            p.step()
        self.assertEqual(self.link.writes, [])
        self.assertEqual(panel.frame_key(STATUS_REC, 10.2, ("a", "b")), panel.frame_key(STATUS_REC, 10.3, ("a", "b")))
        self.assertNotEqual(panel.frame_key(STATUS_REC, 10.2, ("a", "b")), panel.frame_key({**STATUS_REC, "elapsed_s": 3726}, 10.2, ("a", "b")))

    def test_works_without_display_or_leds(self):
        p = self.make(STATUS_IDLE, with_display=False)
        p.set_leds = None
        p.step()

    def test_api_failure_is_shown_not_raised(self):
        p = self.make(STATUS_IDLE)

        def broken(path, body):
            raise RuntimeError("disque absent")
        p.post = broken
        self.run_press(p, 0.3)
        self.assertIn("Échec REC", p.notice[0])


class BuildTests(unittest.TestCase):
    def test_no_display_means_no_panel(self):
        self.assertIsNone(panel.build(config_mod.Config(panel_i2c_bus=9)))  # /dev/i2c-9 n'existe pas
        self.assertIsNone(panel.build(config_mod.Config(panel="off")))

    def test_run_without_hardware_exits_cleanly(self):
        self.assertEqual(panel.run(config_mod.Config(panel_i2c_bus=9)), 0)

    def test_supervisor_stops_when_there_is_no_hardware(self):
        stop = threading.Event()
        t = panel.supervise(None, stop, retry_s=0.1)
        t.join(20)  # le processus enfant sort aussitôt avec le code 0 : plus rien à surveiller
        self.assertFalse(t.is_alive())
        stop.set()


class ConfigTests(unittest.TestCase):
    def test_validation(self):
        config_mod.Config().validate()
        for bad in ({"panel": "oui"}, {"panel_driver": "ili9341"}, {"panel_button_gpio": 3}, {"panel_led_rec_gpio": 40},
                    {"panel_led_ok_gpio": 17}, {"panel_i2c_address": 3}, {"panel_sleep_s": -1}):
            with self.assertRaises(ValueError, msg=str(bad)):
                config_mod.Config(**bad).validate()
        config_mod.Config(panel_led_rec_gpio=0, panel_led_ok_gpio=0, panel_i2c_address=0x3D).validate()


if __name__ == "__main__":
    unittest.main()


class SimulatorTests(unittest.TestCase):
    def test_board_prolongs_a_very_short_click(self):
        from xr18rec import panelsim
        b = panelsim.Board()
        b.press(True)
        b.press(False)  # clic de la souris quasi instantané
        self.assertTrue(b.pressed)  # le contact reste établi ~120 ms : l'anti-rebond le voit
        time.sleep(0.25)
        self.assertFalse(b.pressed)

    def test_power_off_is_never_sent_for_real(self):
        from xr18rec import panelsim
        sent = []
        srv, p = panelsim.make_server(config_mod.Config(), 0, fetch=lambda: STATUS_IDLE, post=lambda path, body: sent.append(path))
        try:
            p.post("/api/system/power", {"action": "poweroff"})
            p.post("/api/start", {})
            self.assertEqual(sent, ["/api/start"])
        finally:
            srv.server_close()
