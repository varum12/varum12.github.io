#!/bin/bash
# Установка YT Factory на Mac (Apple Silicon или Intel). Запуск: bash setup_mac.sh
set -e
cd "$(dirname "$0")"

if ! command -v brew >/dev/null 2>&1; then
  echo "==> Устанавливаю Homebrew (попросит пароль от Mac)"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$b" ]; then
      eval "$($b shellenv)"
      grep -q "brew shellenv" ~/.zprofile 2>/dev/null || echo "eval \"\$($b shellenv)\"" >> ~/.zprofile
    fi
  done
fi

echo "==> Устанавливаю ffmpeg и Python"
brew install ffmpeg python

echo "==> Создаю окружение и ставлю зависимости"
"$(brew --prefix)/bin/python3" -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
chmod +x video

echo
echo "Готово! Проверка: ./video scripts/de/001-gehirn-absturz.yaml"
