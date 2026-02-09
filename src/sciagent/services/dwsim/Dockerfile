# DWSIM: Open-source chemical process simulator
# Includes: DWSIM 9.0.4, .NET 8 runtime, pythonnet for automation
# License: GPL-3.0
# Note: DWSIM is only available for amd64 architecture
FROM --platform=linux/amd64 mcr.microsoft.com/dotnet/runtime:8.0-bookworm-slim

LABEL org.opencontainers.image.source="https://github.com/sciagent-ai/scienv"
LABEL org.opencontainers.image.description="DWSIM chemical process simulator with Python automation"
LABEL org.opencontainers.image.licenses="GPL-3.0"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV DWSIM_PATH=/usr/local/lib/dwsim

# Install Python and system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    wget \
    curl \
    p7zip-full \
    # Libraries required by DWSIM
    libfontconfig1 \
    libfreetype6 \
    libx11-6 \
    libxext6 \
    libxrender1 \
    libice6 \
    libsm6 \
    libgl1 \
    libglib2.0-0 \
    # IPOPT solver (required by DWSIM for optimization)
    coinor-libipopt1v5 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Download and extract DWSIM 9.0.4 portable version
RUN wget -q "https://downloads.sourceforge.net/project/dwsim/DWSIM/DWSIM%209.0/9.0.4/dwsim_9.0.4-amd64_portable.7z" -O /tmp/dwsim.7z \
    && mkdir -p /usr/local/lib \
    && cd /usr/local/lib \
    && 7z x /tmp/dwsim.7z \
    && rm /tmp/dwsim.7z \
    && chmod -R 755 /usr/local/lib/dwsim

# Create virtual environment and install Python packages
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install pythonnet and scientific packages
RUN pip install --no-cache-dir \
    pythonnet>=3.0.0 \
    numpy>=1.26 \
    scipy>=1.12 \
    matplotlib>=3.8 \
    pandas>=2.2

# Create initialization script for DWSIM automation
RUN mkdir -p /opt/dwsim
COPY <<'EOF' /opt/dwsim/init_dwsim.py
"""
DWSIM Automation Initialization Module

This module provides helper functions to initialize DWSIM automation
using pythonnet with the .NET Core runtime.

Usage:
    from init_dwsim import create_flowsheet, load_flowsheet

    # Create a new flowsheet
    manager, flowsheet = create_flowsheet()

    # Or load an existing simulation
    manager, flowsheet = load_flowsheet("/path/to/simulation.dwxmz")
"""

import os
import sys

# Configure pythonnet to use coreclr runtime
from pythonnet import load
load("coreclr")

import clr
from System.IO import Directory

# DWSIM installation path
DWSIM_PATH = "/usr/local/lib/dwsim"

# Add DWSIM assemblies
_dlls = [
    "CapeOpen.dll",
    "DWSIM.Automation.dll",
    "DWSIM.Interfaces.dll",
    "DWSIM.GlobalSettings.dll",
    "DWSIM.SharedClasses.dll",
    "DWSIM.Thermodynamics.dll",
    "DWSIM.Thermodynamics.ThermoC.dll",
    "DWSIM.UnitOperations.dll",
    "DWSIM.Inspector.dll",
]

for dll in _dlls:
    dll_path = os.path.join(DWSIM_PATH, dll)
    if os.path.exists(dll_path):
        clr.AddReference(dll_path)

# Set current directory to DWSIM path for assembly resolution
Directory.SetCurrentDirectory(DWSIM_PATH)

# Import DWSIM automation classes
from DWSIM.Automation import Automation3
from DWSIM.Interfaces.Enums.GraphicObjects import ObjectType
from DWSIM.Thermodynamics import Streams, PropertyPackages
from DWSIM.UnitOperations import UnitOperations
from DWSIM.GlobalSettings import Settings


def create_flowsheet():
    """
    Create a new DWSIM flowsheet.

    Returns:
        tuple: (Automation3 manager, IFlowsheet flowsheet)
    """
    manager = Automation3()
    flowsheet = manager.CreateFlowsheet()
    return manager, flowsheet


def load_flowsheet(filepath):
    """
    Load an existing DWSIM simulation file.

    Args:
        filepath: Path to .dwxmz or .dwxml simulation file

    Returns:
        tuple: (Automation3 manager, IFlowsheet flowsheet)
    """
    manager = Automation3()
    flowsheet = manager.LoadFlowsheet(filepath)
    return manager, flowsheet


def calculate_flowsheet(manager, flowsheet):
    """
    Calculate/solve a flowsheet.

    Args:
        manager: Automation3 manager instance
        flowsheet: IFlowsheet instance

    Returns:
        list: Any errors encountered during calculation
    """
    Settings.SolverMode = 0
    return manager.CalculateFlowsheet2(flowsheet)


def save_flowsheet(manager, flowsheet, filepath, compressed=True):
    """
    Save a flowsheet to file.

    Args:
        manager: Automation3 manager instance
        flowsheet: IFlowsheet instance
        filepath: Output file path (.dwxmz for compressed, .dwxml otherwise)
        compressed: Whether to save as compressed format
    """
    manager.SaveFlowsheet(flowsheet, filepath, compressed)


# Available object types for adding to flowsheet
OBJECT_TYPES = {
    'material_stream': ObjectType.MaterialStream,
    'energy_stream': ObjectType.EnergyStream,
    'heater': ObjectType.Heater,
    'cooler': ObjectType.Cooler,
    'pump': ObjectType.Pump,
    'compressor': ObjectType.Compressor,
    'valve': ObjectType.Valve,
    'mixer': ObjectType.Mixer,
    'splitter': ObjectType.Splitter,
    'heat_exchanger': ObjectType.HeatExchanger,
    'distillation_column': ObjectType.DistillationColumn,
    'absorption_column': ObjectType.AbsorptionColumn,
    'reactor_conversion': ObjectType.RCT_Conversion,
    'reactor_equilibrium': ObjectType.RCT_Equilibrium,
    'reactor_gibbs': ObjectType.RCT_Gibbs,
    'reactor_cstr': ObjectType.RCT_CSTR,
    'reactor_pfr': ObjectType.RCT_PFR,
    'flash_tank': ObjectType.Vessel,
    'separator': ObjectType.ComponentSeparator,
}

print("DWSIM automation initialized successfully")
print(f"DWSIM path: {DWSIM_PATH}")
EOF

# Create workspace
WORKDIR /workspace

# Verify installation - check DWSIM files exist
RUN ls /usr/local/lib/dwsim/DWSIM.Automation.dll && echo "DWSIM installation verified"

# Add DWSIM path to PYTHONPATH for easy imports
ENV PYTHONPATH="/opt/dwsim"

CMD ["python3"]
