#!/usr/bin/env bash
# Prepara un venv para correr script.py en el computador de a bordo del G1
# (PC2 o Thor Backpack). Correr ESTO EN EL ROBOT, no en el Mac ni el portátil.
#
# Por qué no basta con "pip install -r requirements.txt" del Mac:
# ese archivo tiene el torch de PyPI genérico (build de macOS/MPS). En
# Jetson, torch con soporte CUDA viene de wheels específicas de NVIDIA para
# cada versión de JetPack/L4T — instalar el de PyPI a secas normalmente da
# una build de CPU sin aceleración, o falla directo.
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
echo "== Creando venv =="
python3 -m venv .venv-jetson
source .venv-jetson/bin/activate
pip install --upgrade pip

echo ""
echo "== IMPORTANTE: instalar torch a mano antes de seguir =="
echo "Con la versión de JetPack de arriba, ve a la guía oficial de NVIDIA"
echo "'PyTorch for Jetson' y usa la wheel que corresponda EXACTAMENTE a esa"
echo "versión de JetPack/L4T. No hardcodeo la URL acá porque cambia por"
echo "versión y se desactualiza rápido — usar la que NVIDIA publique para"
echo "esta placa específica."
echo ""
echo "Verifica después con:"
echo "  python3 -c 'import torch; print(torch.__version__, torch.cuda.is_available())'"
echo "Debe imprimir True. Si dice False, la wheel no es la de CUDA."
read -p "Presiona Enter cuando ya hayas instalado el torch correcto..."

echo ""
echo "== Instalando el resto de dependencias (sin tocar torch) =="
pip install -r requirements-jetson.txt

echo ""
echo "== Verificación final =="
python3 -c "import torch; print('torch', torch.__version__, '| CUDA disponible:', torch.cuda.is_available())"
python3 -c "import cv2; print('opencv', cv2.__version__)"
python3 -c "from ultralytics import YOLO; print('ultralytics OK')"
python3 -c "import pyrealsense2; print('pyrealsense2 OK')" || echo "pyrealsense2 no instalado todavía — ver nota abajo"

echo ""
echo "== Buscando dispositivos de video (para ubicar la D435i) =="
v4l2-ctl --list-devices 2>/dev/null || echo "v4l2-ctl no instalado: sudo apt install v4l-utils"

echo ""
echo "Nota sobre pyrealsense2: en Jetson normalmente hay que compilar"
echo "librealsense desde código fuente con soporte CUDA, no basta 'pip install"
echo "pyrealsense2' sin más. Si falla, revisar la guía de Intel RealSense para"
echo "Jetson/L4T de esa versión específica."
