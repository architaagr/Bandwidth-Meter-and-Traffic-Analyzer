#!/bin/bash

set -e

echo "Updating package lists..."
sudo apt update

echo "Installing system dependencies..."
sudo apt install -y git python3 python3-pip python3-venv mininet openvswitch-switch

if [ -d "$HOME/osken-env" ]; then
    echo "Existing OS-Ken environment found at ~/osken-env"
else
    echo "Creating OS-Ken virtual environment..."
    python3 -m venv "$HOME/osken-env"
fi

echo "Activating OS-Ken virtual environment..."
source "$HOME/osken-env/bin/activate"

echo "Upgrading pip..."
pip install --upgrade pip

echo "Installing Python dependencies..."
pip install -r requirements.txt

echo ""
echo "Setup complete!"
echo "Verify OS-Ken with:"
echo "osken-manager --version"
