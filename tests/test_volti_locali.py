"""Test del riconoscimento facciale sul Raspberry senza telecamere ne' OpenCV: raffica di foto e regole "volto"."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "integrazioni" / "hikvision-isapi"))

import hikvision_loxone as hk  # noqa: E402
import volti_locali as vl  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def burst(results, person="Mario", min_similarity=45, confirmations=2, seconds=5, fail_first=False):
    """Esegue run_burst con foto finte: results e' una lista (una voce per foto) di liste di FaceHit."""
    clock = FakeClock()
    photos = iter(results)
    state = {"failed": not fail_first}

    def grab():
        clock.t += 0.2                                       # il tempo di scattare una foto
        if not state["failed"]:
            state["failed"] = True
            raise TimeoutError("foto in ritardo")
        hits = next(photos, [])
        return b"jpeg", hits

    return vl.run_burst(grab, lambda hits: hits, person, min_similarity, confirmations, seconds,
                        now=clock.now, sleep=clock.sleep)


def hit(person, sim, width=80):
    return vl.FaceHit(person, sim, width, 0.9)


class BurstTest(unittest.TestCase):
    def test_fires_after_confirmations_and_stops(self):
        res = burst([[hit("Mario", 0.50)], [hit("Mario", 0.30)], [hit("Mario", 0.47)], [hit("Mario", 0.60)]])
        self.assertTrue(res.fired)
        self.assertEqual((res.photos, res.matches), (3, 2))      # si ferma alla seconda conferma
        self.assertAlmostEqual(res.best.similarity, 0.50)

    def test_other_people_small_faces_and_low_similarity_do_not_count(self):
        res = burst([[hit("Luigi", 0.90)], [hit("Mario", 0.80, width=20)], [hit("Mario", 0.40)], [hit(None, 0.0)]])
        self.assertFalse(res.fired)
        self.assertEqual(res.matches, 0)
        self.assertEqual(res.best.person, "Luigi")             # il migliore resta registrato per il controllo
        self.assertEqual(res.faces, 3)                         # il volto da 20 px non conta

    def test_photo_errors_are_retried_until_deadline(self):
        res = burst([[hit("Mario", 0.50)], [hit("Mario", 0.55)]], fail_first=True)
        self.assertTrue(res.fired)
        self.assertEqual(len(res.errors), 1)

    def test_deadline(self):
        res = burst([[]] * 100, seconds=2)
        self.assertFalse(res.fired)
        self.assertLessEqual(res.photos, 6)                     # circa 0,45 s per foto


class FaceWatchTest(unittest.TestCase):
    def test_confirmations_window_cooldown_and_stats(self):
        w = vl.FaceWatch("Mario", 45, confirmations=2, window=8, cooldown=120)
        self.assertEqual(w.step([hit("Mario", 0.50)], now=0), ([hit("Mario", 0.50)], False))
        self.assertFalse(w.step([hit("Mario", 0.52)], now=10)[1])      # la prima conferma e' scaduta (8 s)
        mine, fire = w.step([hit("Mario", 0.48), hit("Luigi", 0.61)], now=12)
        self.assertTrue(fire)
        self.assertEqual([h.person for h in mine], ["Mario"])
        self.assertFalse(w.step([hit("Mario", 0.50)], now=13)[1])      # pausa di 120 s dopo il comando
        self.assertFalse(w.step([hit("Mario", 0.50)], now=14)[1])
        self.assertTrue(w.step([hit("Mario", 0.50)], now=133)[1] or w.step([hit("Mario", 0.50)], now=134)[1])
        w.reset_stats()
        w.step([hit("Mario", 0.40), hit(None, 0.0), hit("Mario", 0.9, width=10)], now=200)
        self.assertEqual((w.photos, w.faces), (1, 2))                   # il volto da 10 px non conta
        self.assertAlmostEqual(w.best_other, 0.40)                      # sotto soglia: utile per tarare


class FaceRuleTest(unittest.TestCase):
    CONFIG = '''
[miniserver]
host = "10.0.0.1"
user = "u"
password = "p"

[[cameras]]
name = "Scale"
host = "10.0.0.5"
user = "u"
password = "p"

[[cameras.events]]
type = "volto"
target = "human"
person = "Mario"
min_similarity = 45
confirmations = 2
window = 8
inputs = ["mario_entrata"]
cooldown = 120
dry_run = true
'''

    def test_parse_dump_and_describe(self):
        ms, cams = hk.load_config(self.CONFIG)
        rule = cams[0].rules[0]
        self.assertEqual(rule.triggers, ["fielddetection", "linedetection"])
        again = hk.load_config(hk.dump_config(ms, cams))[1][0].rules[0]
        self.assertEqual(again, rule)
        self.assertIn("riconoscimento sul Raspberry dopo intrusione/attraversamento linea", hk.describe_rule(rule))
        self.assertIn("2 conferme in 8 s", hk.describe_rule(rule))

    def test_continuous_rule(self):
        ms, cams = hk.load_config(self.CONFIG.replace('type = "volto"', 'type = "volto"\ncontinuous = true'))
        rule = cams[0].rules[0]
        self.assertTrue(rule.continuous)
        self.assertTrue(hk.load_config(hk.dump_config(ms, cams))[1][0].rules[0].continuous)
        self.assertIn("analisi continua dei volti sul Raspberry", hk.describe_rule(rule))
        w = hk.CameraWorker(cams[0], loxone=None)
        started = []
        w.start_face_burst = lambda i, r: started.append(i)
        w.handle(hk.Event("fielddetection", "active", {1}, "human", "1"), now=10)
        self.assertEqual(started, [])                                   # niente raffiche: guarda gia' di continuo

    def test_trigger_starts_one_burst(self):
        _, cams = hk.load_config(self.CONFIG)
        started = []
        w = hk.CameraWorker(cams[0], loxone=None)
        w.start_face_burst = lambda i, rule: (started.append(i), setattr(w, "burst_running", True))
        person = hk.Event("fielddetection", "active", {1}, "human", "1")
        w.handle(hk.Event("fielddetection", "active", {1}, "vehicle", "1"), now=10)   # auto: no
        w.handle(hk.Event("vmd", "active", set(), None, "1"), now=10)                # movimento: no
        self.assertEqual(started, [])
        w.handle(person, now=11)
        w.handle(person, now=11.5)                                                    # raffica gia' in corso
        self.assertEqual(started, [0])
        w.burst_running = False
        w.last_fired[(0, "mario_entrata")] = 20
        w.handle(person, now=60)                                                      # pausa dopo l'ultimo comando
        self.assertEqual(started, [0])
        w.handle(person, now=141)
        self.assertEqual(started, [0, 0])


if __name__ == "__main__":
    unittest.main()
