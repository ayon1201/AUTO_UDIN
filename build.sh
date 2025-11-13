#!/usr/bin/env bash
# exit on error
set -o errexit

# Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Update apt packages
apt-get update

# Install Chromium and dependencies
apt-get install -y \
    chromium \
    chromium-driver \
    tesseract-ocr \
    libnss3 \
    libgconf-2-4 \
    libxi6 \
    libxcursor1 \
    libxss1 \
    libxrandr2 \
    libasound2 \
    libpangocairo-1.0-0 \
    libatk1.0-0 \
    libcups2 \
    libxcomposite1 \
    libxdamage1

# Clean up
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "Build completed successfully!"
