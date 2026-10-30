#!/usr/bin/env bash

set -euo pipefail


# ==============================================================================
# DOMINO PYTHON DEVELOPMENT ENVIRONMENT SETUP
# ==============================================================================
#
# Purpose
# -------
# Build a persistent Python 3.12 development environment for Domino.
#
# Domino behavior:
#
#   - /mnt persists between workspace restarts
#   - the operating-system container is recreated
#   - Domino automatically activates /mnt/.venv when a terminal opens
#
# Therefore:
#
#   - uv persists under /mnt/.uv
#   - Python 3.12 persists under /mnt/.uv/python
#   - the virtual environment persists under /mnt/.venv
#   - Playwright browsers persist under /mnt/.cache/ms-playwright
#   - ephemeral Chromium OS dependencies are restored during venv activation
#
# This script is intended for:
#
#   - initial project setup
#   - intentional environment rebuilds
#   - dependency updates
#
# It is NOT intended to run every time Domino starts.
#
# ==============================================================================


# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================

PROJECT_DIR="/mnt"

PYTHON_VERSION="3.12.13"

UV_HOME="${PROJECT_DIR}/.uv"
UV_BIN_DIR="${UV_HOME}/bin"
UV_BIN="${UV_BIN_DIR}/uv"
UV_CACHE_DIR="${UV_HOME}/cache"
UV_PYTHON_INSTALL_DIR="${UV_HOME}/python"

VENV_DIR="${PROJECT_DIR}/.venv"

REQUIREMENTS_FILE="${PROJECT_DIR}/requirements.txt"
REQUIREMENTS_HASH_FILE="${VENV_DIR}/.requirements.sha256"

PLAYWRIGHT_BROWSERS_PATH="${PROJECT_DIR}/.cache/ms-playwright"

KERNEL_NAME="domino-venv"
KERNEL_DISPLAY_NAME="Python 3.12 (/mnt/.venv)"


# ==============================================================================
# 2. PERSISTENT ENVIRONMENT VARIABLES
# ==============================================================================

export UV_HOME="${UV_HOME}"
export UV_INSTALL_DIR="${UV_BIN_DIR}"
export UV_CACHE_DIR="${UV_CACHE_DIR}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR}"

# /mnt and the uv cache may be on different filesystems in Domino.
export UV_LINK_MODE="copy"

# Keep Playwright's ~650 MB browser installation under persistent /mnt.
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH}"

# Make uv available during setup.
export PATH="${UV_BIN_DIR}:${PATH}"


echo "=============================================================================="
echo "DOMINO PYTHON ENVIRONMENT SETUP"
echo "=============================================================================="
echo
echo "Project directory:        ${PROJECT_DIR}"
echo "Python version:           ${PYTHON_VERSION}"
echo "Virtual environment:      ${VENV_DIR}"
echo "uv binary:                ${UV_BIN}"
echo "uv Python directory:      ${UV_PYTHON_INSTALL_DIR}"
echo "Playwright browser cache: ${PLAYWRIGHT_BROWSERS_PATH}"
echo


# ==============================================================================
# 3. CREATE PERSISTENT DIRECTORIES
# ==============================================================================

echo "=== 3. Creating persistent directories ==="

mkdir -p \
    "${UV_BIN_DIR}" \
    "${UV_CACHE_DIR}" \
    "${UV_PYTHON_INSTALL_DIR}" \
    "${PLAYWRIGHT_BROWSERS_PATH}"

echo "Persistent directories ready."
echo


# ==============================================================================
# 4. INSTALL OR REUSE UV
# ==============================================================================

echo "=== 4. Checking uv ==="

if [ -x "${UV_BIN}" ]; then

    echo "Using existing uv:"
    "${UV_BIN}" --version

else

    echo "uv not found."
    echo "Installing uv into ${UV_BIN_DIR}..."

    curl -LsSf https://astral.sh/uv/install.sh \
        | env \
            UV_INSTALL_DIR="${UV_BIN_DIR}" \
            UV_NO_MODIFY_PATH=1 \
            sh

    echo
    echo "Installed:"
    "${UV_BIN}" --version

fi

echo


# ==============================================================================
# 5. INSTALL OR REUSE PYTHON 3.12
# ==============================================================================

echo "=== 5. Ensuring Python ${PYTHON_VERSION} is available ==="

"${UV_BIN}" python install "${PYTHON_VERSION}"

echo
echo "Managed Python:"
"${UV_BIN}" python list \
    | grep "${PYTHON_VERSION}" \
    || true

echo


# ==============================================================================
# 6. WRITE VALIDATED REQUIREMENTS
# ==============================================================================
#
# These package versions were validated together under:
#
#   Python 3.12.13
#   Domino Ubuntu 22.04
#   uv 0.12.x
#
# ==============================================================================

echo "=== 6. Writing requirements.txt ==="

cat > "${REQUIREMENTS_FILE}" <<'EOF'
# =============================================================================
# Core scientific stack
# =============================================================================

numpy==1.26.4
pandas==2.2.2
matplotlib==3.10.0
scipy==1.15.2
scikit-learn==1.7.2
statsmodels==0.14.6
seaborn==0.13.2
plotly==6.6.0
pyomo==6.10.1


# =============================================================================
# Jupyter / notebook
# =============================================================================

ipykernel==7.2.0
ipython==8.38.0
ipywidgets==8.1.8
jupyterlab==4.5.6
notebook==7.5.5
papermill==2.7.0


# =============================================================================
# Data / file formats
# =============================================================================

tqdm==4.70.0
openpyxl==3.1.5
pyarrow==23.0.1
duckdb==1.5.5


# =============================================================================
# Google Cloud / BigQuery
# =============================================================================

google-cloud-bigquery==3.40.1
google-cloud-bigquery-storage==2.36.2
google-cloud-storage==3.10.1
google-cloud-core==2.5.0
google-api-python-client==2.193.0
google-auth==2.43.0
pandas-gbq==0.30.0
db-dtypes==1.5.0
gcsfs==2026.1.0
fsspec==2026.1.0


# =============================================================================
# Geospatial
# =============================================================================

geopandas==1.1.3
shapely==2.1.2
pyogrio==0.12.1
pyproj==3.7.1
fiona==1.10.1
rasterio==1.4.4
folium==0.20.0
branca==0.8.2
contextily==1.7.0
h3==4.4.2
geopy==2.4.1


# =============================================================================
# Modeling / optimization
# =============================================================================

SQLAlchemy==2.0.48
ipython-sql==0.5.0
networkx==3.4.2
lightgbm==3.3.5
shap==0.49.1
optuna==4.9.0
pyDOE3==1.6.2
pycausalimpact==0.1.1
scikit-lego==0.9.9
detecta==0.0.5


# =============================================================================
# HTTP / scraping
# =============================================================================

requests==2.34.2
requests-oauthlib==2.0.0
python-dotenv==1.2.2
beautifulsoup4==4.14.3
playwright==1.62.0
rapidfuzz==3.14.5
backoff==2.2.1


# =============================================================================
# Runtime / schemas
# =============================================================================

protobuf==6.33.6
pydantic==2.12.5
typing_extensions==4.16.0


# =============================================================================
# MMT
# =============================================================================

em-mmt==0.3.6


# =============================================================================
# Development / testing
# =============================================================================

pytest==8.4.2
ruff==0.16.3
ripgrep==15.1.0
EOF

echo "Requirements written:"
echo "  ${REQUIREMENTS_FILE}"
echo


# ==============================================================================
# 7. DETERMINE WHETHER VENV REBUILD IS REQUIRED
# ==============================================================================

echo "=== 7. Checking virtual environment ==="

CURRENT_REQUIREMENTS_HASH="$(
    sha256sum "${REQUIREMENTS_FILE}" \
        | awk '{print $1}'
)"

REBUILD_VENV=0


# ------------------------------------------------------------------------------
# Environment missing?
# ------------------------------------------------------------------------------

if [ ! -x "${VENV_DIR}/bin/python" ]; then

    echo "Virtual environment does not exist."
    REBUILD_VENV=1

else

    CURRENT_PYTHON_VERSION="$(
        "${VENV_DIR}/bin/python" \
            -c 'import platform; print(platform.python_version())' \
            2>/dev/null \
        || true
    )"

    echo "Existing Python version: ${CURRENT_PYTHON_VERSION:-UNKNOWN}"

    if [ "${CURRENT_PYTHON_VERSION}" != "${PYTHON_VERSION}" ]; then

        echo
        echo "Python version mismatch:"
        echo "  expected: ${PYTHON_VERSION}"
        echo "  found:    ${CURRENT_PYTHON_VERSION:-UNKNOWN}"

        REBUILD_VENV=1

    fi

fi


# ------------------------------------------------------------------------------
# Requirements changed?
# ------------------------------------------------------------------------------

if [ "${REBUILD_VENV}" -eq 0 ]; then

    if [ ! -f "${REQUIREMENTS_HASH_FILE}" ]; then

        echo "Requirements hash is missing."
        REBUILD_VENV=1

    else

        SAVED_REQUIREMENTS_HASH="$(
            cat "${REQUIREMENTS_HASH_FILE}"
        )"

        if [ "${CURRENT_REQUIREMENTS_HASH}" != "${SAVED_REQUIREMENTS_HASH}" ]; then

            echo "requirements.txt has changed."
            REBUILD_VENV=1

        else

            echo "requirements.txt is unchanged."

        fi

    fi

fi

echo


# ==============================================================================
# 8. BUILD OR REUSE VIRTUAL ENVIRONMENT
# ==============================================================================

echo "=== 8. Preparing virtual environment ==="

if [ "${REBUILD_VENV}" -eq 1 ]; then

    echo "Rebuilding:"
    echo "  ${VENV_DIR}"

    rm -rf "${VENV_DIR}"

    "${UV_BIN}" venv \
        --python "${PYTHON_VERSION}" \
        "${VENV_DIR}"

    echo
    echo "Installing project dependencies..."

    "${UV_BIN}" pip install \
        --python "${VENV_DIR}/bin/python" \
        -r "${REQUIREMENTS_FILE}"

    echo "${CURRENT_REQUIREMENTS_HASH}" \
        > "${REQUIREMENTS_HASH_FILE}"

    echo
    echo "Virtual environment rebuilt successfully."

else

    echo "Existing Python environment is current."
    echo "Skipping dependency reinstall."

fi

echo


# ==============================================================================
# 8A. INSTALL AMAZON MMT PIPELINE PROJECT
# ==============================================================================
#
# Install the Git-managed project itself into the persistent virtual
# environment so notebooks and normal Python processes can import
# amazon_pipeline without relying on pytest's pythonpath configuration or
# an externally supplied PYTHONPATH.
#
# --no-deps is intentional because validated dependencies are installed from
# /mnt/requirements.txt above.
#
# Editable installation keeps imports pointed at the live repository source.
# ==============================================================================

echo "=== 8A. Installing MMM pipeline project ==="

"${UV_BIN}" pip install \
    --python "${VENV_DIR}/bin/python" \
    --no-deps \
    -e "/mnt/UniversalPicturesMMM"

echo "MMM pipeline project ready."
echo


# ==============================================================================
# 9. PATCH VENV ACTIVATION FOR DOMINO
# ==============================================================================
#
# Domino automatically activates /mnt/.venv whenever a terminal is opened.
#
# We use that behavior to restore:
#
#   - uv PATH
#   - persistent uv directories
#   - persistent Playwright browser cache
#   - Chromium's ephemeral Ubuntu dependency
#
# ==============================================================================

echo "=== 9. Configuring Domino venv activation ==="

ACTIVATE_FILE="${VENV_DIR}/bin/activate"

"${VENV_DIR}/bin/python" - <<'PY'
from pathlib import Path

activate_path = Path("/mnt/.venv/bin/activate")

text = activate_path.read_text()

start_marker = (
    "# ==============================================================================\n"
    "# DOMINO PROJECT ENVIRONMENT\n"
    "# ==============================================================================\n"
)

end_marker = (
    "# ==============================================================================\n"
    "# END DOMINO PROJECT ENVIRONMENT\n"
    "# ==============================================================================\n"
)

block = r'''
# ==============================================================================
# DOMINO PROJECT ENVIRONMENT
# ==============================================================================

# ------------------------------------------------------------------------------
# Persistent uv environment
# ------------------------------------------------------------------------------

export UV_HOME="/mnt/.uv"
export UV_INSTALL_DIR="/mnt/.uv/bin"
export UV_CACHE_DIR="/mnt/.uv/cache"
export UV_PYTHON_INSTALL_DIR="/mnt/.uv/python"
export UV_LINK_MODE="copy"

export PATH="/mnt/.uv/bin:${PATH}"


# ------------------------------------------------------------------------------
# Persistent Playwright browser cache
# ------------------------------------------------------------------------------

export PLAYWRIGHT_BROWSERS_PATH="/mnt/.cache/ms-playwright"


# ------------------------------------------------------------------------------
# Persistent GitHub SSH authentication
# ------------------------------------------------------------------------------
#
# Domino recreates /home/ubuntu between workspace sessions.
#
# The GitHub SSH key therefore lives in the persistent Domino dataset while
# ~/.ssh is reconstructed automatically whenever /mnt/.venv is activated.
#
# Never copy the private key into the project repository.

export DOMINO_GITHUB_SSH_DIR="/domino/datasets/local/nbcu_mmm_dev/pipeline/secrets/github"
export DOMINO_GITHUB_SSH_KEY="${DOMINO_GITHUB_SSH_DIR}/id_ed25519_github"
export DOMINO_GITHUB_KNOWN_HOSTS="${DOMINO_GITHUB_SSH_DIR}/known_hosts"

if [ -f "${DOMINO_GITHUB_SSH_KEY}" ]; then

    mkdir -p "${HOME}/.ssh"
    chmod 700 "${HOME}/.ssh"

    chmod 700 "${DOMINO_GITHUB_SSH_DIR}" 2>/dev/null || true
    chmod 600 "${DOMINO_GITHUB_SSH_KEY}" 2>/dev/null || true

    # Persist GitHub's host key alongside the persistent SSH key so a fresh
    # Domino container does not need manual known_hosts configuration.
    if [ ! -s "${DOMINO_GITHUB_KNOWN_HOSTS}" ]; then

        ssh-keyscan -H github.com \
            > "${DOMINO_GITHUB_KNOWN_HOSTS}" \
            2>/dev/null || true

        chmod 600 "${DOMINO_GITHUB_KNOWN_HOSTS}" \
            2>/dev/null || true

    fi

    cat > "${HOME}/.ssh/config" <<EOF
Host github.com
    HostName github.com
    User git
    IdentityFile ${DOMINO_GITHUB_SSH_KEY}
    IdentitiesOnly yes
    UserKnownHostsFile ${DOMINO_GITHUB_KNOWN_HOSTS}
    StrictHostKeyChecking yes
EOF

    chmod 600 "${HOME}/.ssh/config"

fi


# ------------------------------------------------------------------------------
# Restore ephemeral Ubuntu system dependencies
# ------------------------------------------------------------------------------

# Domino recreates the Ubuntu container between workspace sessions.
#
# Persistent Python packages live under /mnt/.venv, but Ubuntu packages do not.
# Restore system dependencies required by the project whenever the persistent
# virtual environment is activated in a fresh Domino container.

DOMINO_APT_PACKAGES=()

if ! ldconfig -p 2>/dev/null | grep -q "libgbm.so.1"; then
    DOMINO_APT_PACKAGES+=("libgbm1")
fi

if ! command -v cbc >/dev/null 2>&1; then
    DOMINO_APT_PACKAGES+=("coinor-cbc")
fi

if [ "${#DOMINO_APT_PACKAGES[@]}" -gt 0 ]; then

    export DEBIAN_FRONTEND=noninteractive

    echo "Installing ephemeral Ubuntu dependencies:"
    printf '  %s\n' "${DOMINO_APT_PACKAGES[@]}"

    sudo apt-get update -qq
    sudo apt-get install -y -qq "${DOMINO_APT_PACKAGES[@]}"

fi


# ==============================================================================
# END DOMINO PROJECT ENVIRONMENT
# ==============================================================================
'''

# Replace an existing managed block if one exists.
if start_marker in text and end_marker in text:

    before = text.split(start_marker, 1)[0]

    after = text.split(end_marker, 1)[1]

    text = (
        before
        + block.lstrip("\n")
        + after
    )

else:

    if not text.endswith("\n"):
        text += "\n"

    text += block


activate_path.write_text(text)

print(
    "Configured:",
    activate_path,
)
PY

echo


# ==============================================================================
# 10. INSTALL CURRENT-CONTAINER CHROMIUM DEPENDENCY
# ==============================================================================

echo "=== 10. Checking Chromium OS dependency ==="

if ! ldconfig -p 2>/dev/null | grep -q "libgbm.so.1"; then

    export DEBIAN_FRONTEND=noninteractive

    sudo apt-get update -qq

    sudo apt-get install -y -qq \
        libgbm1

else

    echo "libgbm.so.1 already available."

fi

echo


# ==============================================================================
# 11. INSTALL OR REUSE PLAYWRIGHT CHROMIUM
# ==============================================================================

echo "=== 11. Ensuring Playwright Chromium is available ==="

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH}"

"${VENV_DIR}/bin/python" \
    -m playwright install chromium

# ------------------------------------------------------------------------------
# Repair executable permissions after Domino workspace copy/restore
# ------------------------------------------------------------------------------
#
# A copied Domino workspace can preserve the Playwright browser files while
# stripping executable permissions. Playwright then sees the browser as
# installed but fails at launch with EACCES.
#
# Restore execute permissions on the browser entry points that Playwright may
# spawn directly.

echo "Repairing Playwright executable permissions..."

find "${PLAYWRIGHT_BROWSERS_PATH}" \
    -type f \
    \( \
        -name "chrome" \
        -o -name "chrome-headless-shell" \
        -o -name "chrome_crashpad_handler" \
        -o -name "chrome_sandbox" \
        -o -name "ffmpeg-linux" \
    \) \
    -exec chmod 755 {} +

echo "Playwright executable permissions ready."

echo "Playwright Chromium ready."
echo


# ==============================================================================
# 12. REGISTER PERSISTENT JUPYTER KERNEL
# ==============================================================================
#
# Use --sys-prefix rather than --user.
#
# /home/ubuntu is recreated with every Domino container, while /mnt/.venv
# persists. Installing the kernelspec into the venv therefore makes the kernel
# persistent as well.
#
# ==============================================================================

echo "=== 12. Registering persistent Jupyter kernel ==="

"${VENV_DIR}/bin/python" \
    -m ipykernel install \
    --sys-prefix \
    --name "${KERNEL_NAME}" \
    --display-name "${KERNEL_DISPLAY_NAME}"

echo


# ==============================================================================
# 13. CONFIGURE VS CODE
# ==============================================================================

echo "=== 13. Configuring VS Code ==="

mkdir -p \
    "${PROJECT_DIR}/.vscode-server/data/Machine"

cat > "${PROJECT_DIR}/.vscode-server/data/Machine/settings.json" <<EOF
{
    "workbench.list.openMode": "doubleClick",
    "python.defaultInterpreterPath": "${VENV_DIR}/bin/python",
    "python.terminal.activateEnvironment": true,
    "jupyter.askForKernelRestart": false
}
EOF

echo "VS Code interpreter:"
echo "  ${VENV_DIR}/bin/python"

echo


# ==============================================================================
# 14. VALIDATE PYTHON ENVIRONMENT
# ==============================================================================

echo "=== 14. Validating Python environment ==="

"${VENV_DIR}/bin/python" - <<'PY'
import importlib
import platform
import sys

print("Python executable:", sys.executable)
print("Python version:   ", platform.python_version())
print()

assert platform.python_version() == "3.12.13", (
    f"Expected Python 3.12.13; "
    f"found {platform.python_version()}"
)

packages = [
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "sklearn",
    "statsmodels",
    "pyarrow",
    "duckdb",
    "google.cloud.bigquery",
    "geopandas",
    "shapely",
    "pyproj",
    "rasterio",
    "h3",
    "lightgbm",
    "shap",
    "optuna",
    "playwright",
]

failures = []

for package in packages:

    try:

        module = importlib.import_module(package)

        version = getattr(
            module,
            "__version__",
            "installed",
        )

        print(
            f"{package:25s} "
            f"OK   "
            f"{version}"
        )

    except Exception as exc:

        failures.append(
            (
                package,
                type(exc).__name__,
                str(exc),
            )
        )

        print(
            f"{package:25s} "
            f"FAIL "
            f"{type(exc).__name__}: {exc}"
        )


if failures:

    raise RuntimeError(
        f"{len(failures)} package validation(s) failed."
    )


print()
print("Python package validation complete.")
PY

echo


# ==============================================================================
# 15. VALIDATE PLAYWRIGHT
# ==============================================================================

echo "=== 15. Validating Playwright Chromium ==="

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH}"

"${VENV_DIR}/bin/python" - <<'PY'
import asyncio

from playwright.async_api import async_playwright


async def main():

    async with async_playwright() as playwright:

        browser = await playwright.chromium.launch(
            headless=True
        )

        page = await browser.new_page()

        await page.set_content(
            "<html>"
            "<title>Domino Environment Test</title>"
            "<body>OK</body>"
            "</html>"
        )

        title = await page.title()

        body = await page.locator(
            "body"
        ).inner_text()

        assert title == "Domino Environment Test"
        assert body == "OK"

        print("Chromium launch: OK")
        print("Page title:      ", title)
        print("Body text:       ", body)

        await browser.close()


asyncio.run(main())
PY

echo


# ==============================================================================
# 16. FINAL INFORMATION
# ==============================================================================

echo "=== 16. Final environment information ==="

echo
echo "Python:"
"${VENV_DIR}/bin/python" --version

echo
echo "Python executable:"
"${VENV_DIR}/bin/python" \
    -c 'import sys; print(sys.executable)'

echo
echo "uv:"
"${UV_BIN}" --version

echo
echo "Jupyter kernels:"
"${VENV_DIR}/bin/jupyter" kernelspec list

echo
echo "Playwright cache:"
du -sh "${PLAYWRIGHT_BROWSERS_PATH}" \
    2>/dev/null \
    || true

echo


# ==============================================================================
# COMPLETE
# ==============================================================================

echo "=============================================================================="
echo "DOMINO ENVIRONMENT SETUP COMPLETE"
echo "=============================================================================="
echo
echo "Persistent environment:"
echo
echo "  Python:      ${PYTHON_VERSION}"
echo "  Interpreter: ${VENV_DIR}/bin/python"
echo "  uv:          ${UV_BIN}"
echo "  Browsers:    ${PLAYWRIGHT_BROWSERS_PATH}"
echo
echo "Domino will automatically activate:"
echo
echo "  ${VENV_DIR}"
echo
echo "on new terminals."
echo