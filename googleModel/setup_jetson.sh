#!/usr/bin/env bash
# Prepara un venv para correr script.py en el computador de a bordo del G1
# (PC2 o Thor Backpack). Correr ESTO EN EL ROBOT, no en el Mac ni el portátil.
#
# La PC2 confirmada (2026-09-28) NO TIENE SALIDA A INTERNET y es una imagen
# "minimizada" de Ubuntu (lo dice el aviso de login) — le faltan paquetes que
# Ubuntu normal trae de fábrica, como el soporte de venv. Por eso este script
# no puede hacer un "pip install" normal de nada: cada cosa que falte hay que
# bajarla en OTRA máquina con internet y pasarla por scp.
set -euo pipefail

echo "== Identificando la placa =="
if [ -f /etc/nv_tegra_release ]; then
    cat /etc/nv_tegra_release
else
    echo "No se encontró /etc/nv_tegra_release — ¿esto es una Jetson?"
fi
dpkg-query -W nvidia-l4t-core 2>/dev/null || echo "nvidia-l4t-core no encontrado"
lsb_release -a 2>/dev/null || true

echo ""
echo "Si esto es la PC2 (Jetson Orin NX): ya la confirmamos por SSH directo el"
echo "2026-09-28 — Ubuntu 20.04.6 LTS, nvidia-l4t-core 35.3.1 = JetPack 5.1.1,"
echo "Python del sistema 3.8. Si el comando de arriba no imprime justo eso,"
echo "esto no es la PC2 (probablemente el Thor Backpack, JetPack 7 — versión"
echo "de torch distinta a la de abajo)."

echo ""
echo "== Creando venv =="
if ! python3 -m venv .venv-jetson 2>/tmp/venv_setup_err; then
    if grep -q "ensurepip is not available" /tmp/venv_setup_err; then
        cat <<'MSG'
Falta el paquete python3.8-venv (normal en esta imagen "minimizada"). Como
el robot no tiene internet, hay que instalarlo desde afuera:

  1. En el laptop (con internet), abre:
       https://packages.ubuntu.com/focal/arm64/python3.8-venv
     y descarga el .deb ahí listado (arquitectura arm64, Ubuntu 20.04/focal).
  2. Pásalo al robot:
       scp python3.8-venv_*.deb unitree@192.168.123.164:~/
  3. En el robot:
       sudo dpkg -i python3.8-venv_*.deb
  4. Vuelve a correr este script.
MSG
        exit 1
    fi
    cat /tmp/venv_setup_err
    exit 1
fi
source .venv-jetson/bin/activate
echo "venv OK"

echo ""
echo "== Dependencias de requirements-jetson.txt (sin internet, desde wheels transferidas) =="
if [ ! -d wheels-jetson ]; then
    cat <<'MSG'
No encuentro una carpeta wheels-jetson/ aquí todavía. Hay que armarla en una
máquina CON internet, apuntando exactamente a la arquitectura y Python del
robot (aarch64, Python 3.8):

  pip download \
    --platform manylinux2014_aarch64 \
    --python-version 38 \
    --only-binary=:all: \
    --dest wheels-jetson \
    -r requirements-jetson.txt

  # Si algún paquete no tiene wheel manylinux para aarch64 (pasa seguido con
  # scipy, pandas, etc.), reintenta esa misma línea agregando:
  #   --index-url https://www.piwheels.org/simple
  # piwheels.org precompila wheels para ARM que PyPI a veces no tiene.

Después, pásala al robot:
  scp -r wheels-jetson unitree@192.168.123.164:~/googleModel/

Y vuelve a correr este script.
MSG
    exit 1
fi
pip install --no-index --find-links=wheels-jetson -r requirements-jetson.txt
echo "Dependencias instaladas desde wheels-jetson/"

echo ""
echo "== IMPORTANTE: instalar torch a mano =="
echo "Para JetPack 5.1.1 / Python 3.8 (cp38) / aarch64: en una máquina con"
echo "internet, busca en el hilo oficial de NVIDIA 'PyTorch for Jetson' la"
echo "wheel de la carpeta jp/v511/pytorch etiquetada cp38. Bájala, pásala por"
echo "scp, e instálala aquí (offline, es solo un archivo local) con:"
echo "  pip install /ruta/al/torch-*.whl"
echo ""
echo "Verifica después con:"
echo "  python3 -c 'import torch; print(torch.__version__, torch.cuda.is_available())'"
echo "Debe imprimir True. Si dice False, la wheel no es la de CUDA."
read -p "Presiona Enter cuando ya hayas instalado el torch correcto..."

echo ""
echo "== Verificación final =="
python3 -c "import torch; print('torch', torch.__version__, '| CUDA disponible:', torch.cuda.is_available())"
python3 -c "import cv2; print('opencv', cv2.__version__)"
python3 -c "from ultralytics import YOLO; print('ultralytics OK')"
python3 -c "import pyrealsense2; print('pyrealsense2 OK')" || echo "pyrealsense2 no instalado todavía — ver nota abajo"

echo ""
echo "== Buscando dispositivos de video (para ubicar la D435i) =="
v4l2-ctl --list-devices 2>/dev/null || echo "v4l2-ctl no instalado — bájalo igual que python3.8-venv (paquete v4l-utils, .deb arm64/focal en packages.ubuntu.com) y pásalo por scp."

echo ""
echo "Nota sobre pyrealsense2: normalmente hay que compilar librealsense con"
echo "soporte CUDA para esta versión exacta de L4T (35.3.1) — sin internet en"
echo "el robot esto también implica compilar o transferir binarios desde otra"
echo "máquina. Revisa la guía de Intel RealSense para Jetson/L4T 35.3.x."
