# Deploy на Hetzner VPS через Git

## 1. Підготувати репозиторій локально

Встановіть Git на вашому ПК, якщо його ще немає.

Далі в папці проєкту:

```bash
git init
git add .
git commit -m "Initial OLX bot"
git branch -M main
git remote add origin <URL_ВАШОГО_REPO>
git push -u origin main
```

## 2. Підготувати сервер Hetzner

Підключіться по SSH та встановіть Docker, Compose plugin і Git:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker
sudo mkdir -p /opt/olx_bot
sudo chown -R $USER:$USER /opt/olx_bot
```

Склонуйте проєкт:

```bash
cd /opt
git clone <URL_ВАШОГО_REPO> olx_bot
cd /opt/olx_bot
```

## 3. Створити серверні файли, які не йдуть у Git

Створіть `.env`:

```bash
cp .env.example .env
nano .env
```

Опціонально створіть `config.json`, якщо хочете мати стартові фільтри у файлі:

```bash
cp config.example.json config.json
nano config.json
```

## 4. Перший запуск

```bash
mkdir -p data logs
docker compose up -d --build
docker compose logs -f
```

## 5. Оновлення після `git push`

На сервері достатньо:

```bash
cd /opt/olx_bot
bash deploy.sh
```

## 6. Опціонально: деплой однією командою з локального ПК

```bash
ssh root@<IP_СЕРВЕРА> 'cd /opt/olx_bot && bash deploy.sh'
```

## Що зберігається поза Git

- `.env` містить `BOT_TOKEN` і `CHAT_ID`
- `config.json` опціонально містить стартові фільтри
- `data/` і `logs/` зберігають стан та логи між перезапусками
