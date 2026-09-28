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
    de mandar esto por red desde otra máquina.
    """

    def __init__(self, network_interface):
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

        ChannelFactoryInitialize(0, network_interface)
        self.client = LocoClient()
        self.client.SetTimeout(10.0)
        self.client.Init()
        # TODO: verificar estos nombres de método contra el ejemplo real del
        # SDK instalado (algo como g1_loco_client_example.py) — pueden variar
        # entre versiones. Esto resuelve el pendiente del checklist de
        # hardware "servicio de locomoción responde".

    def move(self, vx, vy, vyaw):
        self.client.Move(vx, vy, vyaw)

    def close(self):
        self.client.Move(0.0, 0.0, 0.0)
        try:
            self.client.Damp()
        except Exception:
            pass


def rate_limit(objetivo, actual, max_delta):
    delta = max(-max_delta, min(max_delta, objetivo - actual))
    return actual + delta


class WebcamSource:
    """Cualquier cámara V4L2/UVC genérica, por índice de /dev/video*. Default en el Mac."""

    def __init__(self, index):
        self.cap = cv2.VideoCapture(index)

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
    parser.add_argument("--camera", type=int, default=0,
                         help="Índice de /dev/video* (solo con --camera-backend webcam).")
    parser.add_argument(
        "--camera-backend", choices=["webcam", "realsense"], default="webcam",
        help="webcam (default): cv2.VideoCapture por índice, para probar en el Mac. "
             "realsense: D435i vía pyrealsense2 — usar en el robot.",
    )
    parser.add_argument(
        "--no-display", action="store_true",
        help="No abrir ventana de video (para correr por ssh sin pantalla).",
    )
    args = parser.parse_args()

    if args.backend == "robot" and not args.iface:
        parser.error("--backend robot necesita --iface (la interfaz de red del robot)")

    backend = UnitreeBackend(args.iface) if args.backend == "robot" else DryRunBackend()

    model = YOLO("yolov8s-pose.pt")
    cap = RealSenseSource() if args.camera_backend == "realsense" else WebcamSource(args.camera)

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
