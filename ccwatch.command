#!/bin/bash
# Abre o ccwatch no macOS (duplo clique). O servidor encerra sozinho ~3 min após fechar as janelas.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null; then
  echo "python3 não encontrado. Instale o Python 3 (python.org) ou rode: xcode-select --install"
  read -r -p "Enter para fechar"; exit 1
fi
python3 server.py
