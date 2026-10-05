import argparse
import math
import time

import cv2
import numpy as np
from ultralytics import YOLO

# --- VARIABLES DE DISTANCIA (Adelante/Atras) ---
UMBRAL_2M = 0.16
MARGEN_DISTANCIA = 0.04

# --- VARIABLES DE ROTACIÓN (Izquierda/Derecha) ---
# 0.10 significa que hay un 10% de tolerancia hacia la izquierda y
# 10% hacia la derecha donde el robot considerará que estás "CENTRADO".
MARGEN_CENTRO = 0.10

# --- VELOCIDADES DE COMANDO ---
VX_ADELANTE = 0.25   # m/s
VX_ATRAS = 0.15      # m/s, más lento hacia atrás: el robot no ve lo que tiene detrás
VYAW_GIRO = 0.35      # rad/s
# Signo asumido: +yaw = giro a la izquierda (convención habitual). Verificar
# contra el SDK real en modo dryrun antes de correr esto en el robot — si
# está al revés, el robot gira hacia el lado contrario al que debería.

# --- LÍMITES DE ACELERACIÓN (sección 05 del documento de arquitectura) ---
DVX_MAX_ACCEL = 0.15     # m/s^2
DVYAW_MAX_ACCEL = 0.3    # rad/s^2, valor de arranque, ajustar en pruebas

# --- WATCHDOG: si no se ve a la persona, parar sin importar el último comando ---
TIEMPO_SIN_PERSONA_PARA_PARAR = 0.4  # segundos


class DryRunBackend:
    """Backend de desarrollo: no mueve nada, solo imprime el comando. Default en el Mac."""

    def move(self, vx, vy, vyaw):
        print(f"[DRYRUN] Move(vx={vx:+.2f}, vy={vy:+.2f}, vyaw={vyaw:+.2f})")

    def close(self):
        print("[DRYRUN] Move(0, 0, 0) + Damp()")


class UnitreeBackend:
    """
    Backend real: manda comandos al G1 vía LocoClient. Solo tiene sentido
    corriendo en el propio computador de a bordo del robot — no hay forma
    de mandar esto por red desde otra máquina. Asume que ChannelFactoryInitialize
    ya se llamó (una sola vez por proceso, en main()) antes de construir esto.
    """

    def __init__(self):
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

        self.client = LocoClient()
        self.client.SetTimeout(10.0)
        self.client.Init()
        # Confirmado 2026-09-28 contra el SDK real en la PC2
        # (unitree_sdk2_python/example/g1/high_level/g1_loco_client_example.py):
        # Move(vx, vy, vyaw) llama a SetVelocity(..., duration=1.0) por
        # default. Cada comando solo es válido 1s salvo continous_move=True,
        # que dejamos en False a propósito — así, si este proceso se cuelga
        # sin llegar a close(), el robot se detiene solo al segundo
        # siguiente en vez de seguir indefinidamente con el último comando.

    def move(self, vx, vy, vyaw):
        self.client.Move(vx, vy, vyaw)

    def close(self):
        self.client.StopMove()
        try:
            self.client.Damp()
        except Exception:
            pass


def rate_limit(objetivo, actual, max_delta):
    delta = max(-max_delta, min(max_delta, objetivo - actual))
    return actual + delta


class WebcamSource:
    """Cualquier cámara V4L2/UVC genérica. Default en el Mac."""

    def __init__(self, camera):
        # Acepta índice numérico ("0") o ruta de dispositivo ("/dev/video4").
        # Con varios nodos /dev/video* del mismo sensor (típico en la D435i:
        # color, profundidad e infrarrojo exponen cada uno el suyo), el
        # índice entero de OpenCV no coincide con el número real del
        # dispositivo — de hecho para cámaras tipo RealSense, OpenCV lo
        # interpreta con un backend especial ("obsensor") en vez de abrir
        # /dev/videoN tal cual. Pasar la ruta evita esa ambigüedad.
        try:
            camera = int(camera)
        except ValueError:
            pass
        self.cap = cv2.VideoCapture(camera)

    def read(self):
        return self.cap.read()

    def release(self):
        self.cap.release()


class RealSenseSource:
    """
    Stream de color de la D435i vía pyrealsense2. Usar esto en vez de
    WebcamSource en el robot — con un índice de /dev/video* genérico no hay
    garantía de agarrar el nodo RGB correcto (la D435i expone varios: color,
    profundidad, IR). No usa el canal de profundidad todavía: la distancia
    sigue viniendo del ancho de hombros, no de esta cámara.
    """

    def __init__(self, width=640, height=480, fps=30):
        import pyrealsense2 as rs

        self.pipeline = rs.pipeline()
        config = rs.config()
        config.enable_stream(rs.stream.color, width, height, rs.format.bgr8, fps)
        self.pipeline.start(config)

    def read(self):
        frames = self.pipeline.wait_for_frames()
        color = frames.get_color_frame()
        if not color:
            return False, None
        return True, np.asanyarray(color.get_data())

    def release(self):
        self.pipeline.stop()


class VideoHubSource:
    """
    Frames de color vía el servicio "videohub" del propio robot (DDS/RPC) —
    confirmado con hardware real 2026-10-07. NO usar WebcamSource con un
    /dev/video* de la D435i: ese dispositivo está permanentemente ocupado
    por /unitree/module/video_hub_pc4/videohub_pc4, un servicio de fábrica
    que arranca solo y nunca lo suelta. Este es el camino correcto, y no
    toca el dispositivo crudo en absoluto.

    GetImageSample() es un poll por RPC (pedir-y-recibir), no una
    suscripción a stream continuo — se llama una vez por fotograma, igual
    que cap.read() en las otras fuentes. Devuelve JPEG como lista de
    enteros (no bytes), de ahí el np.asarray antes de cv2.imdecode.

    Asume que ChannelFactoryInitialize ya se llamó en main().
    """

    def __init__(self, timeout=3.0):
        from unitree_sdk2py.go2.video.video_client import VideoClient

        self.client = VideoClient()
        self.client.SetTimeout(timeout)
        self.client.Init()

    def read(self):
        code, data = self.client.GetImageSample()
        if code != 0 or not data:
            return False, None
        arr = np.asarray(data, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return frame is not None, frame

    def release(self):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend", choices=["dryrun", "robot"], default="dryrun",
        help="dryrun (default): solo imprime. robot: manda comandos reales al "
             "G1 — usar solo corriendo en el propio robot.",
    )
    parser.add_argument(
        "--iface", default=None,
        help="Interfaz de red DDS del robot (requerido con --backend robot). Ej: eth0",
    )
    parser.add_argument("--camera", default="0",
                         help="Índice ('0') o ruta de dispositivo ('/dev/video4') — "
                              "solo con --camera-backend webcam. En Linux con varios "
                              "/dev/video* del mismo sensor, usar la ruta es más confiable.")
    parser.add_argument(
        "--camera-backend", choices=["webcam", "realsense", "videohub"], default="webcam",
        help="webcam (default): cv2.VideoCapture por índice, para probar en el Mac. "
             "realsense: D435i vía pyrealsense2 (sin confirmar en este robot). "
             "videohub: vía el servicio DDS del propio robot — la forma que "
             "sí funciona en la PC2, confirmada con hardware real.",
    )
    parser.add_argument(
        "--no-display", action="store_true",
        help="No abrir ventana de video (para correr por ssh sin pantalla).",
    )
    args = parser.parse_args()

    needs_dds = args.backend == "robot" or args.camera_backend == "videohub"
    if needs_dds and not args.iface:
        parser.error("--backend robot y --camera-backend videohub necesitan "
                     "--iface (la interfaz de red del robot)")

    if needs_dds:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        # Una sola vez por proceso — tanto UnitreeBackend como VideoHubSource
        # asumen que ya se llamó esto antes de construirse.
        ChannelFactoryInitialize(0, args.iface)

    backend = UnitreeBackend() if args.backend == "robot" else DryRunBackend()

    model = YOLO("yolov8s-pose.pt")
    if args.camera_backend == "videohub":
        cap = VideoHubSource()
    elif args.camera_backend == "realsense":
        cap = RealSenseSource()
    else:
        cap = WebcamSource(args.camera)

    vx_actual, vyaw_actual = 0.0, 0.0
    tiempo_anterior = time.time()
    tiempo_ultima_deteccion = time.time()

    try:
        while True:
            success, frame = cap.read()
            if not success:
                break

            frame = cv2.flip(frame, 1)
            results = model(frame, verbose=False)

            vx_objetivo, vyaw_objetivo = 0.0, 0.0
            estado_distancia = "Buscando persona..."
            estado_giro = "ESPERANDO"
            persona_visible = False

            if results[0].keypoints is not None and len(results[0].keypoints.xyn) > 0:
                kpts = results[0].keypoints.xyn[0]

                if len(kpts) > 6:
                    x_izq, y_izq = float(kpts[5][0]), float(kpts[5][1])
                    x_der, y_der = float(kpts[6][0]), float(kpts[6][1])

                    if x_izq > 0 and x_der > 0:
                        persona_visible = True

                        # 1. LÓGICA DE DISTANCIA
                        ancho_hombros = math.sqrt((x_izq - x_der) ** 2 + (y_izq - y_der) ** 2)

                        if ancho_hombros < (UMBRAL_2M - MARGEN_DISTANCIA):
                            estado_distancia = "CAMINANDO HACIA ADELANTE"
                            vx_objetivo = VX_ADELANTE
                        elif ancho_hombros > (UMBRAL_2M + MARGEN_DISTANCIA):
                            estado_distancia = "CAMINANDO HACIA ATRAS"
                            vx_objetivo = -VX_ATRAS
                        else:
                            estado_distancia = "IDLE (Distancia)"

                        # 2. LÓGICA DE ROTACIÓN (punto medio entre los hombros)
                        centro_persona_x = (x_izq + x_der) / 2.0

                        if centro_persona_x < (0.5 - MARGEN_CENTRO):
                            estado_giro = "GIRANDO A LA IZQUIERDA"
                            vyaw_objetivo = VYAW_GIRO
                        elif centro_persona_x > (0.5 + MARGEN_CENTRO):
                            estado_giro = "GIRANDO A LA DERECHA"
                            vyaw_objetivo = -VYAW_GIRO
                        else:
                            estado_giro = "CENTRADO"

            if persona_visible:
                tiempo_ultima_deteccion = time.time()
            elif time.time() - tiempo_ultima_deteccion >= TIEMPO_SIN_PERSONA_PARA_PARAR:
                vx_objetivo, vyaw_objetivo = 0.0, 0.0

            ahora = time.time()
            dt = max(ahora - tiempo_anterior, 1e-3)
            tiempo_anterior = ahora
            vx_actual = rate_limit(vx_objetivo, vx_actual, DVX_MAX_ACCEL * dt)
            vyaw_actual = rate_limit(vyaw_objetivo, vyaw_actual, DVYAW_MAX_ACCEL * dt)

            backend.move(vx_actual, 0.0, vyaw_actual)
            print(f"Motor Avance: {estado_distancia} | Motor Giro: {estado_giro} "
                  f"| vx={vx_actual:+.2f} vyaw={vyaw_actual:+.2f}")

            if not args.no_display:
                annotated_frame = results[0].plot()
                cv2.putText(annotated_frame, f"Distancia: {estado_distancia}", (20, 80),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.putText(annotated_frame, f"Rotacion: {estado_giro}", (20, 110),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2)
                cv2.imshow("Robot Tracking", annotated_frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        backend.close()
        cap.release()
        if not args.no_display:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
