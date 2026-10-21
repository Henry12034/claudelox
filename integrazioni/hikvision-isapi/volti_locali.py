"""Riconoscimento facciale sul Raspberry con OpenCV: YuNet trova i volti, SFace li confronta con quelli noti.

Serve con le telecamere che inquadrano dall'alto, dove il rilevatore dei registratori non trova volti: YuNet li
trova anche piccoli (da ~40 px) e di sbieco nelle foto a piena risoluzione della telecamera. I volti noti sono
salvati come "impronte" (128 numeri per foto), non come immagini, in KNOWN_PATH.

Modelli dell'OpenCV Zoo (licenza Apache 2.0, https://github.com/opencv/opencv_zoo), in MODEL_DIR; install.sh li
scarica e ne controlla l'impronta sha256.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field

MODEL_DIR = os.environ.get("CLAUDELOX_MODELLI", "/opt/claudelox-raspberry/modelli")
DETECTOR = "face_detection_yunet_2023mar.onnx"
RECOGNIZER = "face_recognition_sface_2021dec.onnx"
MODEL_SHA256 = {DETECTOR: "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
                RECOGNIZER: "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"}
KNOWN_PATH = os.path.join(os.environ.get("STATE_DIRECTORY", "/var/lib/hikvision-loxone"), "volti-noti.npz")
SAME_PERSON = 0.363      # soglia di SFace (coseno) per "stessa persona"; per un antifurto se ne usa una piu' alta
MIN_FACE_PX = 36         # volti piu' piccoli danno impronte poco affidabili: non contano
PHOTO_GAP_S = 0.25       # pausa tra due foto della raffica


@dataclass
class FaceHit:
    person: str | None       # persona nota piu' somigliante (None se non ce ne sono)
    similarity: float        # coseno da 0 a 1 (x 100 = percento)
    width: int               # larghezza del volto in pixel
    score: float             # sicurezza del rilevatore
    box: tuple[int, int, int, int] = (0, 0, 0, 0)


@dataclass
class BurstResult:
    photos: int = 0
    faces: int = 0
    matches: int = 0
    best: FaceHit | None = None
    best_photo: bytes = b""
    fired: bool = False
    errors: list[str] = field(default_factory=list)


def verify_models(model_dir: str = MODEL_DIR) -> list[str]:
    """Problemi dei modelli: mancanti o diversi da quelli verificati."""
    problems = []
    for name, digest in MODEL_SHA256.items():
        path = os.path.join(model_dir, name)
        if not os.path.exists(path):
            problems.append(f"manca {path}")
            continue
        with open(path, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != digest:
                problems.append(f"{name} non e' quello verificato")
    return problems


class Recognizer:
    """Rilevatore e riconoscitore caricati una volta; le impronte note si leggono e si salvano da KNOWN_PATH."""

    def __init__(self, model_dir: str = MODEL_DIR, known_path: str = KNOWN_PATH, shrink: int = 2) -> None:
        import cv2
        import numpy as np
        self.cv2, self.np = cv2, np
        self.det = cv2.FaceDetectorYN.create(os.path.join(model_dir, DETECTOR), "", (320, 320), 0.6, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(os.path.join(model_dir, RECOGNIZER), "")
        self.known_path = known_path
        self.shrink = shrink
        self.known: dict = {}
        self.load()

    def load(self) -> None:
        self.known = {}
        if os.path.exists(self.known_path):
            data = self.np.load(self.known_path)
            for name, vec in zip(data["names"], data["features"]):
                self.known.setdefault(str(name), []).append(vec)
            self.known = {k: self.np.stack(v) for k, v in self.known.items()}

    def save(self) -> None:
        names = [n for n, arr in self.known.items() for _ in arr]
        feats = [v for arr in self.known.values() for v in arr]
        os.makedirs(os.path.dirname(self.known_path), exist_ok=True)
        tmp = self.known_path + ".tmp.npz"
        self.np.savez(tmp, names=self.np.array(names, dtype=str),
                      features=self.np.stack(feats) if feats else self.np.zeros((0, 128), "float32"))
        os.chmod(tmp, 0o640)
        os.replace(tmp, self.known_path)

    def decode(self, jpeg: bytes):
        return self.cv2.imdecode(self.np.frombuffer(jpeg, self.np.uint8), self.cv2.IMREAD_COLOR)

    def detect(self, img) -> list:
        """Volti trovati (righe YuNet: riquadro, 5 punti, sicurezza) in coordinate della foto intera."""
        s = self.shrink if min(img.shape[:2]) > 800 else 1     # a meta' risoluzione e' 4 volte piu' veloce
        small = self.cv2.resize(img, (img.shape[1] // s, img.shape[0] // s)) if s > 1 else img
        self.det.setInputSize((small.shape[1], small.shape[0]))
        _, faces = self.det.detect(small)
        out = []
        for f in faces if faces is not None else []:
            f = f.copy()
            f[:14] *= s
            out.append(f)
        return out

    def feature(self, img, face):
        v = self.rec.feature(self.rec.alignCrop(img, face)).flatten().astype("float32")
        return v / (self.np.linalg.norm(v) or 1.0)

    def identify(self, img) -> list[FaceHit]:
        hits = []
        for f in self.detect(img):
            v = self.feature(img, f)
            best, who = 0.0, None
            for name, arr in self.known.items():
                sim = float((arr @ v).max())
                if sim > best:
                    best, who = sim, name
            hits.append(FaceHit(who, best, int(f[2]), float(f[14]), tuple(int(x) for x in f[:4])))
        return hits

    def add(self, person: str, vec) -> None:
        arr = self.known.get(person)
        self.known[person] = vec[None] if arr is None else self.np.vstack([arr, vec])

    def similarity_to(self, person: str, vec) -> float | None:
        arr = self.known.get(person)
        return None if arr is None else float((arr @ vec).max())

    def enroll(self, person: str, img) -> FaceHit | None:
        """Aggiunge l'impronta del volto piu' grande della foto; restituisce None se non trova volti."""
        faces = self.detect(img)
        if not faces:
            return None
        f = max(faces, key=lambda r: r[2] * r[3])
        self.add(person, self.feature(img, f))
        return FaceHit(person, 1.0, int(f[2]), float(f[14]), tuple(int(x) for x in f[:4]))

    def forget(self, person: str) -> bool:
        return self.known.pop(person, None) is not None


class FaceWatch:
    """Analisi continua: decide quando una persona e' stata riconosciuta abbastanza volte (senza telecamera)."""

    def __init__(self, person: str, min_similarity: float, confirmations: int, window: float, cooldown: float) -> None:
        self.person = person
        self.min_similarity = min_similarity
        self.confirmations = max(1, confirmations)
        self.window = window
        self.cooldown = cooldown
        self.hits: list[float] = []
        self.last_fired = -1e9
        self.reset_stats()

    def reset_stats(self) -> None:
        self.photos = 0
        self.faces = 0
        self.best_other = 0.0          # somiglianza piu' alta dei volti NON accettati: serve a tarare la soglia

    def step(self, hits: list[FaceHit], now: float) -> tuple[list[FaceHit], bool]:
        """Volti di una foto -> (volti della persona sopra soglia, True se e' il momento di agire)."""
        self.photos += 1
        big = [h for h in hits if h.width >= MIN_FACE_PX]
        self.faces += len(big)
        mine = [h for h in big if h.person == self.person and h.similarity * 100 >= self.min_similarity]
        for h in big:
            if h not in mine:
                self.best_other = max(self.best_other, h.similarity)
        if not mine:
            return [], False
        self.hits = [t for t in self.hits if now - t <= self.window] + [now]
        if len(self.hits) >= self.confirmations and now - self.last_fired >= self.cooldown:
            self.hits = []
            self.last_fired = now
            return mine, True
        return mine, False


def run_burst(grab, identify, person: str, min_similarity: float, confirmations: int, seconds: float,
              now=time.monotonic, sleep=time.sleep) -> BurstResult:
    """Raffica di foto: si ferma appena `person` e' riconosciuta in `confirmations` foto (somiglianza in percento).

    grab() restituisce (jpeg, immagine) oppure solleva un errore; identify(immagine) restituisce i FaceHit.
    """
    res = BurstResult()
    deadline = now() + seconds
    while now() < deadline:
        try:
            jpeg, img = grab()
        except Exception as e:                       # foto non arrivata: si riprova fino alla scadenza
            res.errors.append(f"{type(e).__name__}: {e}")
            sleep(PHOTO_GAP_S)
            continue
        res.photos += 1
        hits = [h for h in identify(img) if h.width >= MIN_FACE_PX]
        res.faces += len(hits)
        mine = [h for h in hits if h.person == person and h.similarity * 100 >= min_similarity]
        for h in hits:
            if res.best is None or h.similarity > res.best.similarity:
                res.best, res.best_photo = h, jpeg
        if mine:
            res.matches += 1
            if res.matches >= confirmations:
                res.fired = True
                return res
        sleep(PHOTO_GAP_S)
    return res
