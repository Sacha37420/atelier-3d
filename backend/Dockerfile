# atelier-3d — backend Django/Celery + COLMAP/OpenMVS (CPU-only, aucun CUDA).
#
# Le pipeline COLMAP+OpenMVS et ses 3 correctifs viennent tels quels du spike
# technique Lot 0 (dev/atelier-3d-spike/, voir aussi la mémoire
# atelier-3d-lot0-spike-findings) : temps réel mesuré sur le matériel cible
# (2 vCPU, 16 Go, sans AVX), ~18 min pour une reconstruction de 18 photos.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# --- Dépendances COLMAP (liste exacte du Dockerfile officiel du tag 3.11.1) ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake ninja-build build-essential ca-certificates \
    libboost-program-options-dev libboost-graph-dev libboost-system-dev \
    libeigen3-dev libflann-dev libfreeimage-dev libmetis-dev \
    libgoogle-glog-dev libgtest-dev libsqlite3-dev libglew-dev \
    qtbase5-dev libqt5opengl5-dev \
    libcgal-dev libceres-dev \
    && rm -rf /var/lib/apt/lists/*

# --- COLMAP (SfM, CPU-only) ---
RUN git clone --branch 3.11.1 --depth 1 https://github.com/colmap/colmap.git /opt/colmap && \
    cmake -S /opt/colmap -B /opt/colmap/build -GNinja \
        -DCUDA_ENABLED=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local && \
    cmake --build /opt/colmap/build -j"$(nproc)" && \
    cmake --install /opt/colmap/build && \
    rm -rf /opt/colmap

# --- Dépendances OpenMVS (mêmes paquets que docker/buildInDocker.sh officiel) ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpng-dev libjpeg-dev libtiff-dev libglu1-mesa-dev libglew-dev libglfw3-dev \
    libboost-iostreams-dev libboost-serialization-dev libboost-thread-dev \
    libopencv-dev libgmp-dev libmpfr-dev zlib1g-dev python3-dev \
    libnanoflann-dev \
    && rm -rf /var/lib/apt/lists/*

# Eigen 3.4 (buildInDocker.sh recompile sa propre version plutôt que d'utiliser l'apt system)
RUN git clone --depth 1 https://gitlab.com/libeigen/eigen --branch 3.4 /opt/eigen && \
    cmake -S /opt/eigen -B /opt/eigen/build && \
    cmake --build /opt/eigen/build -j"$(nproc)" --target install && \
    rm -rf /opt/eigen

# CGAL v6.0.1 (idem, requis par OpenMVS)
RUN git clone --depth 1 https://github.com/cgal/cgal --branch v6.0.1 /opt/cgal && \
    cmake -S /opt/cgal -B /opt/cgal/build && \
    cmake --build /opt/cgal/build -j"$(nproc)" --target install && \
    rm -rf /opt/cgal

# VCGlib (dépendance header-only d'OpenMVS)
RUN git clone --depth 1 https://github.com/cdcseacave/VCG.git /opt/vcglib

# --- OpenMVS (MVS : densify/mesh/texture, CPU-only) ---
# Fixé sur le tag v2.3.0 : master/develop exigent libjxl + cv::IMWRITE_JPEGXL_QUALITY,
# absent de l'OpenCV d'Ubuntu 24.04 (4.6) → échec de compilation (cf. Lot 0).
# Correctif : Util::formatTime() (libs/Common/Util.h) passe la taille totale du buffer (128)
# à chaque appel _sntprintf(buf+len, 128, ...) au lieu de l'espace restant (128-len). Inoffensif
# sur la plupart des toolchains, mais Ubuntu compile par défaut avec _FORTIFY_SOURCE=2, qui détecte
# l'incohérence et abort() le process ("*** buffer overflow detected ***") — reproduit et diagnostiqué
# via gdb : le crash survient dans DensifyPointCloud, dans EstimateDepthMap -> formatTime, dès qu'un
# temps écoulé/ETA est formaté (donc systématique dès qu'une vraie scène est densifiée).
RUN git clone --branch v2.3.0 --depth 1 https://github.com/cdcseacave/openMVS.git /opt/openMVS && \
    sed -i 's/_sntprintf(buf+len, 128,/_sntprintf(buf+len, 128-len,/g' /opt/openMVS/libs/Common/Util.h && \
    cmake -S /opt/openMVS -B /opt/openMVS/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DVCG_ROOT=/opt/vcglib \
        -DOpenMVS_USE_CUDA=OFF \
        -DOpenMVS_BUILD_VIEWER=OFF \
        -DOpenMVS_USE_BREAKPAD=OFF \
        -DOpenMVS_USE_PYTHON=OFF \
        -DOpenMVS_ENABLE_TESTS=OFF \
        -DCMAKE_INSTALL_PREFIX=/usr/local && \
    cmake --build /opt/openMVS/build -j"$(nproc)" && \
    cmake --install /opt/openMVS/build && \
    rm -rf /opt/openMVS /opt/vcglib

ENV PATH="/usr/local/bin/OpenMVS:${PATH}"

# --- ffmpeg (extraction de frames vidéo, Lot 1) + Python 3.12 + libs GL/X headless
#     (requises par pymeshlab même sans affichage) + python-is-python3 pour
#     que `python manage.py` fonctionne comme sur le reste du lab ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg python3.12 python3-pip python-is-python3 \
    libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# --- Segmentation 2D zero-shot (Lot 4, module Bâtiments) : FastSAM (ultralytics),
#     torch/torchvision CPU-only. Wheel dédiée download.pytorch.org, PAS le
#     wheel PyPI par défaut (embarque CUDA — inutile et bien plus volumineux
#     sur ce matériel). Validé avant d'écrire ce module (cf. facade.py) : torch
#     tourne sans crash sur le CPU cible (contrairement à open3d, Lot 2). curl +
#     libxcb1 : requis par opencv-python (dépendance d'ultralytics), absent des
#     libs GL headless déjà installées ci-dessus.
RUN apt-get update && apt-get install -y --no-install-recommends curl libxcb1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --break-system-packages \
    torch==2.13.0 torchvision==0.28.0 --index-url https://download.pytorch.org/whl/cpu

# Poids FastSAM-s (~23 Mo) téléchargés une fois au build, PAS au runtime d'un
# job (cf. facade.py : un job de plusieurs heures ne doit pas pouvoir échouer
# sur un flake réseau pour une ressource déjà disponible dans l'image, même
# logique que COLMAP/OpenMVS compilés au build, Lot 0).
RUN mkdir -p /opt/fastsam && curl -sSL --fail -o /opt/fastsam/FastSAM-s.pt \
    https://github.com/ultralytics/assets/releases/download/v8.4.0/FastSAM-s.pt

# ──────────────────────────────────────────────────────────────────────────────
# Lot 5 — Conception CAO manuelle : build123d (noyau OCCT) + cq_gears (denture)
# + FreeCAD >= 1.0 headless (Assembly/OndselSolver). Validé au spike Lot 5.0
# (dev/atelier-3d-spike/lot5/, voir aussi la mémoire atelier-3d-lot5-spike-findings).
# ──────────────────────────────────────────────────────────────────────────────

# cadquery embarque cadquery-ocp (bindings OCCT avec VTK). build123d dépend par
# défaut de cadquery-ocp-novtk (même OCCT, sans VTK) — installer les deux
# provoquerait une collision de fichiers dans le même site-packages (module OCP).
# On installe cadquery en premier (pour cq_gears), puis build123d en --no-deps
# pour réutiliser l'OCP déjà en place — vérifié au spike : l'import fonctionne
# malgré la variante VTK, aucun repli par export/import STEP nécessaire (cq_gears
# génère directement une forme OCCT intégrable telle quelle dans build123d).
RUN pip install --no-cache-dir --break-system-packages cadquery && \
    pip install --no-cache-dir --break-system-packages \
    "git+https://github.com/meadiode/cq_gears.git" && \
    pip install --no-cache-dir --break-system-packages --no-deps build123d && \
    pip install --no-cache-dir --break-system-packages \
    typing_extensions "ezdxf>=1.1.0" anytree svgpathtools ipython \
    ocpsvg ocp_gordon trianglesolver sympy scikit-learn webcolors requests lib3mf

# --- FreeCAD >= 1.0 headless (workbench Assembly/OndselSolver, Job CAD_ASSEMBLE) :
# le paquet apt d'Ubuntu 24.04 est antérieur à 1.0 (pas de workbench Assembly) —
# AppImage officielle requise. Extraite au build (--appimage-extract, ne
# nécessite PAS de FUSE/--device : vérifié au spike, fonctionne en sandbox Docker
# sans /dev/fuse) plutôt que stockée + extraite au runtime : le worker Celery est
# un process long-lived, l'extraction une fois pour toutes au build évite ~20-30s
# de coût à chaque (re)démarrage du container. Invoquée en sous-processus
# (`freecadcmd`, cf. cad_assemble.py) et jamais importée dans le process Django :
# l'AppImage embarque son propre Python 3.11, différent du 3.12 du reste du
# backend — même logique de non-réinvention/binaire externe que COLMAP/OpenMVS.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libx11-6 libxkbcommon0 libxkbcommon-x11-0 libfontconfig1 fontconfig \
    libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
    libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/*
RUN curl -sSL --fail -o /tmp/FreeCAD.AppImage \
    https://github.com/FreeCAD/FreeCAD/releases/download/1.1.3/FreeCAD_1.1.3-Linux-x86_64-py311.AppImage && \
    echo "3a853eb69ee595f779f2255dbf80a765926981d8ff68903cefee4dfb03a8f5ef  /tmp/FreeCAD.AppImage" | sha256sum -c - && \
    chmod +x /tmp/FreeCAD.AppImage && \
    cd /opt && /tmp/FreeCAD.AppImage --appimage-extract && \
    mv /opt/squashfs-root /opt/freecad && \
    rm -f /tmp/FreeCAD.AppImage
# Lien ciblé sur le seul binaire utile plutôt que d'ajouter tout /opt/freecad/usr/bin
# au PATH : ce dossier contient aussi ses propres `python`/`pip` (Python 3.11 embarqué,
# sans Django ni cadquery) qui shadowerait silencieusement le python système 3.12 du
# reste du backend si le dossier entier passait devant lui dans le PATH — reproduit
# et corrigé au Lot 5.0 (ModuleNotFoundError sur cadquery, `python` résolvait vers
# l'interpréteur embarqué de FreeCAD).
RUN ln -s /opt/freecad/usr/bin/freecadcmd /usr/local/bin/freecadcmd
ENV QT_QPA_PLATFORM=offscreen

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --no-input && python manage.py runserver 0.0.0.0:8000"]
