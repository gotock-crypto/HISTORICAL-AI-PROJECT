# Установка / обновление History Bot V5

## Windows: загрузка архива

Откройте PowerShell:

```powershell
cd D:\Project\MAX
scp ".\history-bot-v5-reliable-10of10.tar.gz" root@89.44.84.51:/tmp/
ssh root@89.44.84.51
```

Если SSH-пользователь не `root`, замените `root` на вашего пользователя.

## Сервер

Сначала проверьте реальный путь:

```bash
sudo systemctl cat history-bot
```

Инструкция ниже предполагает `/opt/history-bot`.

### 1. Остановить сервис

```bash
sudo systemctl stop history-bot
```

### 2. Сделать backup

```bash
sudo tar -C /opt -czf /opt/history-bot-backup-$(date +%F-%H%M%S).tar.gz history-bot
```

### 3. Проверить SHA256

```bash
sha256sum /tmp/history-bot-v5-reliable-10of10.tar.gz
```

Сверьте с SHA256, указанным в релизе.

### 4. Распаковать во временную папку

```bash
sudo rm -rf /tmp/history-v5-update
sudo mkdir -p /tmp/history-v5-update
sudo tar -xzf /tmp/history-bot-v5-reliable-10of10.tar.gz -C /tmp/history-v5-update
```

### 5. Наложить код без удаления рабочих данных

```bash
sudo cp -a /tmp/history-v5-update/history-bot/. /opt/history-bot/
```

Не используйте `rm -rf /opt/history-bot`: должны сохраниться `.env`, `data/`, `runtime/`, `venv/` и MAX-сессия.

### 6. Проверить код

```bash
cd /opt/history-bot
source venv/bin/activate
pip install -r requirements.txt
python self_test.py
python -m py_compile main.py history_bot/*.py history_bot/content/*.py history_bot/max/*.py
python main.py --health
```

### 7. Ручная генерация обязательного фото-поста

```bash
python main.py --once
```

Убедитесь, что в выводе есть `IMAGE`, `LOCAL`, `SOURCE`, а локальный файл существует.

### 8. Запустить сервис

```bash
sudo systemctl start history-bot
sudo systemctl status history-bot --no-pager
journalctl -u history-bot -n 150 --no-pager
```

## Откат

```bash
sudo systemctl stop history-bot
sudo rm -rf /opt/history-bot
sudo tar -C /opt -xzf /opt/history-bot-backup-YYYY-MM-DD-HHMMSS.tar.gz
sudo systemctl start history-bot
```
