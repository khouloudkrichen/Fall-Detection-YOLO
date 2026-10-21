import math
import os
import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import requests

try:
    import winsound
except ImportError:
    winsound = None


# ======================= A REMPLIR =======================
# Mets ton nouveau token seul dans "telegram_token.txt", dans le meme dossier.
# Le token ne doit pas etre ecrit en clair dans ce script.
TELEGRAM_TOKEN = "8757363794:AAHt5QA8irVA5jAXrMLwk4BuL5I6Y2BGrTs"  
TELEGRAM_CHAT_IDS = ["8869896870"]

LIEU = "Chambre 1"
SOURCE = 0   # Webcam : 0. Pour une video, utiliser par ex. r"D:\\cvProjet\\squat-counter-main\\ma_video.mp4"
CAMERA_INDICES = (0, 1, 2)  # indices essayes si SOURCE est un entier et la camera demandee est indisponible

SOUND_ALARM = True
ALERT_COOLDOWN = 60   # s : pas plus d'un message Telegram par minute
# =========================================================

MODEL_NAME = "yolo11n-pose.pt"
PERSON_MODEL_NAME = "yolo11n.pt"  # Detecteur d objets : classe person uniquement
PERSON_CONF = 0.45
MIN_PERSON_OVERLAP = 0.25  # fraction de la boite de pose couverte par une detection "person"
WINDOW = "Surveillance - chutes possibles"
DETECT_CONF = 0.35
KP_CONF = 0.35

# ---- Seuils ----
LYING_ANGLE = 40           # buste < 40 deg = allonge
UPRIGHT_ANGLE = 60         # buste > 60 deg = debout
LYING_RATIO = 1.3          # largeur/hauteur de la boite > 1.3 = allonge
UPRIGHT_MEMORY = 2.0       # s : il faut avoir ete debout dans cette fenetre
CONFIRM_SECONDS = 1.5      # s allonge pour confirmer (evite les faux "penche")
GAP_TOLERANCE = 0.6        # s : trou tolere pendant la confirmation
LOST_ALERT_SECONDS = 2.5   # s : corps illisible apres position debout => alerte
EDGE_MARGIN = 0.02         # boite qui touche le bord de l'image => sortie, pas une chute
TOO_CLOSE_WIDTH = 0.70     # boite plus large que 70% de l'image => trop pres de la camera
MIN_BODY_POINTS = 4         # points du corps requis pour limiter les faux positifs
HEAD_ONLY_CLOSE = 0.25     # visage seul plus grand que 25% de l'image => trop pres
LOW_TOP_RATIO = 0.35       # haut de la boite sous 35% de l'image + buste pas droit => corps au ras du sol
LOW_MAX_HEIGHT = 0.60      # ... et boite moins haute que 60% de l'image
LYING_LOST_RESET = 3.0     # s : on garde en memoire une position au sol meme si YOLO perd le corps
RECOVER_SECONDS = 2.0      # s debout pour effacer l'alerte
TALL_ASPECT = 1.5          # boite "haute" : hauteur/largeur >= 1.5 ...
TALL_HEIGHT = 0.60         # ... ou hauteur >= 60% de l'image => vraiment debout
STAND_CONFIRM = 0.4        # s debout (boite haute) avant d'annuler une verification

TOKEN_FILE = Path(__file__).resolve().parent / "telegram_token.txt"


def load_token(configured):
    """Token depuis telegram_token.txt (prioritaire) ou la ligne TELEGRAM_TOKEN.
    Nettoie espaces, guillemets, 'bot' ou une adresse collee par erreur."""
    raw = ""
    try:
        if TOKEN_FILE.exists():
            raw = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
    except OSError:
        raw = ""
    if not raw and configured and configured != "COLLE_ICI_TON_TOKEN":
        raw = configured
    if not raw:
        return ""
    m = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", raw)
    return m.group(0) if m else raw.strip().strip("\"'")


TELEGRAM_TOKEN = load_token(TELEGRAM_TOKEN)


# ---------------- Telegram ----------------
def send_telegram(text, photo=None):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "COLLE_ICI_TON_TOKEN":
        print("[Telegram] Token non renseigne : " + text.splitlines()[0])
        return

    for chat_id in TELEGRAM_CHAT_IDS:
        method = "sendPhoto" if photo is not None else "sendMessage"
        data = {"chat_id": chat_id}
        files = None
        if photo is not None:
            data["caption"] = text
            files = {"photo": ("alerte.jpg", photo, "image/jpeg")}
        else:
            data["text"] = text

        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
                data=data, files=files, timeout=(5, 15),
            )
            payload = response.json()
            if response.ok and payload.get("ok"):
                print("[Telegram] Message envoye.")
            else:
                print("[Telegram] Erreur :",
                      payload.get("description", "Reponse inconnue"))
        except (requests.RequestException, ValueError):
            # on n'affiche pas l'exception : elle contiendrait le token
            print("[Telegram] Echec reseau ou reponse invalide.")


def check_telegram():
    """Verifie le token au demarrage et dit clairement si ca ne va pas."""
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "COLLE_ICI_TON_TOKEN":
        print("[Telegram] ATTENTION : token non renseigne (ligne TELEGRAM_TOKEN).")
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe",
            timeout=(5, 10),
        )
        d = r.json()
        if r.ok and d.get("ok"):
            print("[Telegram] Token valide : @" + str(d["result"].get("username")))
        else:
            print("[Telegram] TOKEN INVALIDE (" + str(d.get("description", "?"))
                  + ") : recopie-le depuis BotFather (/mybots, ton bot, API Token).")
    except (requests.RequestException, ValueError):
        print("[Telegram] Impossible de joindre Telegram (connexion internet ?).")


def notify_fall(reason, photo):
    if SOUND_ALARM and winsound is not None:
        try:
            winsound.Beep(1200, 400)
            winsound.Beep(1200, 400)
        except RuntimeError:
            pass
    text = (
        "ALERTE : CHUTE POSSIBLE\n"
        f"Lieu : {LIEU}\n"
        f"Heure : {time.strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"Motif : {reason}\n"
        "Merci de verifier la situation."
    )
    send_telegram(text, photo)


def notify_recovered():
    send_telegram(
        f"Fin d'alerte - {LIEU}\n"
        f"La personne est de nouveau debout ({time.strftime('%H:%M:%S')})."
    )


# ---------------- Analyse d'une personne ----------------
def analyse(points, conf, box, width, height):
    """Caracteristiques d'une personne. reliable=False si le torse est illisible."""
    def mid(a, b):
        pts = [points[i] for i in (a, b) if conf[i] >= KP_CONF]
        return np.mean(pts, axis=0) if pts else None

    shoulders, hips = mid(5, 6), mid(11, 12)
    angle = None
    if shoulders is not None and hips is not None:
        d = shoulders - hips
        if np.linalg.norm(d) >= 8:
            angle = math.degrees(math.atan2(abs(float(d[1])), abs(float(d[0]))))

    x0, y0, x1, y1 = map(float, box)
    bw, bh = max(x1 - x0, 1.0), max(y1 - y0, 1.0)

    # points du corps (epaules, coudes, poignets, hanches, genoux, chevilles)
    body_points = sum(1 for i in range(5, 17) if conf[i] >= KP_CONF)
    enough_body = body_points >= MIN_BODY_POINTS
    # une boite large ne veut dire "allonge" que si on voit vraiment un corps
    wide = enough_body and bw / bh > LYING_RATIO

    upright = angle is not None and angle > UPRIGHT_ANGLE
    # vraiment debout : torse droit ET boite haute (un corps recroqueville a
    # genoux peut avoir un torse "droit", mais sa boite est presque carree)
    tall = bh / bw >= TALL_ASPECT or bh / height >= TALL_HEIGHT
    standing = upright and tall
    # buste horizontal = "au sol" seulement si la boite n'est pas haute
    # (sinon c'est quelqu'un qui se penche, pas quelqu'un qui est tombe)
    floor_angle = (angle is not None and angle < LYING_ANGLE
                   and bh / height < TALL_HEIGHT)
    lying = floor_angle or (wide and not standing)
    reliable = angle is not None or wide

    # trop pres de la camera : la boite remplit presque toute la largeur
    too_close = bw / width > TOO_CLOSE_WIDTH
    # visage seul (aucun corps visible) et gros : la personne est collee a la camera
    if not enough_body and (bw / width > HEAD_ONLY_CLOSE
                            or bh / height > HEAD_ONLY_CLOSE):
        too_close = True
    # la boite touche le bord gauche ou droit de l'image
    edge = x0 <= EDGE_MARGIN * width or x1 >= (1 - EDGE_MARGIN) * width

    # Une posture basse seule ne suffit pas. On la retient comme indice
    # d'une personne au sol uniquement si la boite est aussi assez large,
    # ce qui aide a reconnaitre une personne couchee sans classer une silhouette
    # verticale (ex. vetements sur une chaise) comme une chute.
    low_posture = (
        angle is not None
        and not standing
        and not too_close
        and y0 > LOW_TOP_RATIO * height
        and bh / height < LOW_MAX_HEIGHT
        and bw / bh >= 0.85
    )
    if low_posture:
        lying = True
        reliable = True

    if too_close:
        lying = False

    return {
        "angle": angle,
        "upright": upright,
        "tall": tall,
        "lying": lying,
        "reliable": reliable,
        "cx": (x0 + x1) / 2 / width,
        "edge": edge,
        "too_close": too_close,
        "body_points": body_points,
        "enough_body": enough_body,
    }


# ---------------- Logique de chute (sans camera, testable) ----------------
class FallMonitor:
    def __init__(self):
        self.reset()

    def reset(self):
        self.last_upright = None
        self.last_edge = False
        self.lying_since = None
        self.last_lying = None
        self.lost_since = None
        self.lost_was_upright = False
        self.upright_since = None
        self.alert = False
        self.reason = ""
        self.label = "Observation"

    def _raise(self, reason):
        if self.alert:
            return None
        self.alert = True
        self.reason = reason
        self.label = "ALERTE : CHUTE POSSIBLE"
        return ("alert", reason)

    def update(self, info, now):
        """Retourne ('alert', motif), ('recovered', '') ou None."""
        event = None

        if info is not None and info["too_close"]:
            # Trop pres de la camera : posture incertaine.
            # Reinitialiser le chronometre debout pour ne pas confirmer
            # un retour debout a partir d'images incompletes.
            self.upright_since = None
            self.lost_since = None
            # on ne ramene pas a zero une verification de chute en cours :
            # un objet pris pour une personne ne doit pas la bloquer.
            # (elle expire d'elle-meme si on ne revoit pas de position au sol)
            if (self.last_lying is not None
                    and now - self.last_lying > LYING_LOST_RESET):
                self.lying_since = None
                self.last_lying = None
            if not self.alert:
                self.label = ("Chute possible : verification"
                              if self.lying_since is not None
                              else "Trop pres de la camera")
            return None

        if info is not None and info["reliable"]:
            self.lost_since = None
            self.last_edge = info["edge"]

            # Un retour debout n'est valide que si le corps est suffisamment
            # visible, pas trop pres de la camera et pas coupe par le bord.
            # Cela evite de cloturer une alerte sur quelques points instables.
            standing = (
                info["upright"]
                and info["tall"]
                and info.get("enough_body", False)
                and info.get("body_points", 0) >= MIN_BODY_POINTS
                and not info["too_close"]
                and not info["edge"]
            )
            if standing:
                self.last_upright = now
                if self.upright_since is None:
                    self.upright_since = now

                stand_duration = now - self.upright_since
                if self.alert:
                    # Ne cloturer l'alerte qu'apres une vraie confirmation
                    # debout continue pendant RECOVER_SECONDS.
                    if stand_duration >= RECOVER_SECONDS:
                        self.alert = False
                        self.reason = ""
                        self.lying_since = None
                        self.last_lying = None
                        event = ("recovered", "")
                elif stand_duration >= STAND_CONFIRM:
                    # Sans alerte active, une posture debout stable annule
                    # seulement une detection de chute encore en verification.
                    self.lying_since = None
                    self.last_lying = None
            else:
                self.upright_since = None

            recent = (self.last_upright is not None
                      and now - self.last_upright <= UPRIGHT_MEMORY)

            if info["lying"]:
                # Le filtre pick_person a deja exige une detection person
                # independante et une pose anatomique credible. Ne pas exiger
                # que la personne ait ete debout dans les 2 dernieres secondes :
                # une chute peut commencer apres un accroupissement ou une
                # perte temporaire de suivi pres de la camera. Toute posture
                # au sol credible est donc confirmee pendant CONFIRM_SECONDS.
                if self.lying_since is None:
                    self.lying_since = now
                if self.lying_since is not None:
                    self.last_lying = now
                    self.label = "Chute possible : verification"
                    if now - self.lying_since >= CONFIRM_SECONDS:
                        event = self._raise(
                            "personne allongee apres position debout") or event
                else:
                    self.label = "Posture au sol : verification"
            else:
                # ni debout, ni au sol (torse "droit" d'un corps replie, image floue...) :
                # on garde la verification en cours quelques secondes
                if (self.lying_since is not None and self.last_lying is not None
                        and now - self.last_lying > LYING_LOST_RESET):
                    self.lying_since = None
                self.label = ("Chute possible : verification"
                              if self.lying_since is not None else "Suivi actif")

        else:
            # personne absente OU corps illisible (trop pres, cache, coupe...)
            if self.lost_since is None:
                self.lost_since = now
                self.lost_was_upright = (
                    self.last_upright is not None
                    and now - self.last_upright <= UPRIGHT_MEMORY
                )
            if (self.last_lying is not None
                    and now - self.last_lying > LYING_LOST_RESET):
                self.lying_since = None

            left_by_edge = self.last_edge

            if self.lying_since is not None:
                # une position au sol etait en cours de verification
                self.label = "Chute possible : verification"
                if now - self.lying_since >= CONFIRM_SECONDS:
                    event = self._raise(
                        "personne au sol (corps en partie non detecte)")
            elif self.lost_was_upright and not left_by_edge:
                # Une perte de suivi apres une position debout ne suffit pas
                # a prouver une chute : la personne peut etre proche de la
                # camera, floue ou partiellement hors champ.
                # On conserve l'alerte si une posture au sol a deja ete
                # observee (branche self.lying_since ci-dessus), mais on
                # n'envoie pas d'alerte de chute sur la seule disparition.
                self.label = "Personne non visible : verification requise"
            else:
                self.label = "Personne non visible"

        if self.alert and event is None:
            self.label = "ALERTE : CHUTE POSSIBLE"
        return event


# ---------------- Programme principal ----------------
def open_video_source(source):
    """Ouvre une video ou essaie plusieurs index de webcam sous Windows."""
    if not isinstance(source, int):
        cap = cv2.VideoCapture(source)
        if cap.isOpened():
            print(f"[Video] Fichier/source ouvert : {source}")
            return cap
        cap.release()
        print(f"[Erreur] Impossible d'ouvrir la video : {source}")
        print("Verifie le chemin et le nom exact du fichier.")
        return None

    indices = (source,) + tuple(i for i in CAMERA_INDICES if i != source)
    for index in indices:
        if os.name == "nt":
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(index, cv2.CAP_MSMF)
        else:
            cap = cv2.VideoCapture(index)

        if cap.isOpened():
            ok, frame = cap.read()
            if ok and frame is not None:
                print(f"[Camera] Webcam ouverte avec l'index {index}.")
                return cap
        cap.release()
        print(f"[Camera] Index {index} indisponible, essai suivant...")

    print("[Erreur] Aucune webcam n'a pu etre ouverte (indices essayes : "
          + ", ".join(map(str, indices)) + ").")
    print("Ferme les applications utilisant la camera et verifie les autorisations Windows.")
    print("Si tu veux tester un enregistrement, configure SOURCE avec le chemin complet du fichier video.")
    return None


def pick_person(result, width, height, human_boxes):
    """Choisit une pose credible qui chevauche une detection YOLO de classe person."""
    if result.boxes is None or len(result.boxes) == 0:
        return None, []
    if result.keypoints is None:
        return None, list(result.boxes.xyxy.cpu().numpy())

    boxes = result.boxes.xyxy.cpu().numpy()
    points = result.keypoints.xy.cpu().numpy()
    scores = result.keypoints.conf.cpu().numpy()

    # Le modele de pose peut inventer des points sur des vetements ou une chaise.
    # On exige donc une detection independante de la classe COCO "person".
    def supported_by_person(pose_box):
        x0, y0, x1, y1 = map(float, pose_box)
        pose_area = max((x1 - x0) * (y1 - y0), 1.0)
        for human_box in human_boxes:
            hx0, hy0, hx1, hy1 = map(float, human_box)
            ix0, iy0 = max(x0, hx0), max(y0, hy0)
            ix1, iy1 = min(x1, hx1), min(y1, hy1)
            intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
            if intersection / pose_area >= MIN_PERSON_OVERLAP:
                return True
        return False

    candidates, ignored = [], []
    for i, b in enumerate(boxes):
        # Premier filtre : la boite de pose doit correspondre a une vraie
        # detection "person" du second modele, sinon elle est ignoree.
        if not supported_by_person(b):
            ignored.append(b)
            continue

        # Filtrer les silhouettes d'objets/vetements qui ne presentent pas
        # assez de points anatomiques coherents.
        visible_total = int(np.count_nonzero(scores[i] >= KP_CONF))
        body_count = int(np.count_nonzero(scores[i][5:17] >= KP_CONF))
        shoulder_count = int(np.count_nonzero(scores[i][5:7] >= KP_CONF))
        hip_count = int(np.count_nonzero(scores[i][11:13] >= KP_CONF))
        lower_body_count = int(np.count_nonzero(scores[i][13:17] >= KP_CONF))

        credible_pose = (
            visible_total >= 5
            and body_count >= MIN_BODY_POINTS
            and shoulder_count >= 1
            and hip_count >= 1
            and lower_body_count >= 1
        )
        if not credible_pose:
            ignored.append(b)
            continue

        # Verification geometrique supplementaire : YOLO Pose peut parfois
        # attribuer des points de corps a des vetements poses sur une chaise.
        # On verifie que les epaules et les hanches forment un torse coherent.
        # Ce filtre reste tolerant aux corps partiellement visibles au sol.
        kp = points[i]
        kp_conf = scores[i]
        shoulder_ids = [j for j in (5, 6) if kp_conf[j] >= KP_CONF]
        hip_ids = [j for j in (11, 12) if kp_conf[j] >= KP_CONF]
        lower_ids = [j for j in (13, 14, 15, 16) if kp_conf[j] >= KP_CONF]

        if not shoulder_ids or not hip_ids or not lower_ids:
            ignored.append(b)
            continue

        shoulder_mid = np.mean(kp[shoulder_ids], axis=0)
        hip_mid = np.mean(kp[hip_ids], axis=0)
        torso_len = float(np.linalg.norm(shoulder_mid - hip_mid))
        box_w = max(float(b[2] - b[0]), 1.0)
        box_h = max(float(b[3] - b[1]), 1.0)
        box_diag = math.hypot(box_w, box_h)

        # Torse presque nul ou disproportionne : pose probablement peu fiable.
        # Les limites sont relatives a la boite pour fonctionner a differentes distances.
        if torso_len < 0.12 * box_diag or torso_len > 0.85 * box_diag:
            ignored.append(b)
            continue

        # Au moins un point du bas du corps doit se trouver raisonnablement
        # pres de la boite detectee (une marge autorise les erreurs de YOLO).
        x0, y0, x1, y1 = map(float, b)
        margin_x, margin_y = 0.15 * box_w, 0.15 * box_h
        lower_inside = any(
            x0 - margin_x <= kp[j][0] <= x1 + margin_x
            and y0 - margin_y <= kp[j][1] <= y1 + margin_y
            for j in lower_ids
        )
        if not lower_inside:
            ignored.append(b)
            continue

        candidates.append(i)

    if not candidates:
        return None, ignored

    areas = [(boxes[i][2] - boxes[i][0]) * (boxes[i][3] - boxes[i][1])
             for i in candidates]
    k = candidates[int(np.argmax(areas))]
    return (boxes[k], points[k], scores[k]), ignored


def main():
    from ultralytics import YOLO   # import ici pour pouvoir tester la logique seule

    model = YOLO(MODEL_NAME)
    person_model = YOLO(PERSON_MODEL_NAME)
    webcam = isinstance(SOURCE, int)
    cap = open_video_source(SOURCE)
    if cap is None:
        return

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(fps) or fps <= 0:
        fps = 30.0

    monitor = FallMonitor()
    sender = ThreadPoolExecutor(max_workers=1)
    start = time.monotonic()
    frame_number = 0
    last_sent = float("-inf")
    last_test = float("-inf")

    print("Q : quitter | R : reinitialiser | T : test Telegram")
    print("Filtres actifs : detection independante de personne + pose anatomique + coherence du torse (sans miroir).")
    print("Confirmation : posture au sol credible pendant 1.5 s, meme si la derniere posture debout est plus ancienne.")
    check_telegram()

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("Fin de video ou camera indisponible.")
                break

            now = time.monotonic() - start if webcam else frame_number / fps
            frame_number += 1
            height, width = frame.shape[:2]

            result = model.predict(frame, conf=DETECT_CONF, verbose=False)[0]
            person_result = person_model.predict(
                frame, conf=PERSON_CONF, classes=[0], verbose=False
            )[0]
            if person_result.boxes is not None and len(person_result.boxes) > 0:
                human_boxes = person_result.boxes.xyxy.cpu().numpy()
            else:
                human_boxes = np.empty((0, 4), dtype=float)

            person, ignored = pick_person(
                result, width, height, human_boxes
            )

            info = None
            if person is not None:
                box, pts, scr = person
                info = analyse(pts, scr, box, width, height)

            event = monitor.update(info, now)

            # ---- dessin ----
            for b in ignored:
                cv2.rectangle(frame, (int(b[0]), int(b[1])),
                              (int(b[2]), int(b[3])), (150, 150, 150), 1)
                cv2.putText(frame, "ignore : pose faible/personne non confirmee",
                            (int(b[0]), max(15, int(b[1]) - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.40, (150, 150, 150), 1)

            if person is not None:
                box, pts, scr = person
                if monitor.alert:
                    color = (0, 0, 255)
                elif info["lying"]:
                    color = (0, 165, 255)
                else:
                    color = (0, 255, 0)
                cv2.rectangle(frame, (int(box[0]), int(box[1])),
                              (int(box[2]), int(box[3])), color, 2)
                for p, s in zip(pts, scr):
                    if s >= KP_CONF:
                        cv2.circle(frame, (int(p[0]), int(p[1])), 4,
                                   (0, 255, 255), -1)
                if info["angle"] is not None:
                    cv2.putText(frame, f"Buste : {info['angle']:.0f} deg",
                                (15, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                                (255, 255, 255), 2)

            head_color = (0, 0, 255) if monitor.alert else (0, 255, 0)
            cv2.putText(frame, monitor.label, (15, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, head_color, 2)
            if monitor.alert:
                cv2.putText(frame, monitor.reason[:70], (15, 95),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
            cv2.putText(frame, "Q : quitter | R : reinitialiser | T : test Telegram",
                        (15, height - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        (255, 255, 255), 2)

            # ---- notifications (apres dessin, pour que la photo montre l'alerte) ----
            if event is not None:
                kind, reason = event
                if kind == "alert":
                    # FallMonitor ne genere qu'un evenement par episode.
                    # Envoyer chaque nouvel episode : ne pas perdre une 2e chute
                    # simplement parce qu'elle survient moins de 60 s apres la 1re.
                    last_sent = time.monotonic()
                    encoded, buf = cv2.imencode(".jpg", frame)
                    photo = buf.tobytes() if encoded else None
                    print("[Alerte] Chute detectee : envoi du message Telegram...")
                    sender.submit(notify_fall, reason, photo)
                else:
                    # Une nouvelle chute apres le retour debout doit pouvoir
                    # declencher une nouvelle notification immediatement.
                    last_sent = float("-inf")
                    sender.submit(notify_recovered)

            cv2.imshow(WINDOW, frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("r"):
                monitor.reset()
                print("Alertes reinitialisees.")
            if key == ord("t"):
                t = time.monotonic()
                if t - last_test >= 5:
                    sender.submit(send_telegram,
                                  f"TEST : connexion Telegram\nLieu : {LIEU}")
                    last_test = t

            try:
                if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    break
            except cv2.error:
                break

    except KeyboardInterrupt:
        print("\nArret demande.")
    finally:
        cap.release()
        cv2.destroyAllWindows()
        print("Fermeture, attente des envois en cours...")
        sender.shutdown(wait=True)


if __name__ == "__main__":
    main()